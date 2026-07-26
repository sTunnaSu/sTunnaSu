"""Experimental composition and orchestration for Phase 8 paper research.

The package is deliberately separate from :mod:`src.live`: Phase 8 can read
and, when explicitly authorized, mutate only the Alpaca paper account.  The
default runtime mode is research-only.
"""

from src.phase8_runtime.configuration import Phase8RuntimeConfig, load_runtime_config
from src.phase8_runtime.models import RuntimeMode
from src.phase8_runtime.runtime import Phase8Runtime, build_phase8_runtime

__all__ = [
    "Phase8Runtime",
    "Phase8RuntimeConfig",
    "RuntimeMode",
    "build_phase8_runtime",
    "load_runtime_config",
]
