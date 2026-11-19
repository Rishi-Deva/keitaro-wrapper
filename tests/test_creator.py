"""Endpoint tests with Keitaro mocked by respx.

Multi-offer scenarios: the live tracker has only one offer and the test key cannot create offers,
so extra offers/stream entries are *derived* from real fixtures (same structure, new ids/names).
"""
import copy
import json

import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import Base
from app.keitaro.client import KeitaroClient
from app.main import create_app
from app.models import AuditLog, CreatedCampaign
from tests.conftest import CSRF, FAKE_API, FAKE_KEY, load

CID = 5001  # campaign id in campaign_created.json
GEO_SID, OFFER_SID = 800001, 800002  # stream ids in streams_two.json
REAL_OFFER = 7001


# ---------- fixture builders (derived from real responses) ----------

def offers_list(*ids: int) -> list[dict]:
    base = load("offers_list.json")[0]
    out = []
    for i, oid in enumerate(ids):
        o = copy.deepcopy(base)
        o["id"], o["name"] = oid, f"Example Offer {chr(65 + i)}"
        out.append(o)
    return out


def streams_with(offer_ids: list[int], shares: list[int] | None = None) -> list[dict]:
    streams = load("streams_two.json")
    for s in streams:
        s["campaign_id"] = CID
    tmpl = streams[1]["offers"][0]
    shares = shares or [100 // len(offer_ids)] * len(offer_ids) if offer_ids else []
    streams[1]["offers"] = [
        {**copy.deepcopy(tmpl), "id": 2080000 + i, "offer_id": oid, "share": sh}
        for i, (oid, sh) in enumerate(zip(offer_ids, shares))
    ]
    return streams


def campaign(name: str = "test_example_probe1") -> dict:
    c = load("campaign_created.json")
    c["name"] = name
    return c


# ---------- app + mocks ----------

@pytest.fixture
def api():
    with respx.mock(base_url=FAKE_API, assert_all_called=False) as r:
        yield r


@pytest.fixture
def app_client(api):
    kc = KeitaroClient(FAKE_API, FAKE_KEY, timeout=2, connect_timeout=1)
    app = create_app(settings=get_settings(), client=kc)
    with TestClient(app, headers=CSRF) as tc:
        Base.metadata.create_all(app.state.engine)
        tc.db = app.state.sessionmaker  # type: ignore[attr-defined]
        yield tc


def put_body(route) -> dict:
    return json.loads(route.calls.last.request.content)


# ---------- reference endpoints ----------

def test_health_and_config(app_client):
    assert app_client.get("/health").json() == {"status": "ok"}
    cfg = app_client.get("/api/config").json()
    assert cfg == {"campaign_prefix": "test_example_", "redirect_url": "https://google.com", "kt_link_enabled": True}


def test_geo_list(app_client):
    geo = app_client.get("/api/geo").json()
    assert {"MX", "AU", "RO"} <= set(geo) and len(geo) == 249


def test_offer_search(app_client, api):
    api.get("/offers").respond(json=offers_list(REAL_OFFER, 20001, 20002))
    assert [o["id"] for o in app_client.get("/api/offers").json()] == [REAL_OFFER, 20001, 20002]
    assert [o["id"] for o in app_client.get("/api/offers", params={"q": "offer b"}).json()] == [20001]
    assert [o["id"] for o in app_client.get("/api/offers", params={"q": "20002"}).json()] == [20002]
    assert app_client.get("/api/offers", params={"q": "nothing"}).json() == []
    assert len(app_client.get("/api/offers", params={"limit": 1}).json()) == 1


def test_campaign_list_marks_editable(app_client, api):
    api.get("/campaigns").respond(json=[campaign(), campaign("someone_else")])
    rows = app_client.get("/api/campaigns").json()
    assert [r["editable"] for r in rows] == [True, False]


# ---------- Part 1: creator ----------

def _mock_create(api, *, stream_fail_on: int | None = None, delete_fails: bool = False):
    api.get("/offers").respond(json=offers_list(REAL_OFFER))
    camp = api.post("/campaigns").respond(json=campaign())
    calls = {"n": 0}

    def stream_side_effect(request):
        calls["n"] += 1
        if stream_fail_on == calls["n"]:
            return httpx.Response(422, json={"filters": ["Invalid filter"]})
        body = json.loads(request.content)
        return httpx.Response(200, json={**body, "id": 7000 + calls["n"]})

    streams = api.post("/streams").mock(side_effect=stream_side_effect)
    delete = api.delete(f"/campaigns/{CID}").respond(
        502 if delete_fails else 200, json=[{"id": CID, "state": "deleted"}]
    )
    return camp, streams, delete


def test_create_campaign_full_flow(app_client, api):
    camp, streams, delete = _mock_create(api)
    r = app_client.post("/api/campaigns", json={"name": "demo mx", "geo": "mx", "offer_id": REAL_OFFER})
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["id"] == CID and data["name"] == "test_example_demo mx" and data["geo"] == "MX"

    sent = json.loads(camp.calls.last.request.content)
    assert sent["name"] == "test_example_demo mx"
    assert sent["alias"].startswith("test_example_demo_mx_")
    assert (sent["domain_id"], sent["group_id"], sent["traffic_source_id"]) == (101, 202, 303)

    s1, s2 = (json.loads(c.request.content) for c in streams.calls)
    assert s1["campaign_id"] == CID and s1["position"] == 1
    assert s1["schema"] == "redirect" and s1["action_type"] == "http"
    assert s1["action_payload"] == "https://google.com"
    assert s1["filters"] == [{"name": "country", "mode": "accept", "payload": ["MX"]}]
    assert s2["position"] == 2 and s2["schema"] == "landings" and s2["filters"] == []
    assert s2["offers"] == [{"offer_id": REAL_OFFER, "share": 100, "state": "active"}]
    assert not delete.called

    with app_client.db() as db:
        rec = db.query(CreatedCampaign).one()
        assert (rec.keitaro_id, rec.status, rec.geo_stream_id, rec.offer_stream_id) == (CID, "created", 7001, 7002)


def test_create_keeps_existing_prefix(app_client, api):
    camp, _, _ = _mock_create(api)
    app_client.post("/api/campaigns", json={"name": "test_example_x", "geo": "AU", "offer_id": REAL_OFFER})
    assert json.loads(camp.calls.last.request.content)["name"] == "test_example_x"


@pytest.mark.parametrize("geo", ["XX", "M1", "  "])
def test_create_invalid_geo(app_client, api, geo):
    camp = api.post("/campaigns")
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": geo, "offer_id": REAL_OFFER})
    assert r.status_code == 422
    assert not camp.called


def test_create_unknown_offer(app_client, api):
    api.get("/offers").respond(json=offers_list(REAL_OFFER))
    camp = api.post("/campaigns")
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": 1})
    assert r.status_code == 422 and r.json()["error"] == "offer_not_found"
    assert not camp.called


@pytest.mark.parametrize("name", ["", "   ", "bad<script>", "x" * 300])
def test_create_invalid_name(app_client, api, name):
    api.get("/offers").respond(json=offers_list(REAL_OFFER))
    camp = api.post("/campaigns")
    r = app_client.post("/api/campaigns", json={"name": name, "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 422
    assert not camp.called


def test_create_keitaro_validation_passthrough(app_client, api):
    api.get("/offers").respond(json=offers_list(REAL_OFFER))
    api.post("/campaigns").respond(422, json=load("errors.json")["alias_required_422"])
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 422
    assert r.json()["fields"] == {"alias": ["Alias is required"]}


@pytest.mark.parametrize("fail_on", [1, 2])
def test_create_rolls_back_when_stream_fails(app_client, api, fail_on):
    _, _, delete = _mock_create(api, stream_fail_on=fail_on)
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 502 and r.json()["error"] == "stream_failed"
    assert "rolled back" in r.json()["detail"]
    assert delete.called
    with app_client.db() as db:
        assert db.query(CreatedCampaign).one().status == "rolled_back"
        assert db.query(AuditLog).filter_by(action="campaign_rolled_back").count() == 1


def test_create_rollback_failure_is_reported(app_client, api):
    _mock_create(api, stream_fail_on=2, delete_fails=True)
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 502 and "ROLLBACK FAILED" in r.json()["detail"]
    with app_client.db() as db:
        assert db.query(CreatedCampaign).one().status == "rollback_failed"


def test_create_timeout_maps_to_504(app_client, api):
    api.get("/offers").mock(side_effect=httpx.ReadTimeout("slow"))
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 504 and r.json()["error"] == "keitaro_unavailable"
    assert FAKE_KEY not in r.text and "tracker.test" not in r.text


# ---------- review finding 5: unclear outcome of POST campaigns ----------

def _post_times_out(api, *, created: bool, lookup_fails: bool = False, delete_status: int = 200):
    api.get("/offers").respond(json=offers_list(REAL_OFFER))
    seen = {}

    def post(request):
        seen["alias"] = json.loads(request.content)["alias"]
        raise httpx.ReadTimeout("slow", request=request)

    api.post("/campaigns").mock(side_effect=post)

    def listing(request):
        if lookup_fails:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(200, json=[{"id": CID, "alias": seen["alias"], "name": "x"}] if created else [])

    api.get("/campaigns").mock(side_effect=listing)
    delete = api.delete(f"/campaigns/{CID}").respond(delete_status, json=[{"id": CID, "state": "deleted"}])
    streams = api.post("/streams")
    return delete, streams


def test_timeout_but_created_is_found_by_alias_and_rolled_back(app_client, api):
    delete, streams = _post_times_out(api, created=True)
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 502 and r.json()["error"] == "create_unclear"
    assert "rolled back" in r.json()["detail"]
    assert delete.called and not streams.called
    with app_client.db() as db:
        rec = db.query(CreatedCampaign).one()
        assert (rec.keitaro_id, rec.status) == (CID, "rolled_back")


def test_timeout_and_not_created(app_client, api):
    delete, _ = _post_times_out(api, created=False)
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 504 and r.json()["error"] == "create_failed"
    assert not delete.called
    with app_client.db() as db:
        assert db.query(CreatedCampaign).one().status == "failed"


def test_timeout_and_lookup_fails_reports_alias(app_client, api):
    delete, _ = _post_times_out(api, created=True, lookup_fails=True)
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 504 and r.json()["error"] == "create_unknown"
    with app_client.db() as db:
        rec = db.query(CreatedCampaign).one()
        assert rec.status == "unknown" and rec.alias in r.json()["detail"]
    assert not delete.called


def test_timeout_created_but_rollback_fails(app_client, api):
    _post_times_out(api, created=True, delete_status=502)
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 502 and "ROLLBACK FAILED" in r.json()["detail"]
    with app_client.db() as db:
        assert db.query(CreatedCampaign).one().status == "rollback_failed"


def test_response_without_id_is_handled_not_500(app_client, api):
    api.get("/offers").respond(json=offers_list(REAL_OFFER))
    seen = {}

    def post(request):
        seen["alias"] = json.loads(request.content)["alias"]
        return httpx.Response(200, json={"name": "x"})  # no id

    api.post("/campaigns").mock(side_effect=post)
    api.get("/campaigns").mock(side_effect=lambda req: httpx.Response(200, json=[{"id": CID, "alias": seen["alias"]}]))
    delete = api.delete(f"/campaigns/{CID}").respond(json=[{"id": CID, "state": "deleted"}])
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 502 and r.json()["error"] == "create_unclear"
    assert delete.called


def test_deleted_offer_rejected_on_create(app_client, api):
    offers = offers_list(REAL_OFFER)
    offers[0]["state"] = "deleted"
    api.get("/offers").respond(json=offers)
    camp = api.post("/campaigns")
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 422 and not camp.called


def test_create_without_csrf_header_rejected(app_client, api):
    camp = api.post("/campaigns")
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER},
                        headers={"X-Requested-With": ""})
    assert r.status_code == 403 and not camp.called


def test_timeout_and_found_entry_without_id_is_unknown_not_500(app_client, api):
    api.get("/offers").respond(json=offers_list(REAL_OFFER))
    seen = {}

    def post(request):
        seen["alias"] = json.loads(request.content)["alias"]
        raise httpx.ReadTimeout("slow", request=request)

    api.post("/campaigns").mock(side_effect=post)
    api.get("/campaigns").mock(side_effect=lambda req: httpx.Response(
        200, json=["junk", {"alias": seen["alias"], "name": "no id here"}]))
    r = app_client.post("/api/campaigns", json={"name": "a", "geo": "MX", "offer_id": REAL_OFFER})
    assert r.status_code == 504 and r.json()["error"] == "create_unknown"
    with app_client.db() as db:
        assert db.query(CreatedCampaign).one().status == "unknown"
