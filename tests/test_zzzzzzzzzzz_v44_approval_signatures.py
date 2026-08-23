import sqlite3
from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app

TEST_DB = Path(__file__).resolve().parent / 'euas_test.db'


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def make_submitted_work(client, admin):
    ref = client.get('/api/reference', headers=admin).json()
    supervisor = next(x for x in ref['users'] if x['username'] == 'supervisor')
    asset = next(x for x in client.get('/api/assets', headers=admin).json() if x['asset_no'] == 'TR-001')
    created = client.post('/api/work-orders', headers=admin, json={
        'title': 'V4.4 approval signature evidence regression',
        'asset_id': asset['id'], 'priority': 'High', 'supervisor_id': supervisor['id'],
        'estimated_hours': 1.5,
    })
    assert created.status_code == 200, created.text
    wid = created.json()['id']
    submitted = client.post(f'/api/work-orders/{wid}/transition', headers=admin, json={'action': 'submit'})
    assert submitted.status_code == 200, submitted.text
    return wid


def test_v44_approval_signature_requires_reauth_intent_and_records_evidence():
    with TestClient(app) as client:
        admin = auth(client)
        supervisor = auth(client, 'supervisor', 'Supervisor@2026')
        wid = make_submitted_work(client, admin)
        approval = next(x for x in client.get('/api/approvals?status=Pending', headers=supervisor).json()
                        if x['record_type'] == 'work_order' and x['record_id'] == wid)
        intent = f"I approve {approval['record_code']}"

        missing = client.post(f"/api/approvals/{approval['id']}/decision", headers=supervisor,
                              json={'decision': 'approve'})
        assert missing.status_code == 400
        assert client.get(f'/api/work-orders/{wid}', headers=admin).json()['status'] == 'Submitted'

        wrong_intent = client.post(f"/api/approvals/{approval['id']}/decision", headers=supervisor,
                                   json={'decision': 'approve', 'current_password': 'Supervisor@2026', 'signer_intent': 'I approve something else'})
        assert wrong_intent.status_code == 400

        wrong_password = client.post(f"/api/approvals/{approval['id']}/decision", headers=supervisor,
                                     json={'decision': 'approve', 'current_password': 'wrong-password', 'signer_intent': intent})
        assert wrong_password.status_code == 401

        signed = client.post(f"/api/approvals/{approval['id']}/decision", headers=supervisor, json={
            'decision': 'approve', 'comments': 'Credential verified in v4.4 regression',
            'current_password': 'Supervisor@2026', 'signer_intent': intent,
        })
        assert signed.status_code == 200, signed.text
        evidence_meta = signed.json()['signature_evidence']
        assert evidence_meta['evidence_no'].startswith('SIG-')
        assert len(evidence_meta['evidence_hash']) == 64
        assert client.get(f'/api/work-orders/{wid}', headers=admin).json()['status'] == 'Approved'

        evidence = client.get(f"/api/approvals/{approval['id']}/signature-evidence", headers=supervisor)
        assert evidence.status_code == 200, evidence.text
        data = evidence.json()
        assert data['credential_verified'] == 1
        assert data['signer_username'] == 'supervisor'
        assert data['intent_statement'] == intent
        assert data['payload']['record_snapshot']['status'] == 'Approved'
        assert data['payload']['signer']['username'] == 'supervisor'

        queue = client.get('/api/approvals?status=', headers=admin).json()
        signed_row = next(x for x in queue if x['id'] == approval['id'])
        assert signed_row['evidence_no'] == data['evidence_no']
        assert signed_row['credential_verified'] == 1

        integrity = client.get('/api/approval-signatures/verify', headers=admin)
        assert integrity.status_code == 200 and integrity.json()['valid'] is True
        assert integrity.json()['checked'] >= 1 and len(integrity.json()['head_hash']) == 64

        metrics = client.get('/api/metrics', headers=admin).text
        assert 'euas_signed_approvals_total ' in metrics
        assert 'euas_approval_signature_chain_valid 1' in metrics
        export = client.get('/api/exports/approval-signatures.csv', headers=admin)
        assert export.status_code == 200 and data['evidence_no'] in export.text and intent in export.text


def test_v44_signature_chain_detects_tampering_and_preserves_delegation_evidence():
    with TestClient(app) as client:
        admin = auth(client)
        # Earlier regression coverage creates at least one approval signed under active delegation.
        export = client.get('/api/exports/approval-signatures.csv', headers=admin)
        assert export.status_code == 200
        assert 'Approved under active delegation' in export.text

        with sqlite3.connect(TEST_DB) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute('SELECT id,comments FROM approval_signature_evidence ORDER BY id DESC LIMIT 1').fetchone()
            assert row is not None
            original = row['comments'] or ''
            conn.execute('UPDATE approval_signature_evidence SET comments=? WHERE id=?', ('TAMPERED-EVIDENCE', row['id']))
            conn.commit()

        broken = client.get('/api/approval-signatures/verify', headers=admin)
        assert broken.status_code == 200
        assert broken.json()['valid'] is False
        assert broken.json()['reason'] == 'column_payload_mismatch'

        with sqlite3.connect(TEST_DB) as conn:
            conn.execute('UPDATE approval_signature_evidence SET comments=? WHERE id=?', (original, row['id']))
            conn.commit()

        restored = client.get('/api/approval-signatures/verify', headers=admin)
        assert restored.status_code == 200 and restored.json()['valid'] is True

        with sqlite3.connect(TEST_DB) as conn:
            delegated = conn.execute('SELECT COUNT(*) FROM approval_signature_evidence WHERE delegated_authority=1').fetchone()[0]
        assert delegated >= 1

        # Release metadata is source-measured so stale version/schema badges cannot silently ship again.
        import json, re
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / 'RELEASE_MANIFEST.json').read_text(encoding='utf-8'))
        main_src = (root / 'app' / 'main.py').read_text(encoding='utf-8')
        db_src = (root / 'core' / 'database' / 'runtime.py').read_text(encoding='utf-8')
        readme = (root / 'README.md').read_text(encoding='utf-8')
        sw = (root / 'static' / 'sw.js').read_text(encoding='utf-8')
        all_routes = re.findall(r"@app\.(?:get|post|put|patch|delete)\((['\"])(.*?)\1", main_src)
        api_routes = [path for _, path in all_routes if path.startswith('/api/')]
        current_version = (root / 'VERSION').read_text(encoding='utf-8').strip()
        config_src = (root / 'core' / 'configuration' / 'settings.py').read_text(encoding='utf-8')
        schema_version = int(re.search(r'SCHEMA_VERSION\s*=\s*(\d+)', config_src).group(1))
        test_count = sum(len(re.findall(r'^def test_', f.read_text(encoding='utf-8'), re.M)) for f in (root / 'tests').rglob('test_*.py'))
        assert manifest['version'] == current_version and manifest['schema_version'] == schema_version
        assert manifest['api_endpoints'] == len(all_routes)
        assert manifest['api_routes_under_api'] == len(api_routes)
        assert manifest['relational_tables'] == len(re.findall(r'CREATE TABLE IF NOT EXISTS\s+', db_src, re.I))
        assert manifest['explicit_indexes'] == len(re.findall(r'CREATE (?:UNIQUE )?INDEX IF NOT EXISTS\s+', db_src, re.I))
        assert manifest['automated_tests'] == test_count
        assert f'version-{current_version}' in readme and f'schema-v{schema_version}' in readme and f'regression_tests-{test_count}' in readme
        assert f"euas-shell-v{current_version}" in sw
