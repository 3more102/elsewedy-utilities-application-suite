from __future__ import annotations

from apps.audit import audit
from core.shared import next_no


class SupplierError(RuntimeError):
    status_code = 409


class SupplierNotFound(SupplierError):
    status_code = 404


class SupplierUnavailable(SupplierError):
    status_code = 409


def create_supplier(conn, data: dict, actor_id: int) -> dict:
    payload = dict(data)
    code = payload.get('vendor_code') or next_no(conn, 'vendors', 'vendor_code', 'VND-', 100)
    cur = conn.execute(
        '''INSERT INTO vendors(vendor_code,name,category,contact_person,email,phone,status)
           VALUES(?,?,?,?,?,?,?)''',
        (
            code, payload['name'], payload['category'], payload.get('contact_person', ''),
            payload.get('email', ''), payload.get('phone', ''), payload.get('status', 'Active'),
        ),
    )
    audit(conn, actor_id, 'CREATE', 'Vendors', code, '', payload)
    return {'id': cur.lastrowid, 'vendor_code': code}


def supplier_for_procurement(conn, supplier_id: int) -> dict:
    row = conn.execute('SELECT * FROM vendors WHERE id=?', (supplier_id,)).fetchone()
    if not row:
        raise SupplierNotFound('Supplier not found')
    supplier = dict(row)
    if supplier.get('status') != 'Active':
        raise SupplierUnavailable(f"Supplier {supplier['vendor_code']} is {supplier.get('status') or 'Unavailable'}")
    return supplier
