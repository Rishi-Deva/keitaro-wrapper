"""Part 1: create a campaign with two streams.

Stream 1: visitors from GEO -> redirect to FALLBACK_REDIRECT_URL (https://google.com).
Stream 2: everyone else -> selected offer (share 100).
On any failure after the campaign exists, the campaign is deleted (rollback).
Payload shapes are verified against the real API, see docs/api-notes.md.
"""
from __future__ import annotations

import logging
import re
import secrets

from sqlalchemy.orm import Session

from app.config import Settings
from app.geo import normalize_geo
from app.keitaro.client import KeitaroClient
from app.keitaro.errors import KeitaroBadResponse, KeitaroError, KeitaroUnavailable
from app.models import AuditLog, CreatedCampaign
from app.services.errors import ServiceError

log = logging.getLogger(__name__)

NAME_MAX = 200
_NAME_OK = re.compile(r"^[\w\- .\[\]()#]+$", re.UNICODE)


def build_name(raw: str, prefix: str) -> str:
    name = " ".join((raw or "").split())
    if not name:
        raise ServiceError(422, "name_required", "Campaign name is required")
    if not name.startswith(prefix):
        name = prefix + name
    if len(name) > NAME_MAX:
        raise ServiceError(422, "name_too_long", f"Campaign name is longer than {NAME_MAX} characters")
    if not _NAME_OK.match(name):
        raise ServiceError(422, "name_invalid", "Name may contain letters, digits, spaces and - _ . [ ] ( ) #")
    return name


def build_alias(name: str) -> str:
    # alias is required by Keitaro (verified 422 "Alias is required"); random suffix avoids collisions
    slug = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")[:60] or "campaign"
    return f"{slug}_{secrets.token_hex(3)}"


def campaign_payload(name: str, alias: str, s: Settings) -> dict:
    return {
        "name": name,
        "alias": alias,
        "type": "position",
        "state": "active",
        "domain_id": s.default_domain_id,
        "group_id": s.default_group_id,
        "traffic_source_id": s.default_traffic_source_id,
    }


def geo_stream_payload(campaign_id: int, geo: str, s: Settings) -> dict:
    return {
        "campaign_id": campaign_id,
        "type": "regular",
        "name": f"Geo {geo} -> redirect",
        "position": 1,
        "state": "active",
        "schema": "redirect",
        "action_type": "http",
        "action_payload": str(s.fallback_redirect_url),
        "collect_clicks": True,
        "filter_or": False,
        "filters": [{"name": "country", "mode": "accept", "payload": [geo]}],
    }


def offer_stream_payload(campaign_id: int, offer_id: int) -> dict:
    return {
        "campaign_id": campaign_id,
        "type": "regular",
        "name": f"Offer {offer_id}",
        "position": 2,
        "state": "active",
        "schema": "landings",
        "action_type": "http",
        "collect_clicks": True,
        "filter_or": False,
        "filters": [],
        "offers": [{"offer_id": offer_id, "share": 100, "state": "active"}],
    }


async def _cleanup_unclear_create(
    err: Exception, alias: str, record: CreatedCampaign, client: KeitaroClient, db: Session
) -> None:
    """POST campaigns had no clear answer. Find our campaign by its unique alias (read) and delete it."""
    reason = str(err) if isinstance(err, KeitaroError) else f"malformed Keitaro response ({type(err).__name__})"
    try:
        listed = await client.list_campaigns()
        found = [c for c in listed if isinstance(c, dict) and c.get("alias") == alias]
        found_ids = [int(c["id"]) for c in found]
    except (KeitaroError, KeyError, TypeError, ValueError) as le:
        record.status, record.error = "unknown", f"{reason}; lookup by alias failed: {le}"
        db.add(record)
        db.commit()
        raise ServiceError(
            504, "create_unknown",
            f"{reason}. Could not check whether the campaign was created. Look for alias '{alias}' in Keitaro "
            "and delete it if it exists",
        ) from err
    if not found:
        record.error = f"{reason}; no campaign with this alias in Keitaro"
        db.add(record)
        db.commit()
        raise ServiceError(504 if isinstance(err, KeitaroUnavailable) else 502, "create_failed",
                           f"{reason}. The campaign was not created") from err
    cid = found_ids[0]
    record.keitaro_id = cid
    try:
        await client.delete_campaign(cid)
        record.status, record.error = "rolled_back", f"{reason}; created anyway, deleted"
        db.add(AuditLog(action="campaign_rolled_back", campaign_id=cid))
        outcome = "it was created anyway and has been rolled back (deleted)"
    except KeitaroError as de:
        record.status, record.error = "rollback_failed", f"{reason}; rollback failed: {de}"
        outcome = f"it was created (id {cid}) but ROLLBACK FAILED, delete it manually"
    db.add(record)
    db.commit()
    raise ServiceError(502, "create_unclear", f"{reason}: {outcome}") from err


async def create_campaign(
    *, name: str, geo: str, offer_id: int, client: KeitaroClient, db: Session, settings: Settings
) -> dict:
    full_name = build_name(name, settings.campaign_prefix)
    code = normalize_geo(geo)
    if not code:
        raise ServiceError(422, "geo_invalid", f"Unknown country code: {geo!r}. Use ISO 3166-1 alpha-2, e.g. MX")
    offers = await client.list_offers()
    offer = next((o for o in offers if o.get("id") == offer_id), None)
    if offer is None or offer.get("state") == "deleted":
        raise ServiceError(422, "offer_not_found", f"Offer {offer_id} not found in Keitaro (or deleted there)")

    alias = build_alias(full_name)
    record = CreatedCampaign(name=full_name, alias=alias, geo=code, offer_id=offer_id, status="failed")

    try:
        campaign = await client.create_campaign(campaign_payload(full_name, alias, settings))
        cid = int(campaign["id"])
    except (KeitaroUnavailable, KeitaroBadResponse, KeyError, TypeError, ValueError) as e:
        # Unclear outcome: Keitaro may have created the campaign (e.g. timeout after it saved it).
        await _cleanup_unclear_create(e, alias, record, client, db)
        raise AssertionError("unreachable")  # _cleanup_unclear_create always raises
    except KeitaroError as e:  # definite rejection (422 etc.): nothing was created
        record.error = str(e)
        db.add(record)
        db.commit()
        raise

    record.keitaro_id = cid
    try:
        s1 = await client.create_stream(geo_stream_payload(cid, code, settings))
        record.geo_stream_id = int(s1["id"])
        s2 = await client.create_stream(offer_stream_payload(cid, offer_id))
        record.offer_stream_id = int(s2["id"])
    except (KeitaroError, KeyError, TypeError, ValueError) as e:
        record.error = str(e)
        log.warning("stream creation failed for campaign %s, rolling back", cid)
        try:
            await client.delete_campaign(cid)
            record.status = "rolled_back"
            db.add(AuditLog(action="campaign_rolled_back", campaign_id=cid))
        except KeitaroError as de:
            record.status = "rollback_failed"
            record.error = f"{e}; rollback failed: {de}"
            log.error("rollback failed for campaign %s", cid)
        db.add(record)
        db.commit()
        status = "rolled back" if record.status == "rolled_back" else "ROLLBACK FAILED, delete it manually"
        raise ServiceError(
            502, "stream_failed", f"Campaign {cid} created but streams failed ({status}): {e}"
        ) from e

    record.status = "created"
    db.add(record)
    db.add(AuditLog(action="campaign_created", campaign_id=cid, offer_id=offer_id))
    db.commit()
    return {
        "id": cid,
        "name": full_name,
        "alias": alias,
        "geo": code,
        "offer": {"id": offer_id, "name": offer.get("name")},
        "streams": [
            {"id": record.geo_stream_id, "position": 1, "geo": code, "redirect": str(settings.fallback_redirect_url)},
            {"id": record.offer_stream_id, "position": 2, "offer_id": offer_id, "share": 100},
        ],
    }
