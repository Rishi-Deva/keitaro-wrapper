"""Keitaro client errors.

Messages contain only HTTP method + API path (never the tracker host or the API key),
so they are safe to log and to return to the frontend.
"""
from typing import Any


class KeitaroError(Exception):
    """Base error. `status` is the upstream HTTP status (None for network errors)."""

    def __init__(self, message: str, *, status: int | None = None, details: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.details = details

    def __str__(self) -> str:
        return self.message


class KeitaroAuthError(KeitaroError):
    """401/403: wrong key or the key lacks permission for this resource."""


class KeitaroNotFound(KeitaroError):
    """404. Keitaro returns nginx HTML here, not JSON."""


class KeitaroValidationError(KeitaroError):
    """400/422. `details` holds Keitaro's field errors, e.g. {"alias": ["Alias is required"]}."""


class KeitaroUnavailable(KeitaroError):
    """Timeout, connection error or 5xx."""


class KeitaroBadResponse(KeitaroError):
    """2xx with a body we cannot parse."""
