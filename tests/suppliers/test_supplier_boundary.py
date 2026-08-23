from app.auth import hash_password
from apps.procurement import ProcurementConflict, approve_requisition, create_purchase_order, create_requisition, submit_requisition
from apps.suppliers import SupplierUnavailable, create_supplier, supplier_for_procurement
from core.database import db, init_db


def _admin_id(conn):
    return int(conn.execute("SELECT id FROM users WHERE username='omar'").fetchone()['id'])


def test_inactive_supplier_is_rejected_for_purchase_order():
    init_db(hash_password)
    with db() as conn:
        actor_id = _admin_id(conn)
        supplier = create_supplier(conn, {'name': 'Inactive supplier regression', 'category': 'Test', 'status': 'Inactive'}, actor_id)
        try:
            supplier_for_procurement(conn, supplier['id'])
        except SupplierUnavailable:
            pass
        else:
            raise AssertionError('inactive supplier must not be procurement-eligible')
        item = conn.execute('SELECT * FROM inventory_items ORDER BY id LIMIT 1').fetchone()
        pr = create_requisition(conn, {
            'title': 'Supplier status regression',
            'items': [{'inventory_item_id': item['id'], 'description': item['name'], 'quantity': 1, 'estimated_unit_cost': item['unit_price']}],
        }, actor_id)
        submit_requisition(conn, pr['id'], actor_id)
        approve_requisition(conn, pr['id'], actor_id)
        try:
            create_purchase_order(conn, {'pr_id': pr['id'], 'vendor_id': supplier['id']}, actor_id)
        except ProcurementConflict as exc:
            assert 'Inactive' in str(exc)
        else:
            raise AssertionError('purchase order must reject inactive supplier')
        assert conn.execute('SELECT COUNT(*) FROM purchase_orders WHERE pr_id=?', (pr['id'],)).fetchone()[0] == 0
        assert conn.execute('SELECT status FROM purchase_requisitions WHERE id=?', (pr['id'],)).fetchone()['status'] == 'Approved'
