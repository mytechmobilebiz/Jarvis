"""VPS & Server Management (CWP/SSH): restricted, allowlisted, audited server operations."""

from __future__ import annotations

from .base import BaseAgent


class ServerManagementAgent(BaseAgent):
    name = "server_management"
    title = "VPS & Server Management (CWP/SSH)"
    description = (
        "Server diagnostics and maintenance over SSH with a restricted non-root account. Only allowlisted operations "
        "can run; every command is audited; changes need approval, and production changes need an admin's typed confirmation."
    )
