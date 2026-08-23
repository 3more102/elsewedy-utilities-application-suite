from app.auth import hash_password
from apps.contracts import ContractInvalid, create_contract
from core.database import db, init_db


def _admin_id(conn):
    return int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()['id'])


def test_contract_rejects_inverted_validity_window_and_accepts_valid_metadata():
    init_db(hash_password)
    with db() as conn:
        actor_id = _admin_id(conn)
        supplier_id = conn.execute('SELECT id FROM vendors ORDER BY id LIMIT 1').fetchone()['id']
        try:
            create_contract(conn, {
                'title': 'Invalid window', 'vendor_id': supplier_id,
                'start_date': '2026-09-01', 'end_date': '2026-08-01', 'value': 100,
            }, actor_id)
        except ContractInvalid:
            pass
        else:
            raise AssertionError('inverted contract validity window must be rejected')
        valid = create_contract(conn, {
            'title': 'Valid metadata contract', 'vendor_id': supplier_id,
            'start_date': '2026-08-01', 'end_date': '2026-09-01', 'value': 100,
        }, actor_id)
        row = conn.execute('SELECT * FROM contracts WHERE id=?', (valid['id'],)).fetchone()
        assert row['start_date'] == '2026-08-01' and row['end_date'] == '2026-09-01'
