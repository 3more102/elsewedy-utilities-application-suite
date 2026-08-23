"""EUAS approval request, delegation, and integrity-evidence application."""

from .delegation import DelegationError, active_delegation, create_delegation, revoke_delegation
from .evidence import (
    ApprovalSnapshotError,
    append_evidence_event,
    capture_request_snapshot,
    decision_history,
    decision_snapshot,
    expected_intent,
    ensure_request_snapshot,
    record_signature,
    snapshot_hash,
    target_snapshot,
    verify_evidence_chain,
    verify_request_snapshot,
    verify_signature_chain,
)
from .service import create_approval, resolve_approval

__all__ = [
    'create_approval', 'resolve_approval',
    'DelegationError', 'active_delegation', 'create_delegation', 'revoke_delegation',
    'ApprovalSnapshotError', 'capture_request_snapshot', 'ensure_request_snapshot', 'verify_request_snapshot',
    'target_snapshot', 'decision_snapshot', 'snapshot_hash', 'expected_intent', 'record_signature', 'verify_signature_chain',
    'append_evidence_event', 'verify_evidence_chain', 'decision_history',
]
