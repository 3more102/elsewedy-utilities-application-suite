from pathlib import Path
from fastapi.testclient import TestClient
from app.main import app
from app.database import init_db
from app.auth import hash_password


def auth(client, username='omar', password='EUAS@2026'):
    r = client.post('/api/auth/login', json={'username': username, 'password': password})
    assert r.status_code == 200, r.text
    return {'Authorization': f"Bearer {r.json()['token']}"}


def test_v46_user_override_precedence_reauth_and_enforced_write_path():
    with TestClient(app) as client:
        admin = auth(client)
        planner = auth(client, 'planner', 'Planner@2026')
        snapshot = client.get('/api/admin/access-control', headers=admin)
        assert snapshot.status_code == 200, snapshot.text
        access = snapshot.json()
        assert access['confirmation_phrase'] == 'UPDATE ACCESS'
        assert any(p['code'] == 'work.write' and p['risk_level'] == 'Elevated' for p in access['permissions'])
        planner_user = next(u for u in access['users'] if u['username'] == 'planner')
        planner_role = next(r for r in access['roles'] if r['code'] == 'planner')
        assert 'work.write' in planner_role['permissions']

        wrong = client.post(f"/api/admin/users/{planner_user['id']}/permission-overrides", headers=admin, json={
            'permission_code': 'work.write', 'effect': 'Deny', 'reason': 'Regression test temporary deny override.',
            'current_password': 'wrong-password', 'confirmation': 'UPDATE ACCESS'
        })
        assert wrong.status_code == 401
        bad_confirmation = client.post(f"/api/admin/users/{planner_user['id']}/permission-overrides", headers=admin, json={
            'permission_code': 'work.write', 'effect': 'Deny', 'reason': 'Regression test temporary deny override.',
            'current_password': 'EUAS@2026', 'confirmation': 'WRONG'
        })
        assert bad_confirmation.status_code == 400

        deny = client.post(f"/api/admin/users/{planner_user['id']}/permission-overrides", headers=admin, json={
            'permission_code': 'work.write', 'effect': 'Deny', 'reason': 'Regression test temporary deny override.',
            'current_password': 'EUAS@2026', 'confirmation': 'UPDATE ACCESS'
        })
        assert deny.status_code == 200, deny.text

        blocked = client.post('/api/work-orders', headers=planner, json={'title': 'V4.6 denied work creation'})
        assert blocked.status_code == 403
        assert 'work.write' in blocked.text
        mine = client.get('/api/auth/me/permissions', headers=planner)
        assert mine.status_code == 200
        work_perm = next(p for p in mine.json()['permissions'] if p['code'] == 'work.write')
        assert work_perm['allowed'] is False and work_perm['source'] == 'user_deny'

        metrics = client.get('/api/metrics', headers=admin).text
        assert 'euas_active_permission_overrides 1' in metrics
        assert 'euas_active_permission_denies 1' in metrics
        export = client.get('/api/exports/access-control.csv', headers=admin)
        assert export.status_code == 200 and 'planner' in export.text and 'work.write' in export.text and 'Deny' in export.text

        inherit = client.post(f"/api/admin/users/{planner_user['id']}/permission-overrides", headers=admin, json={
            'permission_code': 'work.write', 'effect': 'Inherit', 'reason': 'Regression test restore role inheritance.',
            'current_password': 'EUAS@2026', 'confirmation': 'UPDATE ACCESS'
        })
        assert inherit.status_code == 200, inherit.text
        allowed = client.post('/api/work-orders', headers=planner, json={'title': 'V4.6 inherited work creation'})
        assert allowed.status_code == 200, allowed.text
        mine = client.get('/api/auth/me/permissions', headers=planner).json()
        work_perm = next(p for p in mine['permissions'] if p['code'] == 'work.write')
        assert work_perm['allowed'] is True and work_perm['source'] == 'role_grant'


def test_v46_dynamic_role_grant_persists_and_lockout_guards_hold():
    with TestClient(app) as client:
        admin = auth(client)
        executive = auth(client, 'exec', 'Viewer@2026')
        access = client.get('/api/admin/access-control', headers=admin).json()
        executive_role = next(r for r in access['roles'] if r['code'] == 'executive')
        original = list(executive_role['permissions'])

        before = client.post('/api/work-orders', headers=executive, json={'title': 'V4.6 executive blocked before grant'})
        assert before.status_code == 403

        wrong = client.put('/api/admin/roles/executive/permissions', headers=admin, json={
            'permissions': original + ['work.write'], 'current_password': 'wrong-password',
            'reason': 'Regression test delegated planning capability.', 'confirmation': 'UPDATE ACCESS'
        })
        assert wrong.status_code == 401
        grant = client.put('/api/admin/roles/executive/permissions', headers=admin, json={
            'permissions': original + ['work.write'], 'current_password': 'EUAS@2026',
            'reason': 'Regression test delegated planning capability.', 'confirmation': 'UPDATE ACCESS'
        })
        assert grant.status_code == 200, grant.text
        after = client.post('/api/work-orders', headers=executive, json={'title': 'V4.6 executive allowed by dynamic grant'})
        assert after.status_code == 200, after.text

        restore = client.put('/api/admin/roles/executive/permissions', headers=admin, json={
            'permissions': original, 'current_password': 'EUAS@2026',
            'reason': 'Regression test restore executive baseline grants.', 'confirmation': 'UPDATE ACCESS'
        })
        assert restore.status_code == 200, restore.text
        # Re-running database initialization must not silently re-add a grant removed by an administrator.
        init_db(hash_password)
        after_restart = client.get('/api/admin/roles/executive/permissions', headers=admin).json()
        assert 'work.write' not in after_restart['permissions']
        blocked_again = client.post('/api/work-orders', headers=executive, json={'title': 'V4.6 executive blocked after restore'})
        assert blocked_again.status_code == 403

        admin_role = next(r for r in access['roles'] if r['code'] == 'admin')
        without_core = [p for p in admin_role['permissions'] if p != 'admin.permissions.manage']
        lockout = client.put('/api/admin/roles/admin/permissions', headers=admin, json={
            'permissions': without_core, 'current_password': 'EUAS@2026',
            'reason': 'Regression test must reject administrator lockout.', 'confirmation': 'UPDATE ACCESS'
        })
        assert lockout.status_code == 409

        admin_user = next(u for u in access['users'] if u['username'] == 'omar')
        core_deny = client.post(f"/api/admin/users/{admin_user['id']}/permission-overrides", headers=admin, json={
            'permission_code': 'admin.permissions.manage', 'effect': 'Deny',
            'reason': 'Regression test must reject core administrator deny.',
            'current_password': 'EUAS@2026', 'confirmation': 'UPDATE ACCESS'
        })
        assert core_deny.status_code == 409
        last_admin = client.patch(f"/api/admin/users/{admin_user['id']}/role", headers=admin, json={
            'role_code': 'executive', 'reason': 'Regression test must preserve one active administrator.',
            'current_password': 'EUAS@2026', 'confirmation': 'UPDATE ACCESS'
        })
        assert last_admin.status_code == 409

        ui = Path(__file__).resolve().parents[1] / 'static' / 'app.js'
        ui_text = ui.read_text(encoding='utf-8')
        assert 'Role Permission Matrix' in ui_text and 'manageUserPermissions' in ui_text and '/api/admin/access-control' in ui_text
