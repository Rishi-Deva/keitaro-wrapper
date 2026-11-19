"""Async client for Keitaro Admin API v1.

Field names and behaviour are verified against a real tracker, see docs/api-notes.md.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.keitaro.errors import (
    KeitaroAuthError,
    KeitaroBadResponse,
    KeitaroError,
    KeitaroNotFound,
    KeitaroUnavailable,
    KeitaroValidationError,
)

log = logging.getLogger(__name__)

JSON = Any


class KeitaroClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 15.0,
        connect_timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("Keitaro API key is empty")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"Api-Key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(timeout, connect=connect_timeout),
            transport=transport,
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "KeitaroClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------- low level ----------

    async def _request(self, method: str, path: str, *, json: JSON = None, params: dict | None = None) -> JSON:
        path = path.lstrip("/")
        where = f"{method} {path}"
        try:
            resp = await self._client.request(method, path, json=json, params=params)
        except httpx.TimeoutException as e:
            log.warning("keitaro timeout: %s", where)
            raise KeitaroUnavailable(f"Keitaro timeout on {where}") from e
        except httpx.HTTPError as e:
            log.warning("keitaro network error: %s (%s)", where, type(e).__name__)
            raise KeitaroUnavailable(f"Keitaro is unreachable on {where}: {type(e).__name__}") from e

        status = resp.status_code
        body = _safe_json(resp)
        if 200 <= status < 300:
            if body is _NO_JSON:
                raise KeitaroBadResponse(f"Keitaro returned non-JSON body on {where}", status=status)
            return body

        raw = body if body is not _NO_JSON else None
        msg = _error_text(raw) or resp.reason_phrase or "error"
        detail = _field_errors(raw)  # never pass Keitaro's raw body (e.g. "stacktrace") further
        log.info("keitaro %s -> %s", where, status)
        if status in (401, 403):
            raise KeitaroAuthError(f"Keitaro denied {where}: {msg}", status=status, details=detail)
        if status == 404:
            raise KeitaroNotFound(f"Not found in Keitaro: {where}", status=status, details=detail)
        if status in (400, 422):
            raise KeitaroValidationError(f"Keitaro rejected {where}: {msg}", status=status, details=detail)
        if status >= 500:
            raise KeitaroUnavailable(f"Keitaro server error {status} on {where}", status=status, details=detail)
        raise KeitaroError(f"Unexpected Keitaro response {status} on {where}", status=status, details=detail)

    # ---------- reference data ----------

    async def list_offers(self) -> list[dict]:
        return _as_list(await self._request("GET", "offers"))

    async def list_domains(self) -> list[dict]:
        return _as_list(await self._request("GET", "domains"))

    async def list_campaign_groups(self) -> list[dict]:
        # verified: type must be "campaigns" (plural); "campaign" returns []
        return _as_list(await self._request("GET", "groups", params={"type": "campaigns"}))

    async def list_traffic_sources(self) -> list[dict]:
        return _as_list(await self._request("GET", "traffic_sources"))

    # ---------- campaigns ----------

    async def list_campaigns(self) -> list[dict]:
        return _as_list(await self._request("GET", "campaigns"))

    async def get_campaign(self, campaign_id: int) -> dict:
        return _as_dict(await self._request("GET", f"campaigns/{int(campaign_id)}"))

    async def create_campaign(self, payload: dict) -> dict:
        return _as_dict(await self._request("POST", "campaigns", json=payload))

    async def delete_campaign(self, campaign_id: int) -> None:
        # soft delete: campaign gets state=deleted
        await self._request("DELETE", f"campaigns/{int(campaign_id)}")

    # ---------- streams ----------

    async def list_streams(self, campaign_id: int) -> list[dict]:
        return _as_list(await self._request("GET", f"campaigns/{int(campaign_id)}/streams"))

    async def create_stream(self, payload: dict) -> dict:
        return _as_dict(await self._request("POST", "streams", json=payload))

    async def update_stream(self, stream_id: int, payload: dict) -> dict:
        # partial PUT is fine; note that `offers` replaces the whole list
        return _as_dict(await self._request("PUT", f"streams/{int(stream_id)}", json=payload))


_NO_JSON = object()


def _safe_json(resp: httpx.Response) -> JSON:
    if "json" not in resp.headers.get("content-type", ""):
        return _NO_JSON
    try:
        return resp.json()
    except ValueError:
        return _NO_JSON


def _error_text(body: JSON) -> str:
    """Keitaro error bodies: {"error": "..."} or {"field": ["msg", ...]}."""
    if isinstance(body, dict):
        if isinstance(body.get("error"), str):
            return body["error"][:500]
        parts = []
        for k, v in body.items():
            if isinstance(v, list):
                parts.append(f"{k}: {'; '.join(map(str, v))}")
            elif isinstance(v, str):
                parts.append(f"{k}: {v}")
        return ", ".join(parts)
    if isinstance(body, str):
        return body
    return ""


_DROP_KEYS = {"stacktrace", "trace", "exception", "file", "line"}


def _field_errors(body: JSON) -> dict[str, list[str]] | None:
    """Keep only {"field": ["message", ...]} pairs from a Keitaro error body."""
    if not isinstance(body, dict):
        return None
    out: dict[str, list[str]] = {}
    for k, v in body.items():
        if k in _DROP_KEYS or k == "error":
            continue
        if isinstance(v, str):
            out[str(k)] = [v[:500]]
        elif isinstance(v, list) and all(isinstance(x, str) for x in v):
            out[str(k)] = [x[:500] for x in v]
    return out or None


def _as_list(body: JSON) -> list[dict]:
    if not isinstance(body, list):
        raise KeitaroBadResponse("Expected a list from Keitaro")
    return body


def _as_dict(body: JSON) -> dict:
    # DELETE/GET by id sometimes return a one-element list
    if isinstance(body, list) and len(body) == 1 and isinstance(body[0], dict):
        return body[0]
    if not isinstance(body, dict):
        raise KeitaroBadResponse("Expected an object from Keitaro")
    return body
