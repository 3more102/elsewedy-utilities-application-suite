import sqlite3
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

import app.main as mainmod
from app.main import app

TEST_DB = Path(__file__).resolve().parent / "euas_test.db"


def auth(client, username="omar", password="EUAS@2026"):
    response = client.post("/api/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def insert_outbox(*, status="Pending", attempts=0, last_error=""):
    event_no = f"EVT-TEST-{uuid4().hex[:16].upper()}"
    with sqlite3.connect(TEST_DB) as conn:
        cur = conn.execute(
            """
            INSERT INTO event_outbox(
                event_no,event_type,aggregate_type,aggregate_id,payload_json,
                status,attempts,created_at,processed_at,last_error
            ) VALUES(?,?,?,?,?,?,?,?,NULL,?)
            """,
            (
                event_no,
                "test.lifecycle",
                "test",
                event_no,
                "{}",
                status,
                attempts,
                mainmod.now(),
                last_error,
            ),
        )
        conn.commit()
        return cur.lastrowid, event_no


def get_outbox(event_id):
    with sqlite3.connect(TEST_DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM event_outbox WHERE id=?", (event_id,)).fetchone()
        assert row is not None
        return dict(row)


def test_final_failed_attempt_moves_event_to_dead_letter(monkeypatch):
    monkeypatch.setattr(mainmod, "EVENT_WEBHOOK_URL", "https://integration.example.test/euas/events")
    monkeypatch.setattr(mainmod, "OUTBOX_MAX_ATTEMPTS", 2)

    def fail_delivery(*args, **kwargs):
        raise OSError("integration unavailable")

    monkeypatch.setattr(mainmod.urllib_request, "urlopen", fail_delivery)

    with TestClient(app):
        event_id, _ = insert_outbox(status="Failed", attempts=1, last_error="first failure")
        with mainmod.db() as conn:
            result = mainmod._process_outbox(conn)

        event = get_outbox(event_id)
        assert event["status"] == "DeadLetter"
        assert event["attempts"] == 2
        assert event["processed_at"]
        assert "integration unavailable" in event["last_error"]
        assert result["dead_lettered"] >= 1


def test_legacy_exhausted_failed_event_is_normalized_to_dead_letter(monkeypatch):
    monkeypatch.setattr(mainmod, "OUTBOX_MAX_ATTEMPTS", 3)
    with TestClient(app):
        event_id, _ = insert_outbox(status="Failed", attempts=3, last_error="already exhausted")
        with mainmod.db() as conn:
            result = mainmod._process_outbox(conn)

        event = get_outbox(event_id)
        assert event["status"] == "DeadLetter"
        assert event["attempts"] == 3
        assert event["processed_at"]
        assert event["last_error"] == "already exhausted"
        assert result["dead_lettered"] >= 1


def test_manual_retry_rearms_dead_lettered_event(monkeypatch):
    monkeypatch.setattr(mainmod, "OUTBOX_MAX_ATTEMPTS", 2)
    with TestClient(app) as client:
        admin = auth(client)
        event_id, event_no = insert_outbox(status="DeadLetter", attempts=2, last_error="exhausted")

        response = client.post(f"/api/events/outbox/{event_id}/retry", headers=admin)
        assert response.status_code == 200, response.text
        assert response.json()["event_no"] == event_no

        event = get_outbox(event_id)
        assert event["status"] == "Pending"
        assert event["attempts"] == 0
        assert event["processed_at"] is None
        assert event["last_error"] == ""


def test_dead_letter_is_terminal_for_queue_and_visible_in_metrics(monkeypatch):
    monkeypatch.setattr(mainmod, "OUTBOX_MAX_ATTEMPTS", 2)
    with TestClient(app) as client:
        admin = auth(client)
        event_id, _ = insert_outbox(status="DeadLetter", attempts=2, last_error="exhausted")

        status = client.get("/api/automation/status", headers=admin)
        assert status.status_code == 200, status.text
        assert status.json()["queue"]["outbox_dead_lettered"] >= 1

        metrics = client.get("/api/metrics", headers=admin)
        assert metrics.status_code == 200, metrics.text
        assert "euas_outbox_dead_lettered " in metrics.text

        event = get_outbox(event_id)
        assert event["status"] == "DeadLetter"
