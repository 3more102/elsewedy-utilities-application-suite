from __future__ import annotations


class FmeaError(RuntimeError):
    status_code = 422


class FmeaNotFound(FmeaError):
    status_code = 404


class FmeaConflict(FmeaError):
    status_code = 409


def calculate_risk(severity: int, occurrence: int, detectability: int) -> tuple[int, str]:
    values = (int(severity), int(occurrence), int(detectability))
    if any(value < 1 or value > 10 for value in values):
        raise FmeaError('FMEA severity, occurrence and detectability must be between 1 and 10')
    rpn = values[0] * values[1] * values[2]
    band = 'Critical' if rpn >= 300 else 'High' if rpn >= 160 else 'Medium' if rpn >= 80 else 'Low'
    return rpn, band


def get_record(conn, asset_fmea_id: int, expected_asset_id: int | None = None, active_required: bool = True) -> dict:
    row = conn.execute(
        '''SELECT f.*,a.asset_no,a.name asset_name,fm.mode_no,fm.name failure_mode_name,fm.category failure_mode_category
           FROM asset_fmea f JOIN assets a ON a.id=f.asset_id JOIN failure_modes fm ON fm.id=f.failure_mode_id
           WHERE f.id=?''',
        (asset_fmea_id,),
    ).fetchone()
    if not row:
        raise FmeaNotFound('Asset FMEA record not found')
    record = dict(row)
    if expected_asset_id is not None and int(record['asset_id']) != int(expected_asset_id):
        raise FmeaError('FMEA record must belong to the same asset')
    if active_required and record['status'] == 'Retired':
        raise FmeaConflict('Retired FMEA records cannot be linked to new work or CBM rules')
    return record


def would_create_failure_mode_cycle(conn, mode_id: int, parent_id: int | None) -> bool:
    if parent_id is None:
        return False
    if int(parent_id) == int(mode_id):
        return True
    seen = set()
    current = parent_id
    while current is not None and current not in seen:
        if int(current) == int(mode_id):
            return True
        seen.add(current)
        row = conn.execute('SELECT parent_id FROM failure_modes WHERE id=?', (current,)).fetchone()
        current = row['parent_id'] if row else None
    return False
