from app.auth import hash_password
from apps.hse import HseConflict, create_incident, update_incident
from core.database import db, init_db


def _admin_id(conn) -> int:
    return int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()['id'])


def test_closed_hse_incident_is_terminal_and_preserves_first_closure():
    init_db(hash_password)
    with db() as conn:
        actor_id = _admin_id(conn)
        incident = create_incident(conn, {
            'incident_type': 'Near Miss', 'title': 'Terminal HSE regression', 'severity': 2, 'probability': 2,
            'description': 'test', 'corrective_action': '',
        }, actor_id)
        closed = update_incident(conn, incident['id'], {'status': 'Closed', 'corrective_action': 'Barrier installed'}, actor_id)
        assert closed['status'] == 'Closed'
        try:
            update_incident(conn, incident['id'], {'status': 'Investigating'}, actor_id)
        except HseConflict:
            pass
        else:
            raise AssertionError('closed HSE incident must not be reopened implicitly')
        current = conn.execute('SELECT status,corrective_action FROM safety_incidents WHERE id=?', (incident['id'],)).fetchone()
        assert current['status'] == 'Closed' and current['corrective_action'] == 'Barrier installed'
