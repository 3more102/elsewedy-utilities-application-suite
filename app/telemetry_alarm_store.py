from __future__ import annotations

from . import application as _application


TELEMETRY_ALARM_LOCK_ID = 1
_legacy_evaluate_telemetry_alarm = _application._evaluate_telemetry_alarm


class TelemetryAlarmCoordinatorUnavailable(RuntimeError):
    """Raised when telemetry alarm evaluation starts before its coordinator exists."""


def ensure_telemetry_alarm_lock(conn) -> None:
    conn.execute(
        '''CREATE TABLE IF NOT EXISTS telemetry_alarm_lock(
             id INTEGER PRIMARY KEY,
             guard INTEGER NOT NULL DEFAULT 0
           )'''
    )
    conn.execute(
        'INSERT OR IGNORE INTO telemetry_alarm_lock(id,guard) VALUES(?,0)',
        (TELEMETRY_ALARM_LOCK_ID,),
    )


def _lock_telemetry_alarm_coordinator(conn) -> None:
    locked = conn.execute(
        'UPDATE telemetry_alarm_lock SET guard=guard WHERE id=?',
        (TELEMETRY_ALARM_LOCK_ID,),
    )
    if int(locked.rowcount or 0) != 1:
        raise TelemetryAlarmCoordinatorUnavailable(
            'telemetry alarm coordinator is not initialized'
        )


def evaluate_telemetry_alarm_atomic(
    conn,
    channel: dict,
    value: float,
    captured_at: str,
    actor_id: int | None,
):
    """Serialize the complete active-alarm open/update/clear decision.

    The historical evaluator remains the sole owner of threshold semantics,
    notification/outbox payloads, alarm numbering, occurrence counts, and audit
    behavior. The global coordinator only makes its read-active-then-mutate
    sequence linearizable. A global gate is deliberate because one ingestion
    transaction may evaluate several channels; per-channel row locks retained
    until commit could deadlock across batches that list channels in opposite
    orders.
    """
    _lock_telemetry_alarm_coordinator(conn)
    return _legacy_evaluate_telemetry_alarm(
        conn,
        channel,
        float(value),
        captured_at,
        actor_id,
    )


def install_telemetry_alarm_evaluator() -> None:
    if _application._evaluate_telemetry_alarm is evaluate_telemetry_alarm_atomic:
        return
    _application._evaluate_telemetry_alarm = evaluate_telemetry_alarm_atomic
