from __future__ import annotations

from apps.audit import audit
from apps.events import emit_event
from apps.notifications import notify
from core.configuration import DB_BACKEND
from core.database import now
from core.shared import next_no

from .risk import is_high_risk, risk_score, validate_hse_status, validate_hse_transition


class HseCommandError(RuntimeError):
    status_code = 409


class HseNotFound(HseCommandError):
    status_code = 404


class HseInvalid(HseCommandError):
    status_code = 400


class HseConflict(HseCommandError):
    status_code = 409


def _begin_write(conn) -> None:
    if DB_BACKEND == 'sqlite' and not getattr(conn, 'in_transaction', False):
        conn.execute('BEGIN IMMEDIATE')


def _locked_incident(conn, incident_id: int) -> dict:
    _begin_write(conn)
    suffix = ' FOR UPDATE' if DB_BACKEND == 'postgresql' else ''
    row = conn.execute(f'SELECT * FROM safety_incidents WHERE id=?{suffix}', (incident_id,)).fetchone()
    if not row:
        raise HseNotFound('HSE record not found')
    return dict(row)


def create_incident(conn, data: dict, actor_id: int) -> dict:
    payload = dict(data)
    score = risk_score(payload['severity'], payload['probability'])
    number = next_no(conn, 'safety_incidents', 'incident_no', 'HSE-', 7001)
    cur = conn.execute(
        '''INSERT INTO safety_incidents(
             incident_no,incident_type,title,site_id,location_id,asset_id,reported_by,severity,probability,risk_score,
             status,description,corrective_action,occurred_at,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?,'Open',?,?,?,?)''',
        (
            number, payload['incident_type'], payload['title'], payload.get('site_id'), payload.get('location_id'),
            payload.get('asset_id'), actor_id, payload['severity'], payload['probability'], score,
            payload['description'], payload.get('corrective_action', ''), payload.get('occurred_at') or now(), now(),
        ),
    )
    audit(conn, actor_id, 'CREATE', 'HSE', number, '', payload)
    emit_event(conn, 'hse.incident.created', 'safety_incident', number, {'incident_id': cur.lastrowid, 'risk_score': score})
    if is_high_risk(score):
        notify(conn, 'High HSE risk', f'{number} has risk score {score}', 'Critical', None, 'maintenance_manager', 'hse', number)
    return {'id': cur.lastrowid, 'incident_no': number, 'risk_score': score}


def update_incident(conn, incident_id: int, changes: dict, actor_id: int) -> dict:
    incident = _locked_incident(conn, incident_id)
    changes = {key: value for key, value in dict(changes).items() if value is not None}
    if not changes:
        return incident
    if 'status' in changes:
        if not validate_hse_status(changes['status']):
            raise HseInvalid('Invalid HSE status')
        if not validate_hse_transition(incident['status'], changes['status']):
            raise HseConflict(f"HSE status cannot transition from {incident['status']} to {changes['status']}")
    severity = int(changes.get('severity', incident['severity']))
    probability = int(changes.get('probability', incident['probability']))
    if not 1 <= severity <= 5 or not 1 <= probability <= 5:
        raise HseInvalid('HSE severity and probability must be between 1 and 5')
    if 'severity' in changes or 'probability' in changes:
        changes['risk_score'] = risk_score(severity, probability)
    sets = ','.join(f'{key}=?' for key in changes)
    conn.execute(f'UPDATE safety_incidents SET {sets} WHERE id=?', (*changes.values(), incident_id))
    audit(conn, actor_id, 'UPDATE', 'HSE', incident['incident_no'], incident, changes)
    new_score = changes.get('risk_score', incident['risk_score'])
    if is_high_risk(new_score) and not is_high_risk(incident['risk_score']):
        notify(
            conn, 'High HSE risk', f"{incident['incident_no']} escalated to risk score {new_score}",
            'Critical', None, 'maintenance_manager', 'hse', incident['incident_no'],
        )
        emit_event(
            conn, 'hse.risk.escalated', 'safety_incident', incident['incident_no'],
            {'incident_id': incident_id, 'previous_risk_score': incident['risk_score'], 'risk_score': new_score},
        )
    if changes.get('status') == 'Closed' and incident['status'] != 'Closed':
        emit_event(conn, 'hse.incident.closed', 'safety_incident', incident['incident_no'], {'incident_id': incident_id})
    row = conn.execute('SELECT * FROM safety_incidents WHERE id=?', (incident_id,)).fetchone()
    return dict(row)
