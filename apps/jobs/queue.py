from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta

from core.configuration import DB_BACKEND
from core.correlation import correlation_id as make_correlation_id
from core.database import now

JOB_STATES = (
    'Pending', 'Leased', 'Running', 'RetryScheduled', 'Succeeded', 'Failed', 'DeadLetter', 'Cancelled'
)
CLAIMABLE_STATES = ('Pending', 'RetryScheduled')
ACTIVE_STATES = ('Leased', 'Running')
TERMINAL_STATES = ('Succeeded', 'DeadLetter', 'Cancelled')


class JobNotFound(LookupError):
    pass


class JobStateError(RuntimeError):
    pass


class JobLeaseError(RuntimeError):
    pass


def _row(cursor):
    value = cursor.fetchone()
    return dict(value) if value else None


def _rows(cursor):
    return [dict(value) for value in cursor.fetchall()]


def _future(seconds: int) -> str:
    return (datetime.now() + timedelta(seconds=max(0, int(seconds)))).isoformat(timespec='seconds')


def _payload_json(payload) -> str:
    return json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, default=str)


def enqueue_job(
    conn,
    *,
    job_type: str,
    payload: dict | None = None,
    priority: int = 0,
    available_at: str | None = None,
    max_attempts: int = 5,
    correlation_id: str | None = None,
    deduplication_key: str | None = None,
) -> dict:
    job_type = str(job_type or '').strip()
    if not job_type:
        raise ValueError('job_type is required')
    if max_attempts < 1:
        raise ValueError('max_attempts must be at least 1')
    if deduplication_key:
        existing = _row(conn.execute('SELECT * FROM jobs WHERE deduplication_key=?', (deduplication_key,)))
        if existing:
            existing['idempotent_replay'] = True
            return existing
    stamp = now()
    external_id = 'JOB-' + uuid.uuid4().hex[:20].upper()
    corr = make_correlation_id(correlation_id)
    cur = conn.execute(
        """INSERT INTO jobs(job_id,job_type,payload_json,status,priority,created_at,available_at,attempt_count,max_attempts,last_error,correlation_id,deduplication_key,updated_at)
           VALUES(?,?,?,'Pending',?,?,?,?,?,'',?,?,?)""",
        (
            external_id,
            job_type,
            _payload_json(payload),
            int(priority),
            stamp,
            available_at or stamp,
            0,
            int(max_attempts),
            corr,
            deduplication_key,
            stamp,
        ),
    )
    record = _row(conn.execute('SELECT * FROM jobs WHERE id=?', (cur.lastrowid,)))
    record['idempotent_replay'] = False
    return record


def get_job(conn, job_id: int | str) -> dict:
    if isinstance(job_id, int) or (isinstance(job_id, str) and job_id.isdigit()):
        record = _row(conn.execute('SELECT * FROM jobs WHERE id=?', (int(job_id),)))
    else:
        record = _row(conn.execute('SELECT * FROM jobs WHERE job_id=?', (str(job_id),)))
    if not record:
        raise JobNotFound('Job not found')
    return record


def list_jobs(conn, *, status: str = '', job_type: str = '', limit: int = 100) -> list[dict]:
    sql = 'SELECT * FROM jobs WHERE 1=1'
    args: list[object] = []
    if status:
        sql += ' AND status=?'
        args.append(status)
    if job_type:
        sql += ' AND job_type=?'
        args.append(job_type)
    sql += ' ORDER BY priority DESC,available_at,id LIMIT ?'
    args.append(max(1, min(int(limit), 1000)))
    return _rows(conn.execute(sql, args))


def recover_expired_leases(conn, *, at: str | None = None) -> dict:
    stamp = at or now()
    expired = _rows(
        conn.execute(
            "SELECT * FROM jobs WHERE status IN ('Leased','Running') AND lease_expires_at IS NOT NULL AND lease_expires_at<=? ORDER BY id",
            (stamp,),
        )
    )
    retried = dead_lettered = 0
    for job in expired:
        terminal = int(job['attempt_count']) >= int(job['max_attempts'])
        new_status = 'DeadLetter' if terminal else 'RetryScheduled'
        available = stamp
        conn.execute(
            """UPDATE jobs SET status=?,available_at=?,lease_owner=NULL,lease_expires_at=NULL,last_error=?,finished_at=?,updated_at=?
               WHERE id=? AND status IN ('Leased','Running')""",
            (
                new_status,
                available,
                'Worker lease expired',
                stamp if terminal else None,
                stamp,
                job['id'],
            ),
        )
        conn.execute(
            """UPDATE job_attempts SET status='LeaseExpired',finished_at=?,error_message='Worker lease expired'
               WHERE job_id=? AND attempt_no=? AND status IN ('Leased','Running')""",
            (stamp, job['id'], job['current_attempt_no']),
        )
        conn.execute(
            "UPDATE job_leases SET released_at=?,release_reason='LeaseExpired' WHERE job_id=? AND worker_id=? AND released_at IS NULL",
            (stamp, job['id'], job.get('lease_owner')),
        )
        if terminal:
            dead_lettered += 1
        else:
            retried += 1
    return {'expired': len(expired), 'retry_scheduled': retried, 'dead_lettered': dead_lettered}


def claim_next_job(conn, *, worker_id: str, lease_seconds: int = 60) -> dict | None:
    stamp = now()
    recover_expired_leases(conn, at=stamp)
    expires = _future(lease_seconds)
    if DB_BACKEND == 'postgresql':
        candidate = _row(
            conn.execute(
                """SELECT * FROM jobs WHERE status IN ('Pending','RetryScheduled') AND available_at<=?
                   ORDER BY priority DESC,available_at,id LIMIT 1 FOR UPDATE SKIP LOCKED""",
                (stamp,),
            )
        )
        if not candidate:
            return None
        job_id = candidate['id']
        updated = conn.execute(
            """UPDATE jobs SET status='Leased',lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,current_attempt_no=current_attempt_no+1,started_at=COALESCE(started_at,?),updated_at=?
               WHERE id=? AND status IN ('Pending','RetryScheduled')""",
            (worker_id, expires, stamp, stamp, job_id),
        )
        if updated.rowcount != 1:
            return None
        claimed = get_job(conn, int(job_id))
    else:
        claimed = _row(
            conn.execute(
                """UPDATE jobs SET status='Leased',lease_owner=?,lease_expires_at=?,attempt_count=attempt_count+1,current_attempt_no=current_attempt_no+1,started_at=COALESCE(started_at,?),updated_at=?
                   WHERE id=(SELECT id FROM jobs WHERE status IN ('Pending','RetryScheduled') AND available_at<=? ORDER BY priority DESC,available_at,id LIMIT 1)
                   AND status IN ('Pending','RetryScheduled') RETURNING *""",
                (worker_id, expires, stamp, stamp, stamp),
            )
        )
        if not claimed:
            return None
    conn.execute(
        "INSERT INTO job_attempts(job_id,attempt_no,worker_id,status,started_at) VALUES(?,?,?,'Leased',?)",
        (claimed['id'], claimed['current_attempt_no'], worker_id, stamp),
    )
    conn.execute(
        'INSERT INTO job_leases(job_id,worker_id,leased_at,expires_at) VALUES(?,?,?,?)',
        (claimed['id'], worker_id, stamp, expires),
    )
    return claimed


def start_job(conn, *, job_id: int, worker_id: str) -> dict:
    record = get_job(conn, job_id)
    if record['status'] == 'Running' and record.get('lease_owner') == worker_id:
        return record
    if record['status'] != 'Leased' or record.get('lease_owner') != worker_id:
        raise JobLeaseError('Job is not leased by this worker')
    stamp = now()
    conn.execute("UPDATE jobs SET status='Running',updated_at=? WHERE id=?", (stamp, record['id']))
    conn.execute(
        "UPDATE job_attempts SET status='Running' WHERE job_id=? AND attempt_no=? AND worker_id=?",
        (record['id'], record['current_attempt_no'], worker_id),
    )
    return get_job(conn, record['id'])


def renew_lease(conn, *, job_id: int, worker_id: str, lease_seconds: int = 60) -> dict:
    record = get_job(conn, job_id)
    if record['status'] not in ACTIVE_STATES or record.get('lease_owner') != worker_id:
        raise JobLeaseError('Worker does not own an active lease for this job')
    stamp = now()
    expires = _future(lease_seconds)
    conn.execute('UPDATE jobs SET lease_expires_at=?,updated_at=? WHERE id=?', (expires, stamp, record['id']))
    conn.execute(
        'UPDATE job_leases SET expires_at=? WHERE job_id=? AND worker_id=? AND released_at IS NULL',
        (expires, record['id'], worker_id),
    )
    return get_job(conn, record['id'])


def complete_job(conn, *, job_id: int, worker_id: str) -> dict:
    record = get_job(conn, job_id)
    if record['status'] == 'Succeeded':
        return record
    if record['status'] not in ACTIVE_STATES or record.get('lease_owner') != worker_id:
        raise JobLeaseError('Worker does not own this job')
    stamp = now()
    conn.execute(
        "UPDATE jobs SET status='Succeeded',lease_owner=NULL,lease_expires_at=NULL,finished_at=?,last_error='',updated_at=? WHERE id=?",
        (stamp, stamp, record['id']),
    )
    conn.execute(
        "UPDATE job_attempts SET status='Succeeded',finished_at=?,error_message='' WHERE job_id=? AND attempt_no=? AND worker_id=?",
        (stamp, record['id'], record['current_attempt_no'], worker_id),
    )
    conn.execute(
        "UPDATE job_leases SET released_at=?,release_reason='Succeeded' WHERE job_id=? AND worker_id=? AND released_at IS NULL",
        (stamp, record['id'], worker_id),
    )
    return get_job(conn, record['id'])


def fail_job(conn, *, job_id: int, worker_id: str, error: str, base_backoff_seconds: int = 30) -> dict:
    record = get_job(conn, job_id)
    if record['status'] not in ACTIVE_STATES or record.get('lease_owner') != worker_id:
        raise JobLeaseError('Worker does not own this job')
    stamp = now()
    terminal = int(record['attempt_count']) >= int(record['max_attempts'])
    status = 'DeadLetter' if terminal else 'RetryScheduled'
    backoff = max(0, int(base_backoff_seconds)) * (2 ** max(0, int(record['attempt_count']) - 1))
    available_at = stamp if terminal else _future(min(backoff, 86400))
    message = str(error or 'Job execution failed')[:1000]
    conn.execute(
        """UPDATE jobs SET status=?,available_at=?,lease_owner=NULL,lease_expires_at=NULL,last_error=?,finished_at=?,updated_at=? WHERE id=?""",
        (status, available_at, message, stamp if terminal else None, stamp, record['id']),
    )
    conn.execute(
        "UPDATE job_attempts SET status='Failed',finished_at=?,error_message=? WHERE job_id=? AND attempt_no=? AND worker_id=?",
        (stamp, message, record['id'], record['current_attempt_no'], worker_id),
    )
    conn.execute(
        "UPDATE job_leases SET released_at=?,release_reason=? WHERE job_id=? AND worker_id=? AND released_at IS NULL",
        (stamp, status, record['id'], worker_id),
    )
    return get_job(conn, record['id'])


def replay_job(conn, job_id: int | str) -> dict:
    record = get_job(conn, job_id)
    if record['status'] not in ('DeadLetter', 'Failed'):
        raise JobStateError(f"Job is {record['status']}, not replayable")
    stamp = now()
    conn.execute(
        """UPDATE jobs SET status='Pending',available_at=?,lease_owner=NULL,lease_expires_at=NULL,attempt_count=0,last_error='',started_at=NULL,finished_at=NULL,cancelled_at=NULL,updated_at=? WHERE id=?""",
        (stamp, stamp, record['id']),
    )
    return get_job(conn, record['id'])


def cancel_job(conn, job_id: int | str) -> dict:
    record = get_job(conn, job_id)
    if record['status'] == 'Cancelled':
        return record
    if record['status'] in ('Succeeded', 'DeadLetter'):
        raise JobStateError(f"Job is {record['status']} and cannot be cancelled")
    stamp = now()
    conn.execute(
        "UPDATE jobs SET status='Cancelled',lease_owner=NULL,lease_expires_at=NULL,cancelled_at=?,finished_at=?,updated_at=? WHERE id=?",
        (stamp, stamp, stamp, record['id']),
    )
    conn.execute(
        "UPDATE job_attempts SET status='Cancelled',finished_at=? WHERE job_id=? AND status IN ('Leased','Running')",
        (stamp, record['id']),
    )
    conn.execute(
        "UPDATE job_leases SET released_at=?,release_reason='Cancelled' WHERE job_id=? AND released_at IS NULL",
        (stamp, record['id']),
    )
    return get_job(conn, record['id'])
