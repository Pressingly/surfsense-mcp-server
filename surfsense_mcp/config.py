"""Environment-driven configuration switches shared across tool modules."""

from __future__ import annotations

import os

_TRUTHY_ENV_VALUES: frozenset[str] = frozenset({"1", "true", "yes", "on"})


def delete_tools_enabled() -> bool:
    """Whether the destructive ``delete_*`` tools should be registered.

    Deletes are **off by default**. The five delete tools
    (``delete_search_space``, ``delete_document``, ``delete_research_thread``,
    ``delete_report``, ``delete_note``) are registered only when
    ``SURFSENSE_ENABLE_DELETE`` is truthy, so by default they never appear on
    ``tools/list`` and cannot be called. ``delete_search_space`` in particular
    cascades to every document, thread, and report in the space.

    The value is read at registration time (not import time) and is
    ``.strip().lower()``-normalised: a stray trailing space in a Docker
    ``.env`` file must not silently leave deletes off.
    """
    return os.getenv("SURFSENSE_ENABLE_DELETE", "").strip().lower() in _TRUTHY_ENV_VALUES
