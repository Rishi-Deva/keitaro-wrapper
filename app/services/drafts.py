"""Part 2 editor: local draft + publish (as in the customer's video).

Fetch      -> copy streams/offers from Keitaro into our DB (read-only for Keitaro).
add/remove/bring back/pin/unpin -> change ONLY our DB, shares recalculated at once, stream becomes dirty.
Cancel     -> restore offers from published_snapshot.
Push       -> PUT full list of active offers to Keitaro, verify the response. Our prefix only.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.keitaro.client import KeitaroClient
from app.keitaro.errors import KeitaroBadResponse, KeitaroError, KeitaroNotFound, KeitaroUnavailable
from app.models import AuditLog, CreatedCampaign, Stream, StreamOffer
from app.services import shares
from app.services.errors import ServiceError

DIRTY_FETCH_WARNING = "has unpublished changes, not refreshed: Push or Cancel first"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()  # SQLite drops tzinfo


def is_ours(name: str | None, s: Settings) -> bool:
    return str(name or "").startswith(s.campaign_prefix)


# ---------- serialization ----------

def _snap(rows: list[StreamOffer]) -> list[dict]:
    return [
        {"offer_id": r.offer_id, "offer_name": r.offer_name, "share": r.share, "pinned": r.pinned,
         "removed": r.removed, "in_keitaro": r.in_keitaro, "position": r.position, "state": r.state or "active"}
        for r in sorted(rows, key=lambda x: x.position)
    ]


KtState = list[tuple[int, int, str]]
SNAP_FIELDS = ("offer_id", "offer_name", "share", "pinned", "removed", "in_keitaro", "position", "state")


def _kt_state(items: list[dict]) -> KtState:
    """What Keitaro holds / would hold: not-removed offers as (offer_id, share, state), order-independent."""
    return sorted((int(i["offer_id"]), int(i["share"]), str(i.get("state") or "active"))
                  for i in items if not i.get("removed"))


def kt_offer_state(o: dict) -> str:
    st = o.get("state") or "active"
    if not isinstance(st, str) or not re.fullmatch(r"[a-z_]{1,32}", st):
        raise KeitaroBadResponse("Keitaro returned a malformed offer state")
    return st


def dedupe_kt_offers(raw: list | None) -> tuple[list[dict], list[int]]:
    """Keitaro stream offers with duplicates of the same offer_id removed (first wins) -> (offers, dup ids).
    Used by BOTH Fetch and Push checks so they always see the same list."""
    seen, out, dups = set(), [], []
    try:
        for o in raw or []:
            oid = int(o["offer_id"])
            if oid in seen:
                dups.append(oid)
                continue
            seen.add(oid)
            out.append(o)
    except (KeyError, TypeError, ValueError) as e:
        raise KeitaroBadResponse(f"Keitaro returned malformed stream offers: {type(e).__name__}") from e
    return out, dups


def _parse_kt_offers(raw: list | None) -> KtState:
    """Offers of a stream as returned by Keitaro -> KtState. Malformed data -> KeitaroBadResponse."""
    offers, _ = dedupe_kt_offers(raw)
    try:
        return sorted((int(o["offer_id"]), int(o.get("share") or 0), kt_offer_state(o)) for o in offers)
    except (KeyError, TypeError, ValueError) as e:
        raise KeitaroBadResponse(f"Keitaro returned malformed stream offers: {type(e).__name__}") from e


async def _read_kt_stream(client: KeitaroClient, st: Stream) -> KtState | None:
    """Current offers of this stream in Keitaro (read-only). None if the stream is gone."""
    for x in await client.list_streams(st.campaign_id):
        try:
            if int(x["id"]) == st.keitaro_stream_id:
                return _parse_kt_offers(x.get("offers"))
        except (KeyError, TypeError, ValueError) as e:
            raise KeitaroBadResponse("Keitaro returned a malformed stream") from e
    return None


def _recompute_dirty(stream: Stream) -> None:
    stream.dirty = _kt_state(_snap(stream.offers)) != _kt_state(stream.published_snapshot or [])


def _audit(db: Session, action: str, stream: Stream | None = None, offer_id: int | None = None,
           before: list | None = None, campaign_id: int | None = None) -> None:
    db.add(AuditLog(
        action=action,
        campaign_id=campaign_id if campaign_id is not None else (stream.campaign_id if stream else None),
        stream_id=stream.keitaro_stream_id if stream else None,
        offer_id=offer_id,
        shares_before=before,
        shares_after=_snap(stream.offers) if stream else None,
    ))


def stream_view(stream: Stream, s: Settings) -> dict:
    rows = sorted(stream.offers, key=lambda x: x.position)
    return {
        "id": stream.keitaro_stream_id,
        "campaign_id": stream.campaign_id,
        "name": stream.name,
        "position": stream.position,
        "schema": stream.schema,
        "has_offers": stream.has_offers,
        "dirty": stream.dirty,
        "can_push": is_ours(stream.campaign_name, s),
        "fetched_at": _iso(stream.fetched_at),
        "pushed_at": _iso(stream.pushed_at),
        "share_total": shares.active_total(rows),
        "share_valid": shares.is_valid(rows) if stream.has_offers else True,
        "offers": [
            {"offer_id": r.offer_id, "name": r.offer_name or f"#{r.offer_id}", "share": r.share,
             "pinned": r.pinned, "removed": r.removed, "in_keitaro": r.in_keitaro, "state": r.state or "active"}
            for r in rows
        ],
    }


def campaign_view(campaign_id: int, db: Session, s: Settings, warnings: list[str] | None = None) -> dict:
    streams = db.scalars(select(Stream).where(Stream.campaign_id == campaign_id).order_by(Stream.position)).all()
    name = streams[0].campaign_name if streams else None
    return {
        "id": campaign_id,
        "name": name,
        "editable": is_ours(name, s),
        "kt_url": s.campaign_url(campaign_id),
        "fetched": bool(streams),
        "streams": [stream_view(x, s) for x in streams],
        "warnings": warnings or [],
    }


# ---------- Fetch ----------

async def fetch(campaign_id: int, client: KeitaroClient, db: Session, s: Settings) -> dict:
    try:
        campaign = await client.get_campaign(campaign_id)
    except KeitaroNotFound:
        raise ServiceError(404, "campaign_not_found", f"Campaign {campaign_id} not found in Keitaro") from None
    kt_streams = await client.list_streams(campaign_id)
    try:
        names = {int(o["id"]): o.get("name") for o in await client.list_offers()}
    except (KeyError, TypeError, ValueError) as e:
        raise KeitaroBadResponse("Keitaro returned a malformed offer list") from e
    try:
        return _fetch_apply(campaign_id, campaign, kt_streams, names, db, s)
    except KeitaroBadResponse:
        db.rollback()
        raise
    except (KeyError, TypeError, ValueError) as e:
        db.rollback()
        raise KeitaroBadResponse(f"Keitaro returned malformed streams: {type(e).__name__}") from e


def _fetch_apply(campaign_id: int, campaign: dict, kt_streams: list, names: dict, db: Session, s: Settings) -> dict:
    now, warnings = _now(), []
    local = {x.keitaro_stream_id: x for x in db.scalars(select(Stream).where(Stream.campaign_id == campaign_id))}
    seen: set[int] = set()

    for ks in kt_streams:
        sid = int(ks["id"])
        seen.add(sid)
        st = local.get(sid)
        if st is None:
            st = Stream(keitaro_stream_id=sid, campaign_id=campaign_id, published_snapshot=[], dirty=False)
            st.offers = []
            db.add(st)
        st.campaign_name = campaign.get("name") or ""
        st.name, st.position, st.schema = ks.get("name"), int(ks.get("position") or 0), ks.get("schema")
        kt_offers, dups = dedupe_kt_offers(ks.get("offers"))
        st.has_offers = st.schema == "landings" or bool(kt_offers) or bool(st.offers)
        if st.dirty:
            warnings.append(f"Stream {sid} {DIRTY_FETCH_WARNING}")
            continue
        before = _snap(st.offers)
        by_id = {r.offer_id: r for r in st.offers}
        next_pos = max((r.position for r in st.offers), default=-1) + 1
        kt_ids = []
        for oid in dups:
            warnings.append(f"Stream {sid}: offer {oid} appears twice in Keitaro, the second entry is ignored "
                            "(a Push with changes stores it once; without changes Keitaro keeps the duplicate)")
        for ko in kt_offers:
            oid = int(ko["offer_id"])
            kt_ids.append(oid)
            row = by_id.get(oid)
            if row is None:
                row = StreamOffer(offer_id=oid, position=next_pos, pinned=False)
                next_pos += 1
                st.offers.append(row)
            row.share, row.removed, row.in_keitaro = int(ko.get("share") or 0), False, True
            row.state = kt_offer_state(ko)
            row.offer_name = names.get(oid, row.offer_name)
        for oid, row in by_id.items():
            if oid not in kt_ids:  # deleted in Keitaro (or archived earlier) -> grey
                row.removed, row.share, row.pinned, row.in_keitaro = True, 0, False, False
                # row.state keeps the last known Keitaro state: Bring back restores it, with or without a Fetch
        # keep Keitaro order for offers present there
        order = {oid: i for i, oid in enumerate(kt_ids)}
        ordered = sorted(st.offers, key=lambda r: (r.offer_id not in order, order.get(r.offer_id, 0), r.position))
        for i, r in enumerate(ordered):
            r.position = i
        st.published_snapshot = _snap(st.offers)
        st.dirty = False
        st.fetched_at = now
        if before != st.published_snapshot:
            _audit(db, "fetch", st, before=before)

    for sid, st in local.items():
        if sid not in seen:
            if st.dirty:
                warnings.append(f"Stream {sid} is gone from Keitaro but has unpublished changes; kept locally")
            else:
                db.delete(st)
    db.commit()
    return campaign_view(campaign_id, db, s, warnings)


# ---------- draft operations ----------

def _stream(db: Session, stream_id: int) -> Stream:
    st = db.scalar(select(Stream).where(Stream.keitaro_stream_id == stream_id))
    if st is None:
        raise ServiceError(404, "stream_not_fetched", f"Stream {stream_id} is not in our DB. Fetch the campaign first")
    if not st.has_offers:
        raise ServiceError(409, "stream_has_no_offers", "This stream does not use offers")
    return st


def _row(st: Stream, offer_id: int) -> StreamOffer:
    row = next((r for r in st.offers if r.offer_id == offer_id), None)
    if row is None:
        raise ServiceError(404, "offer_not_in_stream", f"Offer {offer_id} is not in stream {st.keitaro_stream_id}")
    return row


async def _live_offer(client: KeitaroClient, offer_id: int) -> dict:
    """The offer must exist in Keitaro and must not be deleted there."""
    try:
        offer = next((o for o in await client.list_offers() if int(o["id"]) == offer_id), None)
    except (KeyError, TypeError, ValueError) as e:
        raise KeitaroBadResponse("Keitaro returned a malformed offer list") from e
    if offer is None or offer.get("state") == "deleted":
        raise ServiceError(422, "offer_not_found", f"Offer {offer_id} not found in Keitaro (or deleted there)")
    return offer


def _rebalance(st: Stream) -> None:
    rows = sorted(st.offers, key=lambda x: x.position)
    try:
        shares.apply(rows)
    except shares.ShareError as e:
        raise ServiceError(409, e.code, e.message) from e


def _finish(db: Session, st: Stream, action: str, offer_id: int, before: list, s: Settings) -> dict:
    _recompute_dirty(st)
    _audit(db, action, st, offer_id=offer_id, before=before)
    db.commit()
    return stream_view(st, s)


async def add_offer(stream_id: int, offer_id: int, client: KeitaroClient, db: Session, s: Settings) -> dict:
    st = _stream(db, stream_id)
    existing = next((r for r in st.offers if r.offer_id == offer_id), None)
    if existing is not None:
        if existing.removed:
            raise ServiceError(409, "offer_removed_use_bring_back", f"Offer {offer_id} is archived here: use Bring back")
        raise ServiceError(409, "duplicate_offer", f"Offer {offer_id} is already in this stream")
    offer = await _live_offer(client, offer_id)
    before = _snap(st.offers)
    pos = max((r.position for r in st.offers), default=-1) + 1
    row = StreamOffer(offer_id=offer_id, offer_name=offer.get("name"), position=pos, share=0,
                      pinned=False, removed=False, in_keitaro=False, state="active")
    st.offers.append(row)
    try:
        _rebalance(st)
    except ServiceError:
        st.offers.remove(row)  # pending + delete-orphan -> dropped from the session
        raise
    return _finish(db, st, "offer_added", offer_id, before, s)


def _mutate(db: Session, st: Stream, change, action: str, offer_id: int, s: Settings) -> dict:
    before = _snap(st.offers)
    saved = {r.offer_id: (r.share, r.pinned, r.removed) for r in st.offers}
    change()
    try:
        _rebalance(st)
    except ServiceError:
        for r in st.offers:  # all-or-nothing
            r.share, r.pinned, r.removed = saved[r.offer_id]
        db.rollback()
        raise
    return _finish(db, st, action, offer_id, before, s)


def remove_offer(stream_id: int, offer_id: int, db: Session, s: Settings) -> dict:
    st = _stream(db, stream_id)
    row = _row(st, offer_id)
    if row.removed:
        raise ServiceError(409, "already_removed", f"Offer {offer_id} is already removed")

    def change() -> None:
        row.removed, row.pinned, row.share = True, False, 0
    return _mutate(db, st, change, "offer_removed", offer_id, s)


async def bring_back(stream_id: int, offer_id: int, client: KeitaroClient, db: Session, s: Settings) -> dict:
    st = _stream(db, stream_id)
    row = _row(st, offer_id)
    if not row.removed:
        raise ServiceError(409, "not_removed", f"Offer {offer_id} is not removed")
    await _live_offer(client, offer_id)

    def change() -> None:
        row.removed = False
    return _mutate(db, st, change, "offer_brought_back", offer_id, s)


def set_pin(stream_id: int, offer_id: int, pinned: bool, db: Session, s: Settings) -> dict:
    st = _stream(db, stream_id)
    row = _row(st, offer_id)
    if row.removed:
        raise ServiceError(409, "offer_removed", "Removed offers cannot be pinned")
    if row.pinned == pinned:
        return stream_view(st, s)

    def change() -> None:
        row.pinned = pinned
    return _mutate(db, st, change, "pin" if pinned else "unpin", offer_id, s)


def cancel(stream_id: int, db: Session, s: Settings) -> dict:
    st = _stream(db, stream_id)
    before = _snap(st.offers)
    st.offers.clear()
    db.flush()
    for it in st.published_snapshot or []:
        st.offers.append(StreamOffer(**{k: it.get(k, "active" if k == "state" else None) for k in SNAP_FIELDS}))
    st.dirty = False
    _audit(db, "cancel", st, before=before)
    db.commit()
    return stream_view(st, s)


# ---------- Push ----------

def _mark_published(db: Session, st: Stream, note: str | None, s: Settings) -> dict:
    before = list(st.published_snapshot or [])
    for r in st.offers:
        r.in_keitaro = not r.removed
    st.published_snapshot = _snap(st.offers)
    st.dirty = False
    st.pushed_at = _now()
    _audit(db, "push", st, before=before)
    db.commit()
    view = stream_view(st, s)
    view["note"] = note
    return view


async def _resolve_unclear_push(client: KeitaroClient, db: Session, st: Stream, draft: KtState, published: KtState,
                                reason: str, status: int, s: Settings) -> dict:
    """PUT timed out or its response did not match. Re-read Keitaro (read-only) and decide."""
    try:
        kt_after = await _read_kt_stream(client, st)
    except KeitaroError:
        raise ServiceError(504, "push_unknown",
                           f"{reason}. Could not re-check Keitaro either. The draft is kept: press Push again later, "
                           "it checks Keitaro first and will not send the same change twice") from None
    if kt_after is None:
        raise ServiceError(404, "stream_gone",
                           f"{reason}. The stream no longer exists in Keitaro; the draft is kept, Fetch to see Keitaro")
    if kt_after == draft:
        return _mark_published(db, st, f"{reason}, but re-reading Keitaro confirmed the change was saved", s)
    if kt_after == published:
        raise ServiceError(status, "push_not_applied",
                           f"{reason}. Keitaro still has the previous state; the draft is kept, press Push again")
    raise ServiceError(409, "push_inconsistent",
                       f"{reason}. Keitaro now holds something else than both the draft and the last published "
                       "state. The draft is kept; Fetch is blocked while it exists: Cancel, then Fetch to see Keitaro")


async def push(stream_id: int, client: KeitaroClient, db: Session, s: Settings) -> dict:
    st = _stream(db, stream_id)
    try:
        campaign = await client.get_campaign(st.campaign_id)
    except KeitaroNotFound:
        raise ServiceError(404, "campaign_not_found", f"Campaign {st.campaign_id} not found in Keitaro") from None
    if not is_ours(campaign.get("name"), s):  # check the live name, not our cached one
        raise ServiceError(403, "push_forbidden", f"Push is allowed only for campaigns starting with '{s.campaign_prefix}'")
    rows = sorted(st.offers, key=lambda x: x.position)
    if not shares.is_valid(rows):
        raise ServiceError(409, "invalid_shares",
                           f"Active shares sum to {shares.active_total(rows)}%, must be exactly 100% with each >= 1%")

    draft = _kt_state(_snap(rows))
    published = _kt_state(st.published_snapshot or [])
    kt_now = await _read_kt_stream(client, st)
    if kt_now is None:
        raise ServiceError(404, "stream_gone", f"Stream {stream_id} no longer exists in Keitaro. Fetch again")
    if kt_now == draft:
        # e.g. the previous Push timed out after Keitaro had saved it: nothing to send, just confirm
        return _mark_published(db, st, "Keitaro already has exactly this state; nothing was sent", s)
    if kt_now != published:
        raise ServiceError(409, "changed_in_keitaro",
                           "Offers were changed in Keitaro after the last Fetch. Cancel, Fetch, and redo the changes")

    payload = [{"offer_id": r.offer_id, "share": r.share, "state": r.state or "active"} for r in rows if not r.removed]
    try:
        resp = await client.update_stream(stream_id, {"offers": payload})
    except KeitaroUnavailable as e:
        if e.status is not None:  # Keitaro answered with 5xx
            reason, status = f"Keitaro answered the PUT with an error ({e.message})", 502
        else:  # timeout / connection problem: no answer at all
            reason, status = "Keitaro did not answer the PUT", 504
        return await _resolve_unclear_push(client, db, st, draft, published, reason, status, s)
    except KeitaroBadResponse:
        return await _resolve_unclear_push(client, db, st, draft, published,
                                           "Keitaro answered the PUT with an unreadable response", 502, s)
    try:
        got = _parse_kt_offers(resp.get("offers"))
    except KeitaroBadResponse:
        got = None
    if got != draft:
        return await _resolve_unclear_push(client, db, st, draft, published,
                                           "Keitaro answered the PUT with offers differently than sent", 502, s)
    return _mark_published(db, st, None, s)


# ---------- campaign delete ----------

async def delete_campaign(campaign_id: int, client: KeitaroClient, db: Session, s: Settings) -> dict:
    record = db.scalar(select(CreatedCampaign).where(CreatedCampaign.keitaro_id == campaign_id))
    already = False
    try:
        name = (await client.get_campaign(campaign_id)).get("name")
    except KeitaroNotFound:
        # Keitaro hides soft-deleted campaigns (404). Only our own local record proves it is ours.
        if record is None:
            raise ServiceError(404, "campaign_not_found", f"Campaign {campaign_id} not found") from None
        name, already = record.name, True
    if not is_ours(name, s):
        raise ServiceError(403, "delete_forbidden", f"Only campaigns starting with '{s.campaign_prefix}' can be deleted")
    if not already:
        try:
            await client.delete_campaign(campaign_id)
        except KeitaroNotFound:
            already = True
    if record is not None:
        record.status = "deleted"
    for st in db.scalars(select(Stream).where(Stream.campaign_id == campaign_id)):
        db.delete(st)
    _audit(db, "campaign_deleted", campaign_id=campaign_id)
    db.commit()
    return {"id": campaign_id, "deleted": True, "already_deleted_in_keitaro": already}
