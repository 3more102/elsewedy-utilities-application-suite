"""Backward-compatible event imports during the modular architecture migration."""

from apps.events import (
    emit_event,
    process_outbox,
    rearm_outbox_event,
    record_workflow_event,
    workflow_event,
)

__all__ = [
    'emit_event',
    'process_outbox',
    'rearm_outbox_event',
    'record_workflow_event',
    'workflow_event',
]
