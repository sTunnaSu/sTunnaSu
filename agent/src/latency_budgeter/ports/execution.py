"""Execution-engine boundary used by Phase 8 Step 3."""

from __future__ import annotations

from typing import Any, Mapping, Protocol, runtime_checkable


@runtime_checkable
class ExecutionLifecycleObserver(Protocol):
    """Fail-closed authorisation plus post-transition observation callbacks."""

    def authorize_submission(self, intent: Mapping[str, Any]) -> Any | None:
        """Return an opaque ALLOW token or ``None`` before actual submission."""
        ...

    def validate_authorization(self, intent: Mapping[str, Any], authorization: Any) -> bool:
        """Prove the token belongs to this exact intent before engine mutation."""
        ...

    def on_order_submitted(self, order: Any, authorization: Any) -> None:
        """Observe a submission only after the engine registered it."""
        ...

    def on_fill(self, order: Any, fill: Any) -> None:
        """Observe one actual immutable fill."""
        ...

    def on_order_terminal(self, order: Any) -> None:
        """Observe one actual terminal order state."""
        ...
