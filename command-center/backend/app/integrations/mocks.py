"""Mock adapters for outward-facing integrations (filled in with the agent modules)."""

from __future__ import annotations

from . import Integration

ADAPTERS: list[type[Integration]] = []
