"""Errors a service call can raise. Every one has a stable code an agent can branch on."""
from __future__ import annotations

CODES = ("invalid_argument", "unknown_kind", "path_outside_repo", "not_found",
         "no_active_session", "store_busy", "store_corrupt", "too_large", "queue_full", "internal")


class ServiceError(Exception):
    def __init__(self, code: str, message: str):
        if code not in CODES:
            raise ValueError(f"unknown service error code {code!r}")
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}
