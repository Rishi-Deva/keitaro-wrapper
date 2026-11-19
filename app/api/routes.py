from collections.abc import Iterator

from fastapi import APIRouter, Depends, Query, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from app.config import Settings
from app.geo import COUNTRY_CODES
from app.keitaro.client import KeitaroClient
from app.schemas import CampaignCreate, OfferAdd
from app.services import creator, drafts

# Declared for OpenAPI/Swagger ("Authorize" -> value: keitaro-wrapper). The check itself is in app.main middleware.
csrf_header = APIKeyHeader(name="X-Requested-With", auto_error=False,
                           description="CSRF guard: required on POST/DELETE, value `keitaro-wrapper`")
router = APIRouter(prefix="/api", dependencies=[Security(csrf_header)])

OFFER_SEARCH_LIMIT = 20


def get_client(request: Request) -> KeitaroClient:
    return request.app.state.keitaro


def get_cfg(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Iterator[Session]:
    with request.app.state.sessionmaker() as s:
        yield s


@router.get("/config")
def config(s: Settings = Depends(get_cfg)) -> dict:
    return {
        "campaign_prefix": s.campaign_prefix,
        "redirect_url": s.fallback_redirect_url,
        "kt_link_enabled": bool(s.keitaro_campaign_url_template),
    }


@router.get("/geo")
def geo() -> list[str]:
    return sorted(COUNTRY_CODES)


@router.get("/offers")
async def offers(
    q: str = Query("", max_length=100), limit: int = Query(OFFER_SEARCH_LIMIT, ge=1, le=100),
    client: KeitaroClient = Depends(get_client),
) -> list[dict]:
    needle = q.strip().lower()
    out = []
    for o in await client.list_offers():
        if o.get("state") == "deleted":
            continue
        if needle and needle not in str(o.get("name", "")).lower() and needle != str(o.get("id")):
            continue
        out.append({"id": o["id"], "name": o.get("name"), "state": o.get("state")})
        if len(out) >= limit:
            break
    return out


@router.get("/campaigns")
async def campaigns(
    q: str = Query("", max_length=100),
    client: KeitaroClient = Depends(get_client),
    s: Settings = Depends(get_cfg),
) -> list[dict]:
    needle = q.strip().lower()
    return [
        {"id": c["id"], "name": c.get("name"), "state": c.get("state"), "editable": drafts.is_ours(c.get("name"), s)}
        for c in await client.list_campaigns()
        if not needle or needle in str(c.get("name", "")).lower() or needle == str(c.get("id"))
    ]


@router.post("/campaigns", status_code=201)
async def create_campaign(
    body: CampaignCreate,
    client: KeitaroClient = Depends(get_client),
    db: Session = Depends(get_db),
    s: Settings = Depends(get_cfg),
) -> dict:
    return await creator.create_campaign(
        name=body.name, geo=body.geo, offer_id=body.offer_id, client=client, db=db, settings=s
    )


@router.delete("/campaigns/{campaign_id}")
async def delete_campaign(
    campaign_id: int, client: KeitaroClient = Depends(get_client),
    db: Session = Depends(get_db), s: Settings = Depends(get_cfg),
) -> dict:
    return await drafts.delete_campaign(campaign_id, client, db, s)


# ---------- editor: local draft + publish ----------

@router.get("/campaigns/{campaign_id}/streams")
def campaign_streams(campaign_id: int, db: Session = Depends(get_db), s: Settings = Depends(get_cfg)) -> dict:
    """From our DB only (no Keitaro call). Empty until Fetch."""
    return drafts.campaign_view(campaign_id, db, s)


@router.post("/campaigns/{campaign_id}/fetch")
async def fetch_streams(
    campaign_id: int, client: KeitaroClient = Depends(get_client),
    db: Session = Depends(get_db), s: Settings = Depends(get_cfg),
) -> dict:
    return await drafts.fetch(campaign_id, client, db, s)


@router.post("/streams/{stream_id}/offers")
async def add_offer(
    stream_id: int, body: OfferAdd, client: KeitaroClient = Depends(get_client),
    db: Session = Depends(get_db), s: Settings = Depends(get_cfg),
) -> dict:
    return await drafts.add_offer(stream_id, body.offer_id, client, db, s)


@router.post("/streams/{stream_id}/offers/{offer_id}/remove")
def remove_offer(stream_id: int, offer_id: int, db: Session = Depends(get_db), s: Settings = Depends(get_cfg)) -> dict:
    return drafts.remove_offer(stream_id, offer_id, db, s)


@router.post("/streams/{stream_id}/offers/{offer_id}/bring-back")
async def bring_back(
    stream_id: int, offer_id: int, client: KeitaroClient = Depends(get_client),
    db: Session = Depends(get_db), s: Settings = Depends(get_cfg),
) -> dict:
    return await drafts.bring_back(stream_id, offer_id, client, db, s)


@router.post("/streams/{stream_id}/offers/{offer_id}/pin")
def pin(stream_id: int, offer_id: int, db: Session = Depends(get_db), s: Settings = Depends(get_cfg)) -> dict:
    return drafts.set_pin(stream_id, offer_id, True, db, s)


@router.post("/streams/{stream_id}/offers/{offer_id}/unpin")
def unpin(stream_id: int, offer_id: int, db: Session = Depends(get_db), s: Settings = Depends(get_cfg)) -> dict:
    return drafts.set_pin(stream_id, offer_id, False, db, s)


@router.post("/streams/{stream_id}/cancel")
def cancel(stream_id: int, db: Session = Depends(get_db), s: Settings = Depends(get_cfg)) -> dict:
    return drafts.cancel(stream_id, db, s)


@router.post("/streams/{stream_id}/push")
async def push(
    stream_id: int, client: KeitaroClient = Depends(get_client),
    db: Session = Depends(get_db), s: Settings = Depends(get_cfg),
) -> dict:
    return await drafts.push(stream_id, client, db, s)
