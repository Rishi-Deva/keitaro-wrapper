import logging

import httpx
import pytest
import respx

from app.keitaro.client import KeitaroClient
from app.keitaro.errors import (
    KeitaroAuthError,
    KeitaroBadResponse,
    KeitaroNotFound,
    KeitaroUnavailable,
    KeitaroValidationError,
)
from tests.conftest import FAKE_API, FAKE_KEY, load


@pytest.fixture
async def client():
    c = KeitaroClient(FAKE_API, FAKE_KEY, timeout=2, connect_timeout=1)
    yield c
    await c.aclose()


@pytest.fixture
def api():
    with respx.mock(base_url=FAKE_API, assert_all_called=True) as r:
        yield r


def test_empty_key_rejected():
    with pytest.raises(ValueError):
        KeitaroClient(FAKE_API, "")


async def test_sends_api_key_header(client, api):
    route = api.get("/offers").respond(json=load("offers_list.json"))
    offers = await client.list_offers()
    assert offers[0]["id"] == 7001
    assert route.calls.last.request.headers["Api-Key"] == FAKE_KEY


async def test_campaign_groups_use_plural_type(client, api):
    route = api.get("/groups", params={"type": "campaigns"}).respond(json=load("groups_campaigns.json"))
    groups = await client.list_campaign_groups()
    assert any(g["id"] == 202 for g in groups)
    assert route.called


async def test_reference_lists(client, api):
    api.get("/domains").respond(json=load("domains_list.json"))
    api.get("/traffic_sources").respond(json=load("traffic_sources_list.json"))
    api.get("/campaigns").respond(json=load("campaigns_list.json"))
    assert (await client.list_domains())[0]["id"] == 101
    assert (await client.list_traffic_sources())[0]["id"] == 303
    assert len(await client.list_campaigns()) == 3


async def test_streams_parsed(client, api):
    api.get("/campaigns/7/streams").respond(json=load("streams_two.json"))
    streams = await client.list_streams(7)
    geo, offer = streams
    assert geo["filters"][0] == {**geo["filters"][0], "name": "country", "mode": "accept", "payload": ["AU"]}
    assert offer["offers"][0]["share"] == 100


async def test_create_campaign_posts_json(client, api):
    route = api.post("/campaigns").respond(json=load("campaign_created.json"))
    body = {"name": "test_example_x", "alias": "test_example_x"}
    res = await client.create_campaign(body)
    assert res["id"] == 5001
    assert route.calls.last.request.read() == httpx.Request("POST", "/", json=body).read()


async def test_update_stream_put(client, api):
    route = api.put("/streams/11").respond(json={"id": 11, "offers": []})
    res = await client.update_stream(11, {"offers": []})
    assert res["id"] == 11 and route.called


async def test_delete_returns_list_ok(client, api):
    api.delete("/campaigns/5").respond(json=[{"id": 5, "state": "deleted"}])
    await client.delete_campaign(5)


async def test_get_campaign_unwraps(client, api):
    api.get("/campaigns/5").respond(json=load("campaign_created.json"))
    assert (await client.get_campaign(5))["id"] == 5001


# ---------- errors ----------

async def test_422_validation_with_details(client, api):
    err = load("errors.json")["alias_required_422"]
    api.post("/campaigns").respond(422, json=err)
    with pytest.raises(KeitaroValidationError) as e:
        await client.create_campaign({"name": "x"})
    assert e.value.status == 422
    assert e.value.details == err
    assert "Alias is required" in str(e.value)


async def test_400_error_text(client, api):
    api.get("/groups").respond(400, json=load("errors.json")["param_required_400"])
    with pytest.raises(KeitaroValidationError, match="Param 'type' is required"):
        await client._request("GET", "groups")


async def test_403_permission(client, api):
    api.post("/offers").respond(403, json=load("errors.json")["no_permission_403"])
    with pytest.raises(KeitaroAuthError, match="no permission"):
        await client._request("POST", "offers", json={})


async def test_401(client, api):
    api.get("/offers").respond(401, text="Unauthorized")
    with pytest.raises(KeitaroAuthError):
        await client.list_offers()


async def test_404_html_body(client, api):
    api.get("/campaigns/999").respond(
        404, text=load("errors.json")["nginx_404_html"], headers={"content-type": "text/html; charset=utf-8"}
    )
    with pytest.raises(KeitaroNotFound) as e:
        await client.get_campaign(999)
    assert e.value.details is None


async def test_500(client, api):
    api.get("/offers").respond(502, text="Bad Gateway")
    with pytest.raises(KeitaroUnavailable) as e:
        await client.list_offers()
    assert e.value.status == 502


async def test_timeout(client, api):
    api.get("/offers").mock(side_effect=httpx.ReadTimeout("slow"))
    with pytest.raises(KeitaroUnavailable, match="timeout"):
        await client.list_offers()


async def test_connect_error(client, api):
    api.get("/offers").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(KeitaroUnavailable, match="unreachable"):
        await client.list_offers()


async def test_2xx_non_json(client, api):
    api.get("/offers").respond(200, text="<html>login</html>", headers={"content-type": "text/html"})
    with pytest.raises(KeitaroBadResponse):
        await client.list_offers()


async def test_wrong_shape(client, api):
    api.get("/offers").respond(json={"not": "a list"})
    with pytest.raises(KeitaroBadResponse):
        await client.list_offers()


async def test_errors_and_logs_never_leak_key_or_host(client, api, caplog):
    caplog.set_level(logging.DEBUG)
    api.get("/offers").mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(KeitaroUnavailable) as e:
        await client.list_offers()
    text = str(e.value) + caplog.text
    assert FAKE_KEY not in text
    assert "tracker.test" not in str(e.value)
    assert "tracker.test" not in "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("app"))


async def test_ids_are_coerced_to_int(client, api):
    # prevents path injection like "1/../../x"
    with pytest.raises(ValueError):
        await client.get_campaign("1/../offers")  # type: ignore[arg-type]


async def test_error_details_drop_stacktrace(client, api):
    api.post("/campaigns").respond(400, json={"error": "Bad thing", "stacktrace": "/var/www/x.php:12 host.internal",
                                             "name": ["Name is invalid"], "weird": {"nested": 1}})
    with pytest.raises(KeitaroValidationError) as e:
        await client.create_campaign({})
    assert e.value.details == {"name": ["Name is invalid"]}
    assert "stacktrace" not in str(e.value) and "/var/www" not in str(e.value)
