from __future__ import annotations

from datetime import date

from apps.audit import audit
from core.shared import next_no


class ContractError(RuntimeError):
    status_code = 409


class ContractInvalid(ContractError):
    status_code = 400


def _parse_day(value, field: str):
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ContractInvalid(f'{field} must be an ISO date') from exc


def create_contract(conn, data: dict, actor_id: int) -> dict:
    payload = dict(data)
    start = _parse_day(payload.get('start_date'), 'start_date')
    end = _parse_day(payload.get('end_date'), 'end_date')
    if start and end and end < start:
        raise ContractInvalid('Contract end_date cannot be before start_date')
    vendor_id = payload.get('vendor_id')
    if vendor_id and not conn.execute('SELECT id FROM vendors WHERE id=?', (vendor_id,)).fetchone():
        raise ContractInvalid('Supplier not found')
    number = payload.get('contract_no') or next_no(conn, 'contracts', 'contract_no', 'CTR-', 4001)
    cur = conn.execute(
        'INSERT INTO contracts(contract_no,title,vendor_id,start_date,end_date,value,status) VALUES(?,?,?,?,?,?,?)',
        (
            number, payload['title'], vendor_id, payload.get('start_date'), payload.get('end_date'),
            payload.get('value', 0), payload.get('status', 'Active'),
        ),
    )
    audit(conn, actor_id, 'CREATE', 'Contracts', number, '', payload)
    return {'id': cur.lastrowid, 'contract_no': number}
