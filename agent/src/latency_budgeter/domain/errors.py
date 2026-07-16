"""Explicit Phase 8 failure types.

These exceptions distinguish malformed evidence, optimistic-concurrency
conflicts, and idempotency conflicts. Callers must never turn them into silent
data corrections.
"""


class Phase8Error(Exception):
    """Base class for Phase 8 failures."""


class TimestampValidationError(Phase8Error, ValueError):
    """A timestamp cannot be normalised without ambiguity."""


class IdentifierValidationError(Phase8Error, ValueError):
    """An identifier does not satisfy the frozen ownership contract."""


class IdentifierCollisionError(Phase8Error):
    """An identifier is already attached to different immutable content."""


class UnsupportedSchemaVersionError(Phase8Error, ValueError):
    """A record uses a schema version this implementation cannot interpret."""


class UnknownEventTypeError(Phase8Error, ValueError):
    """An event type is outside the frozen Phase 8 event family."""


class IdempotencyConflictError(Phase8Error):
    """An idempotency key was reused for semantically different content."""


class ConcurrentAppendError(Phase8Error):
    """The aggregate changed after the caller's expected version."""


class AggregateIntegrityError(Phase8Error):
    """An event conflicts with the immutable identity of its aggregate."""


class ProjectionError(Phase8Error):
    """An event stream cannot be deterministically reconstructed."""


class LedgerMigrationError(Phase8Error):
    """The persistent ledger schema cannot be safely opened or migrated."""


class HistoryMigrationError(Phase8Error):
    """The latency-history schema cannot be safely opened or migrated."""


class HistoryConflictError(Phase8Error):
    """A history identity was reused for different immutable content."""


class ConfigDriftError(Phase8Error):
    """A repeated decision attempted to use a different frozen configuration."""


class InsufficientLatencyHistory(Phase8Error):
    """At least one required component lacks a legal conservative estimate."""


class DecisionEvidenceError(Phase8Error):
    """Decision-time evidence violates the point-in-time contract."""


class ExecutionBlockedError(Phase8Error):
    """A non-ALLOW, unmatched, or already-submitted opportunity was blocked."""


class LifecycleConflictError(Phase8Error):
    """A lifecycle callback conflicts with immutable evidence already recorded."""


class LifecycleIntegrityError(Phase8Error):
    """Lifecycle evidence cannot satisfy the frozen Step 3 invariants."""


class OutcomeNotDueError(Phase8Error):
    """The frozen horizon or permitted actual-exit trigger has not occurred."""


class OutcomeEvidenceError(Phase8Error):
    """Outcome evidence is missing, ambiguous, post-hoc, or policy-incompatible."""


class OutcomeConflictError(Phase8Error):
    """A retry attempts to replace an immutable Step 4 outcome evaluation."""


class ExperimentIntegrityError(Phase8Error):
    """Matched experiment arms violate the frozen causal-comparison contract."""


class HoldoutPolicyError(Phase8Error):
    """A holdout or release rule is mutable, late, or internally inconsistent."""
