from __future__ import annotations

VALID_PROJECT_TASK_STATUSES = ('Open', 'In Progress', 'Blocked', 'Completed', 'Cancelled')


class InvalidProjectTask(ValueError):
    pass


def normalize_task_changes(changes: dict) -> dict:
    normalized = dict(changes)
    status = normalized.get('status')
    if status is not None and status not in VALID_PROJECT_TASK_STATUSES:
        raise InvalidProjectTask('Invalid project task status')
    if status == 'Completed' and 'progress' not in normalized:
        normalized['progress'] = 100
    return normalized


def recalculate_project_progress(conn, project_id: int) -> float:
    row = conn.execute(
        "SELECT AVG(CASE WHEN status='Completed' THEN 100 ELSE progress END) avg_progress "
        "FROM project_tasks WHERE project_id=? AND status<>'Cancelled'",
        (project_id,),
    ).fetchone()
    progress = round(float(row['avg_progress'] or 0), 1)
    conn.execute('UPDATE projects SET progress=? WHERE id=?', (progress, project_id))
    return progress
