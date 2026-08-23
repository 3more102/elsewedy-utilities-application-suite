from app.auth import hash_password
from apps.jobs import claim_next_job, enqueue_job, fail_job, register_worker, start_job
from apps.observability import job_metric_lines, job_metrics_snapshot, readiness_snapshot
from core.database import db, init_db


def test_job_observability_reports_queue_worker_and_execution_states():
    init_db(hash_password)
    with db() as conn:
        register_worker(conn, worker_id='metrics-worker', name='metrics-worker')
        pending = enqueue_job(conn, job_type='metrics.pending', payload={})
        failed = enqueue_job(conn, job_type='metrics.failed', payload={}, max_attempts=1, priority=100)
        claimed = claim_next_job(conn, worker_id='metrics-worker')
        assert claimed and claimed['id'] in (pending['id'], failed['id'])
        start_job(conn, job_id=claimed['id'], worker_id='metrics-worker')
        fail_job(conn, job_id=claimed['id'], worker_id='metrics-worker', error='expected regression failure')
        snapshot = job_metrics_snapshot(conn)
        lines = job_metric_lines(snapshot)
        assert snapshot['active_workers'] >= 1
        assert snapshot['execution_total'] >= 1
        assert snapshot['execution_failure_total'] >= 1
        assert snapshot['dead_letter'] >= 1
        assert any(line.startswith('euas_jobs_dead_letter ') for line in lines)
        assert any(line.startswith('euas_worker_heartbeat_age_seconds ') for line in lines)


def test_readiness_exposes_job_health_without_failing_on_dead_letters():
    init_db(hash_password)
    with db() as conn:
        enqueue_job(conn, job_type='metrics.readiness.dead', payload={}, max_attempts=1)
        register_worker(conn, worker_id='readiness-worker')
        claimed = claim_next_job(conn, worker_id='readiness-worker')
        start_job(conn, job_id=claimed['id'], worker_id='readiness-worker')
        fail_job(conn, job_id=claimed['id'], worker_id='readiness-worker', error='dead-letter fixture')
    snapshot = readiness_snapshot()
    assert snapshot['status'] == 'ready'
    assert snapshot['checks']['jobs']['dead_letter'] >= 1
    assert 'worker_heartbeat_age_seconds' in snapshot['checks']['jobs']
