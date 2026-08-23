import io
import json
import sqlite3
import zipfile
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app

TEST_DB = Path(__file__).resolve().parent / 'euas_test.db'


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def test_v45_legal_hold_and_credential_verified_retention_execution():
    with TestClient(app) as client:
        admin = auth(client)
        with sqlite3.connect(TEST_DB) as conn:
            old = '2000-01-01T00:00:00'
            held_id = conn.execute("INSERT INTO notifications(user_id,role_code,title,message,severity,link_module,link_id,is_read,created_at) VALUES(NULL,NULL,'V45 held','held notification','Info','','',0,?)", (old,)).lastrowid
            purge_id = conn.execute("INSERT INTO notifications(user_id,role_code,title,message,severity,link_module,link_id,is_read,created_at) VALUES(NULL,NULL,'V45 purge','purge notification','Info','','',0,?)", (old,)).lastrowid
            conn.commit()

        hold = client.post('/api/governance/retention/holds', headers=admin, json={
            'data_class': 'Notifications', 'record_key': str(held_id),
            'reason': 'Preserve notification for active investigation evidence.'
        })
        assert hold.status_code == 200, hold.text
        hold_id = hold.json()['id']

        preview = client.post('/api/governance/retention/runs', headers=admin, json={'mode': 'Preview', 'data_class': 'Notifications'})
        assert preview.status_code == 200, preview.text
        p = preview.json()
        assert p['summary']['held_records'] >= 1
        assert p['summary']['executable_records'] >= 1
        assert p['summary']['purged_records'] == 0

        missing = client.post('/api/governance/retention/runs', headers=admin, json={'mode': 'Execute', 'data_class': 'Notifications'})
        assert missing.status_code == 400
        wrong_password = client.post('/api/governance/retention/runs', headers=admin, json={
            'mode': 'Execute', 'data_class': 'Notifications', 'confirmation': 'EXECUTE RETENTION', 'current_password': 'wrong-password'
        })
        assert wrong_password.status_code == 401

        executed = client.post('/api/governance/retention/runs', headers=admin, json={
            'mode': 'Execute', 'data_class': 'Notifications', 'confirmation': 'EXECUTE RETENTION', 'current_password': 'EUAS@2026'
        })
        assert executed.status_code == 200, executed.text
        data = executed.json()
        assert data['summary']['purged_records'] >= 1
        with sqlite3.connect(TEST_DB) as conn:
            assert conn.execute('SELECT COUNT(*) FROM notifications WHERE id=?', (purge_id,)).fetchone()[0] == 0
            assert conn.execute('SELECT COUNT(*) FROM notifications WHERE id=?', (held_id,)).fetchone()[0] == 1

        bad_release = client.post(f'/api/governance/retention/holds/{hold_id}/release', headers=admin, json={
            'current_password': 'wrong-password', 'reason': 'Investigation is complete and evidence is released.'
        })
        assert bad_release.status_code == 401
        released = client.post(f'/api/governance/retention/holds/{hold_id}/release', headers=admin, json={
            'current_password': 'EUAS@2026', 'reason': 'Investigation is complete and evidence is released.'
        })
        assert released.status_code == 200, released.text
        rerun = client.post('/api/governance/retention/runs', headers=admin, json={
            'mode': 'Execute', 'data_class': 'Notifications', 'confirmation': 'EXECUTE RETENTION', 'current_password': 'EUAS@2026'
        })
        assert rerun.status_code == 200 and rerun.json()['summary']['purged_records'] >= 1
        with sqlite3.connect(TEST_DB) as conn:
            assert conn.execute('SELECT COUNT(*) FROM notifications WHERE id=?', (held_id,)).fetchone()[0] == 0


def test_v45_protected_data_evidence_package_and_retention_chain_tamper_detection():
    with TestClient(app) as client:
        admin = auth(client)
        with sqlite3.connect(TEST_DB) as conn:
            work = conn.execute('SELECT id,created_at FROM work_orders ORDER BY id LIMIT 1').fetchone()
            assert work
            original_created = work[1]
            conn.execute("UPDATE work_orders SET created_at='2000-01-01T00:00:00' WHERE id=?", (work[0],))
            conn.execute("UPDATE retention_policies SET retention_days=1,active=1 WHERE data_class='Work Management'")
            conn.commit()

        run = client.post('/api/governance/retention/runs', headers=admin, json={
            'mode': 'Execute', 'data_class': 'Work Management', 'confirmation': 'EXECUTE RETENTION', 'current_password': 'EUAS@2026'
        })
        assert run.status_code == 200, run.text
        info = run.json()
        assert info['summary']['protected_records'] >= 1
        assert info['summary']['purged_records'] == 0
        with sqlite3.connect(TEST_DB) as conn:
            assert conn.execute('SELECT COUNT(*) FROM work_orders WHERE id=?', (work[0],)).fetchone()[0] == 1
            conn.execute('UPDATE work_orders SET created_at=? WHERE id=?', (original_created, work[0]))
            conn.commit()

        detail = client.get(f"/api/governance/retention/runs/{info['id']}", headers=admin)
        assert detail.status_code == 200 and detail.json()['manifest']['format'] == 'EUAS-Retention-Evidence-v1'
        evidence = client.get(f"/api/governance/retention/runs/{info['id']}/evidence", headers=admin)
        assert evidence.status_code == 200 and evidence.headers['content-type'].startswith('application/zip')
        with zipfile.ZipFile(io.BytesIO(evidence.content)) as z:
            assert {'manifest.json', 'items.csv', 'verification.json'} <= set(z.namelist())
            manifest = json.loads(z.read('manifest.json'))
            assert manifest['run_no'] == info['run_no']

        verify = client.get('/api/governance/retention/verify', headers=admin)
        assert verify.status_code == 200 and verify.json()['valid'] is True

        with sqlite3.connect(TEST_DB) as conn:
            row = conn.execute('SELECT id,manifest_json FROM retention_runs ORDER BY id DESC LIMIT 1').fetchone()
            original = row[1]
            conn.execute("UPDATE retention_runs SET manifest_json='{}' WHERE id=?", (row[0],))
            conn.commit()
        broken = client.get('/api/governance/retention/verify', headers=admin)
        assert broken.status_code == 200 and broken.json()['valid'] is False
        with sqlite3.connect(TEST_DB) as conn:
            conn.execute('UPDATE retention_runs SET manifest_json=? WHERE id=?', (original, row[0]))
            conn.commit()
        restored = client.get('/api/governance/retention/verify', headers=admin)
        assert restored.status_code == 200 and restored.json()['valid'] is True

        export = client.get('/api/exports/retention-runs.csv', headers=admin)
        assert export.status_code == 200 and info['run_no'] in export.text
        metrics = client.get('/api/metrics', headers=admin).text
        assert 'euas_retention_runs_total ' in metrics
        assert 'euas_retention_purged_records_total ' in metrics
        assert 'euas_active_retention_holds ' in metrics
        assert 'euas_retention_run_chain_valid 1' in metrics
