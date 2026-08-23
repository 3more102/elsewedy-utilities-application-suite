from __future__ import annotations

from core.database import now
from core.shared import next_no


def post_cost(conn, work_order: dict, cost_type: str, amount: float, quantity: float, reference: str, user_id: int):
    """Append one maintenance cost-ledger entry."""
    if amount <= 0:
        return None
    number = next_no(conn, 'maintenance_cost_ledger', 'entry_no', 'COST-', 1)
    cur = conn.execute(
        '''INSERT INTO maintenance_cost_ledger(
             entry_no,work_order_id,asset_id,cost_type,amount,quantity,reference,posted_by,posted_at
           ) VALUES(?,?,?,?,?,?,?,?,?)''',
        (
            number, work_order['id'], work_order.get('asset_id'), cost_type, amount, quantity,
            reference or work_order['wo_no'], user_id, now(),
        ),
    )
    return {'id': cur.lastrowid, 'entry_no': number, 'amount': amount}
