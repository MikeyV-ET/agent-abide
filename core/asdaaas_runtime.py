"""Process-wide asdaaas runtime state.

asdaaas.py is often loaded twice: once as ``__main__`` and once as ``import asdaaas``.
Module-level globals on asdaaas.py then diverge (health shows backend=grok,
session_id=null, model=unknown while the live process is Claude).

Anything that must be shared across those two module objects lives HERE.
"""
from __future__ import annotations

current_model_id: str = "unknown"
current_session_id = None  # type: ignore
current_backend_type: str = "unknown"  # set at boot; not default "grok"
code_version: str = "unknown"
current_reasoning_effort: str | None = None


def set_identity(*, model_id=None, session_id=..., backend_type=None,
                 code_version_val=None, reasoning_effort=...):
    global current_model_id, current_session_id, current_backend_type, code_version
    global current_reasoning_effort
    if model_id is not None:
        current_model_id = model_id
    if session_id is not ...:
        current_session_id = session_id
    if backend_type is not None:
        current_backend_type = backend_type
    if code_version_val is not None:
        code_version = code_version_val
    if reasoning_effort is not ...:
        current_reasoning_effort = reasoning_effort
