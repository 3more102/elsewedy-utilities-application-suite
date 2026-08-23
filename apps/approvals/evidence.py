from __future__ import annotations

import hashlib
import json
from typing import Any

from core.correlation import correlation_id as new_correlation_id
from core.database import now
from core.shared import next_no


class ApprovalSnapshotError(ValueError):
    pass


def _row_dict(row) -> dict:
    if row is None:
        raise ApprovalSnapshotError('Approval target not found')
    return dict(row)


def target_snapshot(conn, record_type: str, record_id: int) -> dict:
    """Return the canonical persisted target state used for approval fingerprints."""
    if record_type == 'work_order':
        return _row_dict(conn.execute('SELECT * FROM work_orders WHERE id=?', (record_id,)).fetchone())
    if record_type == 'purchase_requisition':
        return _row_dict(conn.execute('SELECT * FROM purchase_requisitions WHERE id=?', (record_id,)).fetchone())
    if record_type == 'alarm_shelf':
        return _row_dict(conn.execute('SELECT * FROM alarm_shelves WHERE id=?', (record_id,)).fetchone())
    if record_type == 'rcm_strategy':
        return _row_dict(conn.execute('SELECT * FROM rcm_strategies WHERE id=?', (record_id,)).fetchone())
    return {'record_type': record_type, 'record_id': int(record_id)}


def stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, default=str)


def snapshot_hash(snapshot: dict) -> str:
    return hashlib.sha256(stable_json(snapshot).encode('utf-8')).hexdigest()


def capture_request_snapshot(conn, record_type: str, record_id: int) -> tuple[str, str, str]:
    snapshot = target_snapshot(conn, record_type, record_id)
    payload = stable_json(snapshot)
    digest = hashlib.sha256(payload.encode('utf-8')).hexdigest()
    version = str(snapshot.get('updated_at') or snapshot.get('requested_at') or snapshot.get('id') or record_id)
    return payload, digest, version


def ensure_request_snapshot(conn, approval: dict) -> dict:
    """Backfill legacy pending approvals once, preserving upgrade compatibility."""
    current = dict(approval)
    if current.get('request_snapshot_hash') and current.get('request_snapshot_json'):
        return current
    payload, digest, version = capture_request_snapshot(conn, current['record_type'], current['record_id'])
    corr = current.get('correlation_id') or new_correlation_id()
    conn.execute(
        '''UPDATE approval_requests
           SET request_snapshot_json=?,request_snapshot_hash=?,request_resource_version=?,correlation_id=?
           WHERE id=? AND (request_snapshot_hash IS NULL OR request_snapshot_hash='')''',
        (payload, digest, version, corr, current['id']),
    )
    current.update(
        request_snapshot_json=payload,
        request_snapshot_hash=digest,
        request_resource_version=version,
        correlation_id=corr,
    )
    return current


def verify_request_snapshot(conn, approval: dict) -> dict:
    approval = ensure_request_snapshot(conn, approval)
    current_snapshot = target_snapshot(conn, approval['record_type'], approval['record_id'])
    current_hash = snapshot_hash(current_snapshot)
    expected_hash = approval.get('request_snapshot_hash') or ''
    return {
        'valid': bool(expected_hash) and current_hash == expected_hash,
        'expected_hash': expected_hash,
        'current_hash': current_hash,
        'snapshot': current_snapshot,
        'resource_version': approval.get('request_resource_version') or '',
    }



def expected_intent(decision: str, record_code: str) -> str:
    action = 'approve' if decision.lower().strip() == 'approve' else 'reject'
    return f'I {action} {record_code}'


def decision_snapshot(conn, approval: dict) -> dict:
    # Preserve the richer post-decision evidence payload used by the existing API.
    record_type = approval['record_type']
    record_id = int(approval['record_id'])
    if record_type == 'alarm_shelf':
        row = conn.execute(
            """SELECT sh.*,oa.alarm_no,oa.status alarm_status,oa.severity
               FROM alarm_shelves sh JOIN operational_alarms oa ON oa.id=sh.alarm_id WHERE sh.id=?""",
            (record_id,),
        ).fetchone()
        return _row_dict(row)
    if record_type == 'rcm_strategy':
        row = conn.execute(
            """SELECT r.*,f.fmea_no,f.asset_id,f.rpn,f.risk_band,f.status fmea_status,a.asset_no,a.name asset_name,
              fm.mode_no,fm.name failure_mode_name,ou.full_name owner_name,ap.full_name approved_by_name,ac.full_name activated_by_name,
              cb.rule_no linked_cbm_rule_no,cb.name linked_cbm_rule_name,pm.pm_no linked_pm_no,pm.name linked_pm_name
              FROM rcm_strategies r JOIN asset_fmea f ON f.id=r.asset_fmea_id JOIN assets a ON a.id=f.asset_id
              JOIN failure_modes fm ON fm.id=f.failure_mode_id LEFT JOIN users ou ON ou.id=r.owner_id LEFT JOIN users ap ON ap.id=r.approved_by
              LEFT JOIN users ac ON ac.id=r.activated_by LEFT JOIN cbm_rules cb ON cb.id=r.linked_cbm_rule_id
              LEFT JOIN maintenance_plans pm ON pm.id=r.linked_pm_plan_id WHERE r.id=?""",
            (record_id,),
        ).fetchone()
        return _row_dict(row)
    return target_snapshot(conn, record_type, record_id)

def signature_digest(prev_hash: str, payload_json: str) -> str:
    return hashlib.sha256(f'{prev_hash or ""}|{payload_json}'.encode('utf-8')).hexdigest()


def record_signature(
    conn,
    approval: dict,
    target_status: str,
    user: dict,
    intent_statement: str,
    comments: str,
    delegated: bool,
    record_snapshot: dict,
    delegation_id: int | None = None,
) -> dict:
    evidence_no = next_no(conn, 'approval_signature_evidence', 'evidence_no', 'SIG-', 9001)
    signed_at = now()
    payload = {
        'schema': 2,
        'evidence_no': evidence_no,
        'approval': {
            'id': approval['id'], 'approval_no': approval['approval_no'], 'module': approval['module'],
            'record_type': approval['record_type'], 'record_id': approval['record_id'],
            'record_code': approval['record_code'], 'title': approval['title'],
            'requested_by': approval['requested_by'], 'requested_at': approval['requested_at'],
            'request_snapshot_hash': approval.get('request_snapshot_hash') or '',
            'correlation_id': approval.get('correlation_id') or '',
        },
        'decision': target_status,
        'signer': {
            'user_id': user['id'], 'username': user['username'],
            'full_name': user['full_name'], 'role': user['role'],
        },
        'authority': {'delegated': bool(delegated), 'delegation_id': delegation_id},
        'credential_verified': True,
        'intent_statement': intent_statement,
        'comments': comments or '',
        'signed_at': signed_at,
        'record_snapshot': record_snapshot,
    }
    payload_json = stable_json(payload)
    previous = conn.execute('SELECT evidence_hash FROM approval_signature_evidence ORDER BY id DESC LIMIT 1').fetchone()
    prev_hash = previous['evidence_hash'] if previous and previous['evidence_hash'] else ''
    digest = signature_digest(prev_hash, payload_json)
    cur = conn.execute(
        '''INSERT INTO approval_signature_evidence(
          evidence_no,approval_id,approval_no,module,record_type,record_id,record_code,decision,signer_user_id,signer_username,signer_name,signer_role,
          delegated_authority,credential_verified,intent_statement,comments,signed_at,payload_json,prev_hash,evidence_hash
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            evidence_no, approval['id'], approval['approval_no'], approval['module'], approval['record_type'],
            approval['record_id'], approval['record_code'], target_status, user['id'], user['username'],
            user['full_name'], user['role'], int(bool(delegated)), 1, intent_statement, comments or '',
            signed_at, payload_json, prev_hash, digest,
        ),
    )
    return {'id': cur.lastrowid, 'evidence_no': evidence_no, 'signed_at': signed_at, 'evidence_hash': digest, 'prev_hash': prev_hash}


def verify_signature_chain(conn) -> dict:
    prev = ''
    checked = 0
    for row in conn.execute('SELECT * FROM approval_signature_evidence ORDER BY id').fetchall():
        r = dict(row)
        checked += 1
        payload_json = r['payload_json'] or ''
        expected = signature_digest(prev, payload_json)
        try:
            payload = json.loads(payload_json)
        except Exception:
            return {'valid': False, 'checked': checked, 'first_invalid_id': r['id'], 'first_invalid_evidence_no': r['evidence_no'], 'reason': 'invalid_payload_json', 'head_hash': prev}
        approval = payload.get('approval') or {}
        signer = payload.get('signer') or {}
        authority = payload.get('authority') or {}
        columns_match = (
            payload.get('evidence_no') == r['evidence_no'] and approval.get('approval_no') == r['approval_no'] and
            approval.get('module') == r['module'] and approval.get('record_type') == r['record_type'] and
            int(approval.get('record_id', -1)) == int(r['record_id']) and approval.get('record_code') == r['record_code'] and
            payload.get('decision') == r['decision'] and int(signer.get('user_id', -1)) == int(r['signer_user_id']) and
            signer.get('username') == r['signer_username'] and signer.get('full_name') == r['signer_name'] and
            signer.get('role') == r['signer_role'] and int(bool(authority.get('delegated'))) == int(r['delegated_authority']) and
            bool(payload.get('credential_verified')) == bool(r['credential_verified']) and
            payload.get('intent_statement') == r['intent_statement'] and (payload.get('comments') or '') == (r['comments'] or '') and
            payload.get('signed_at') == r['signed_at']
        )
        if (r['prev_hash'] or '') != prev or (r['evidence_hash'] or '') != expected or not columns_match:
            reason = 'chain_link' if (r['prev_hash'] or '') != prev else ('hash_mismatch' if (r['evidence_hash'] or '') != expected else 'column_payload_mismatch')
            return {'valid': False, 'checked': checked, 'first_invalid_id': r['id'], 'first_invalid_evidence_no': r['evidence_no'], 'reason': reason, 'head_hash': prev}
        prev = r['evidence_hash']
    return {'valid': True, 'checked': checked, 'first_invalid_id': None, 'first_invalid_evidence_no': None, 'reason': 'ok', 'head_hash': prev}


def lifecycle_digest(prev_hash: str, payload_json: str) -> str:
    return hashlib.sha256(f'{prev_hash or ""}|{payload_json}'.encode('utf-8')).hexdigest()


def append_evidence_event(
    conn,
    event_type: str,
    actor_user_id: int,
    *,
    approval_id: int | None = None,
    delegation_id: int | None = None,
    effective_actor_user_id: int | None = None,
    decision: str = '',
    resource_type: str = '',
    resource_id: int | None = None,
    resource_fingerprint: str = '',
    correlation_id: str = '',
    details: dict | None = None,
) -> dict:
    evidence_no = next_no(conn, 'approval_evidence_events', 'evidence_no', 'APE-', 1)
    created_at = now()
    corr = correlation_id or new_correlation_id()
    payload = {
        'schema': 1,
        'evidence_no': evidence_no,
        'event_type': event_type,
        'approval_id': approval_id,
        'delegation_id': delegation_id,
        'actor_user_id': actor_user_id,
        'effective_actor_user_id': effective_actor_user_id or actor_user_id,
        'decision': decision or '',
        'resource_type': resource_type or '',
        'resource_id': resource_id,
        'resource_fingerprint': resource_fingerprint or '',
        'correlation_id': corr,
        'created_at': created_at,
        'details': details or {},
    }
    payload_json = stable_json(payload)
    previous = conn.execute('SELECT evidence_hash FROM approval_evidence_events ORDER BY id DESC LIMIT 1').fetchone()
    prev_hash = previous['evidence_hash'] if previous and previous['evidence_hash'] else ''
    digest = lifecycle_digest(prev_hash, payload_json)
    cur = conn.execute(
        '''INSERT INTO approval_evidence_events(
          evidence_no,approval_id,delegation_id,event_type,actor_user_id,effective_actor_user_id,decision,resource_type,resource_id,
          resource_fingerprint,correlation_id,payload_json,prev_hash,evidence_hash,created_at
        ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            evidence_no, approval_id, delegation_id, event_type, actor_user_id, effective_actor_user_id or actor_user_id,
            decision or '', resource_type or '', resource_id, resource_fingerprint or '', corr, payload_json, prev_hash, digest, created_at,
        ),
    )
    return {'id': cur.lastrowid, 'evidence_no': evidence_no, 'evidence_hash': digest, 'prev_hash': prev_hash, 'correlation_id': corr}


def verify_evidence_chain(conn) -> dict:
    prev = ''
    checked = 0
    for row in conn.execute('SELECT * FROM approval_evidence_events ORDER BY id').fetchall():
        r = dict(row)
        checked += 1
        payload_json = r['payload_json'] or ''
        expected = lifecycle_digest(prev, payload_json)
        try:
            payload = json.loads(payload_json)
        except Exception:
            return {'valid': False, 'checked': checked, 'first_invalid_id': r['id'], 'reason': 'invalid_payload_json', 'head_hash': prev}
        columns_match = (
            payload.get('evidence_no') == r['evidence_no'] and payload.get('event_type') == r['event_type'] and
            payload.get('approval_id') == r['approval_id'] and payload.get('delegation_id') == r['delegation_id'] and
            int(payload.get('actor_user_id', -1)) == int(r['actor_user_id']) and
            int(payload.get('effective_actor_user_id', -1)) == int(r['effective_actor_user_id']) and
            (payload.get('decision') or '') == (r['decision'] or '') and
            (payload.get('resource_type') or '') == (r['resource_type'] or '') and
            payload.get('resource_id') == r['resource_id'] and
            (payload.get('resource_fingerprint') or '') == (r['resource_fingerprint'] or '') and
            payload.get('correlation_id') == r['correlation_id'] and payload.get('created_at') == r['created_at']
        )
        if (r['prev_hash'] or '') != prev or (r['evidence_hash'] or '') != expected or not columns_match:
            reason = 'chain_link' if (r['prev_hash'] or '') != prev else ('hash_mismatch' if (r['evidence_hash'] or '') != expected else 'column_payload_mismatch')
            return {'valid': False, 'checked': checked, 'first_invalid_id': r['id'], 'reason': reason, 'head_hash': prev}
        prev = r['evidence_hash']
    return {'valid': True, 'checked': checked, 'first_invalid_id': None, 'reason': 'ok', 'head_hash': prev}


def decision_history(conn, approval_id: int) -> list[dict]:
    result = []
    for row in conn.execute('SELECT * FROM approval_evidence_events WHERE approval_id=? ORDER BY id', (approval_id,)).fetchall():
        item = dict(row)
        item['payload'] = json.loads(item.pop('payload_json'))
        result.append(item)
    return result
