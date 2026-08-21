"""LangGraph adapters (§11.5).

Everything in this package exists so the rest of the codebase does not have to
know LangGraph. The import-boundary test in `tests/unit/test_project_layout.py`
enforces that: `langgraph` may be imported here and under `app/graphs/`, and
nowhere else.
"""

from __future__ import annotations

from app.infrastructure.langgraph.checkpointer import (
    CHECKPOINT_TABLES,
    CheckpointerProvider,
    checkpointer_dsn,
    setup_checkpoint_tables,
)

__all__ = [
    "CHECKPOINT_TABLES",
    "CheckpointerProvider",
    "checkpointer_dsn",
    "setup_checkpoint_tables",
]
