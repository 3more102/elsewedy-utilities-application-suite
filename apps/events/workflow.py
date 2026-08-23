from __future__ import annotations

from core.database import now
from .outbox import emit_event


def record_workflow_event(
    conn,
    module: str,
    record_type: str,
    record_id: int,
    record_code: str,
    event: str,
    from_status: str,
    to_status: str,
    actor_id: int,
    notes: str = '',
) -> None:
    """Persist internal workflow history and the external outbox event atomically."""
    conn.execute(
        """INSERT INTO workflow_events(
               module,record_type,record_id,record_code,event,from_status,to_status,actor_id,notes,created_at
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            module,
            record_type,
            record_id,
            record_code,
            event,
            from_status or '',
            to_status or '',
            actor_id,
            notes or '',
            now(),
        ),
    )
    event_name = 'workflow.' + module.lower().replace(' ', '_') + '.' + event.lower().replace(' ', '_')
    emit_event(
        conn,
        event_name,
        record_type,
        record_code,
        {
            'record_id': record_id,
            'record_code': record_code,
            'from_status': from_status or '',
            'to_status': to_status or '',
            'actor_id': actor_id,
            'notes': notes or '',
        },
    )


workflow_event = record_workflow_event
