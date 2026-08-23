from __future__ import annotations

from datetime import date, timedelta

RCM_CONSEQUENCES = ('Safety', 'Environmental', 'Operational', 'Non-Operational', 'Hidden')
RCM_STRATEGY_TYPES = ('Condition-Based', 'Time-Based', 'Run-to-Failure', 'Failure-Finding', 'Redesign')


class RcmError(RuntimeError):
    status_code = 422


class RcmNotFound(RcmError):
    status_code = 404


class RcmConflict(RcmError):
    status_code = 409


def review_days(risk_band: str) -> int:
    return {'Critical': 90, 'High': 180, 'Medium': 365, 'Low': 730}.get(str(risk_band), 365)


def default_review_due(fmea: dict) -> str:
    return (date.today() + timedelta(days=review_days(fmea.get('risk_band') or 'Medium'))).isoformat()


def get_strategy(conn, strategy_id: int) -> dict:
    row = conn.execute(
        '''SELECT r.*,f.fmea_no,f.asset_id,f.rpn,f.risk_band,f.status fmea_status,a.asset_no,a.name asset_name,
          fm.mode_no,fm.name failure_mode_name,ou.full_name owner_name,ap.full_name approved_by_name,ac.full_name activated_by_name,
          cb.rule_no linked_cbm_rule_no,cb.name linked_cbm_rule_name,pm.pm_no linked_pm_no,pm.name linked_pm_name
          FROM rcm_strategies r JOIN asset_fmea f ON f.id=r.asset_fmea_id JOIN assets a ON a.id=f.asset_id
          JOIN failure_modes fm ON fm.id=f.failure_mode_id LEFT JOIN users ou ON ou.id=r.owner_id LEFT JOIN users ap ON ap.id=r.approved_by
          LEFT JOIN users ac ON ac.id=r.activated_by LEFT JOIN cbm_rules cb ON cb.id=r.linked_cbm_rule_id
          LEFT JOIN maintenance_plans pm ON pm.id=r.linked_pm_plan_id WHERE r.id=?''',
        (strategy_id,),
    ).fetchone()
    if not row:
        raise RcmNotFound('RCM strategy not found')
    return dict(row)


def validate_payload(conn, fmea: dict, data: dict, require_ready: bool = False) -> bool:
    consequence = str(data.get('consequence_classification') or '')
    strategy = str(data.get('strategy_type') or '')
    if consequence not in RCM_CONSEQUENCES:
        raise RcmError('Invalid RCM consequence classification')
    if strategy not in RCM_STRATEGY_TYPES:
        raise RcmError('Invalid RCM maintenance strategy type')
    if fmea.get('status') == 'Retired':
        raise RcmConflict('Retired FMEA records cannot have new or submitted RCM strategies')
    interval = data.get('interval_days')
    if strategy in ('Time-Based', 'Failure-Finding') and not interval:
        raise RcmError(f'{strategy} strategies require interval_days')
    if interval is not None and (int(interval) < 1 or int(interval) > 3650):
        raise RcmError('RCM interval_days must be between 1 and 3650')
    if strategy == 'Run-to-Failure' and consequence in ('Safety', 'Environmental'):
        raise RcmError('Run-to-Failure is not permitted for Safety or Environmental consequence classifications')
    cbm_id = data.get('linked_cbm_rule_id')
    if cbm_id:
        row = conn.execute('SELECT id,asset_fmea_id,active FROM cbm_rules WHERE id=?', (cbm_id,)).fetchone()
        if not row:
            raise RcmNotFound('Linked CBM rule not found')
        cbm = dict(row)
        if int(cbm.get('asset_fmea_id') or 0) != int(fmea['id']):
            raise RcmError('Linked CBM rule must reference the same FMEA record')
        if require_ready and not cbm.get('active'):
            raise RcmConflict('Linked CBM rule must be active before RCM submission or activation')
    if require_ready and strategy == 'Condition-Based' and not cbm_id:
        raise RcmError('Condition-Based RCM strategies require a linked active CBM rule before submission')
    pm_id = data.get('linked_pm_plan_id')
    if pm_id:
        row = conn.execute('SELECT id,asset_id,active FROM maintenance_plans WHERE id=?', (pm_id,)).fetchone()
        if not row:
            raise RcmNotFound('Linked maintenance plan not found')
        pm = dict(row)
        if int(pm['asset_id']) != int(fmea['asset_id']):
            raise RcmError('Linked maintenance plan must belong to the same asset')
        if require_ready and not pm.get('active'):
            raise RcmConflict('Linked maintenance plan must be active before RCM submission or activation')
    if require_ready and strategy == 'Time-Based' and not pm_id:
        raise RcmError('Time-Based RCM strategies require a linked active maintenance plan before submission')
    return True
