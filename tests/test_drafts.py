"""Editor draft/publish scenarios from the customer's video (requirements 1-8), on a stateful fake Keitaro.

The fake is seeded from real fixtures; our campaign's offer stream starts with 3 offers 34/33/33.
"""
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import Base
from app.keitaro.client import KeitaroClient
from app.main import create_app
from app.models import AuditLog, CreatedCampaign
from tests.conftest import CSRF, FAKE_API, FAKE_KEY
from tests.fake_keitaro import FIRST_OFFER, FOREIGN, OUR, FakeKeitaro

A, B, C, D, E = (FIRST_OFFER + i for i in range(5))


@pytest.fixture
def kt():
    return FakeKeitaro(prefix="test_example_")


@pytest.fixture
def c(kt):
    client = KeitaroClient(FAKE_API, FAKE_KEY, transport=kt.transport())
    app = create_app(settings=get_settings(), client=client)
    with TestClient(app, headers=CSRF) as tc:
        Base.metadata.create_all(app.state.engine)
        tc.db = app.state.sessionmaker  # type: ignore[attr-defined]
        tc.kt = kt  # type: ignore[attr-defined]
        yield tc


def fetch(c, cid=OUR):
    r = c.post(f"/api/campaigns/{cid}/fetch")
    assert r.status_code == 200, r.text
    return r.json()


def offer_stream(view):
    return next(s for s in view["streams"] if s["has_offers"])


def shares(stream, include_removed=False):
    return [(o["offer_id"], o["share"]) for o in stream["offers"] if include_removed or not o["removed"]]


def removed(stream):
    return [o["offer_id"] for o in stream["offers"] if o["removed"]]


def sid(c, cid=OUR):
    return c.kt.offer_stream(cid)["id"]


def op(c, method, path, ok=200, **kw):
    r = c.request(method, path, **kw)
    assert r.status_code == ok, r.text
    return r.json()


# ---------- 1. Fetch ----------

def test_fetch_loads_streams_into_db(c):
    assert c.get(f"/api/campaigns/{OUR}/streams").json()["fetched"] is False
    v = fetch(c)
    assert v["editable"] is True
    assert v["kt_url"] == f"https://tracker.test/admin/#!/campaign/{OUR}/main"
    geo, off = v["streams"]
    # stream without offers: just a stream, no filter/redirect parsing
    assert geo["has_offers"] is False and geo["offers"] == [] and "filters" not in geo
    assert shares(off) == [(A, 34), (B, 33), (C, 33)]
    assert off["dirty"] is False and off["share_valid"] is True
    assert all(o["in_keitaro"] for o in off["offers"])
    # served from DB afterwards, no Keitaro call
    n = len(c.kt.requests)
    assert c.get(f"/api/campaigns/{OUR}/streams").json()["streams"] == v["streams"]
    assert len(c.kt.requests) == n
    assert c.kt.writes() == []


def test_fetch_foreign_campaign_allowed_read_only(c):
    v = fetch(c, FOREIGN)
    assert v["editable"] is False and offer_stream(v)["can_push"] is False
    assert c.kt.writes() == []


def test_fetch_unknown_campaign(c):
    assert c.post("/api/campaigns/999/fetch").status_code == 404


def test_refetch_is_idempotent(c):
    a = fetch(c)
    b = fetch(c)
    assert [s["offers"] for s in a["streams"]] == [s["offers"] for s in b["streams"]]
    with c.db() as db:
        assert db.query(AuditLog).filter_by(action="fetch").count() == 1  # second fetch changed nothing


# ---------- 2. Add offer: DB only, shares at once, yellow ----------

def test_add_offer_changes_only_db(c):
    fetch(c)
    s = op(c, "POST", f"/api/streams/{sid(c)}/offers", json={"offer_id": D})
    assert shares(s) == [(A, 25), (B, 25), (C, 25), (D, 25)]  # video: 3 offers -> 4 x 25
    assert s["dirty"] is True and s["share_total"] == 100
    assert c.kt.kt_offers() == [(A, 34), (B, 33), (C, 33)]  # Keitaro untouched
    assert c.kt.writes() == []
    # persisted
    v = c.get(f"/api/campaigns/{OUR}/streams").json()
    assert offer_stream(v)["dirty"] is True and len(offer_stream(v)["offers"]) == 4


def test_add_offer_errors(c):
    fetch(c)
    assert c.post(f"/api/streams/{sid(c)}/offers", json={"offer_id": A}).json()["error"] == "duplicate_offer"
    assert c.post(f"/api/streams/{sid(c)}/offers", json={"offer_id": 1}).status_code == 422
    geo_sid = c.kt.geo_stream()["id"]
    assert c.post(f"/api/streams/{geo_sid}/offers", json={"offer_id": D}).json()["error"] == "stream_has_no_offers"
    assert c.post("/api/streams/424242/offers", json={"offer_id": D}).json()["error"] == "stream_not_fetched"


# ---------- 3. Push ----------

def test_push_publishes_and_clears_yellow(c):
    fetch(c)
    op(c, "POST", f"/api/streams/{sid(c)}/offers", json={"offer_id": D})
    s = op(c, "POST", f"/api/streams/{sid(c)}/push")
    assert s["dirty"] is False and all(o["in_keitaro"] for o in s["offers"])
    assert c.kt.kt_offers() == [(A, 25), (B, 25), (C, 25), (D, 25)]
    assert c.kt.writes() == [("PUT", f"streams/{sid(c)}")]
    # re-fetch shows the same, not yellow
    assert offer_stream(fetch(c))["dirty"] is False


def test_push_forbidden_for_foreign_campaign(c):
    fetch(c, FOREIGN)
    fsid = sid(c, FOREIGN)
    op(c, "POST", f"/api/streams/{fsid}/offers", json={"offer_id": D})  # draft is allowed
    r = c.post(f"/api/streams/{fsid}/push")
    assert r.status_code == 403 and r.json()["error"] == "push_forbidden"
    assert c.kt.writes() == []


def test_push_checks_live_name_not_cached(c):
    fetch(c)
    c.kt.campaigns[OUR]["name"] = "renamed_by_someone"  # no longer ours in Keitaro
    op(c, "POST", f"/api/streams/{sid(c)}/offers", json={"offer_id": D})
    assert c.post(f"/api/streams/{sid(c)}/push").status_code == 403
    assert c.kt.writes() == []


def test_push_refuses_if_changed_in_keitaro_since_fetch(c):
    fetch(c)
    op(c, "POST", f"/api/streams/{sid(c)}/offers", json={"offer_id": D})
    c.kt.drop_offer_in_kt(B)
    r = c.post(f"/api/streams/{sid(c)}/push")
    assert r.status_code == 409 and r.json()["error"] == "changed_in_keitaro"
    assert c.kt.writes() == []


def _draft_d(c):
    fetch(c)
    op(c, "POST", f"/api/streams/{sid(c)}/offers", json={"offer_id": D})


def test_push_timeout_after_save_is_confirmed_by_reread(c):
    _draft_d(c)
    c.kt.put_failure = "timeout_after_save"
    v = op(c, "POST", f"/api/streams/{sid(c)}/push")
    assert v["dirty"] is False and "confirmed" in v["note"]
    assert c.kt.kt_offers() == [(A, 25), (B, 25), (C, 25), (D, 25)]
    assert c.kt.writes() == [("PUT", f"streams/{sid(c)}")]


def test_push_timeout_before_save_keeps_draft_and_retry_works(c):
    _draft_d(c)
    c.kt.put_failure = "timeout_before_save"
    r = c.post(f"/api/streams/{sid(c)}/push")
    assert r.status_code == 504 and r.json()["error"] == "push_not_applied"
    assert offer_stream(c.get(f"/api/campaigns/{OUR}/streams").json())["dirty"] is True
    v = op(c, "POST", f"/api/streams/{sid(c)}/push")  # safe retry
    assert v["dirty"] is False
    assert c.kt.kt_offers() == [(A, 25), (B, 25), (C, 25), (D, 25)]


def test_push_retry_after_unknown_outcome_does_not_send_twice(c):
    _draft_d(c)
    c.kt.put_failure = "timeout_after_save_reads_down"
    r = c.post(f"/api/streams/{sid(c)}/push")
    assert r.status_code == 504 and r.json()["error"] == "push_unknown"
    assert offer_stream(c.get(f"/api/campaigns/{OUR}/streams").json())["dirty"] is True
    c.kt.fail_reads = False
    v = op(c, "POST", f"/api/streams/{sid(c)}/push")
    assert v["dirty"] is False and "nothing was sent" in v["note"]
    assert c.kt.writes() == [("PUT", f"streams/{sid(c)}")]  # exactly one PUT in total


def test_push_garbage_response_but_saved_is_confirmed(c):
    _draft_d(c)
    c.kt.put_failure = "garbage_after_save"
    v = op(c, "POST", f"/api/streams/{sid(c)}/push")
    assert v["dirty"] is False and "confirmed" in v["note"]


def test_push_ends_in_third_state_is_reported_and_draft_kept(c):
    _draft_d(c)
    c.kt.put_failure = "other_state"
    r = c.post(f"/api/streams/{sid(c)}/push")
    assert r.status_code == 409 and r.json()["error"] == "push_inconsistent"
    v = offer_stream(c.get(f"/api/campaigns/{OUR}/streams").json())
    assert v["dirty"] is True and len(v["offers"]) == 4


# ---------- offer state from Keitaro (review finding 1) ----------

def test_push_keeps_disabled_state_from_keitaro(c):
    c.kt.offer_stream()["offers"][1]["state"] = "disabled"
    v = offer_stream(fetch(c))
    assert [o["state"] for o in v["offers"]] == ["active", "disabled", "active"]
    op(c, "POST", f"/api/streams/{sid(c)}/offers", json={"offer_id": D})
    op(c, "POST", f"/api/streams/{sid(c)}/push")
    sent = {o["offer_id"]: o["state"] for o in c.kt.offer_stream()["offers"]}
    assert sent == {A: "active", B: "disabled", C: "active", D: "active"}


def test_disabling_in_keitaro_after_fetch_blocks_push(c):
    _draft_d(c)
    c.kt.offer_stream()["offers"][0]["state"] = "disabled"
    r = c.post(f"/api/streams/{sid(c)}/push")
    assert r.status_code == 409 and r.json()["error"] == "changed_in_keitaro"
    assert c.kt.writes() == []


def test_cancel_restores_state(c):
    c.kt.offer_stream()["offers"][1]["state"] = "disabled"
    fetch(c)
    op(c, "POST", f"/api/streams/{sid(c)}/offers/{B}/remove")
    v = op(c, "POST", f"/api/streams/{sid(c)}/cancel")
    assert next(o for o in v["offers"] if o["offer_id"] == B)["state"] == "disabled"


def test_push_sends_full_active_list_without_removed(c):
    fetch(c)
    op(c, "POST", f"/api/streams/{sid(c)}/offers/{B}/remove")
    op(c, "POST", f"/api/streams/{sid(c)}/push")
    assert c.kt.kt_offers() == [(A, 50), (C, 50)]


# ---------- 4. Cancel ----------

def test_cancel_reverts_all_draft_changes(c):
    v0 = offer_stream(fetch(c))
    s = sid(c)
    op(c, "POST", f"/api/streams/{s}/offers", json={"offer_id": D})
    op(c, "POST", f"/api/streams/{s}/offers/{A}/pin")
    op(c, "POST", f"/api/streams/{s}/offers/{B}/remove")
    v = op(c, "POST", f"/api/streams/{s}/cancel")
    assert v["dirty"] is False
    assert v["offers"] == v0["offers"]  # added offer gone, shares/pins/removed back
    assert c.kt.writes() == []


def test_cancel_after_push_restores_pushed_state(c):
    fetch(c)
    s = sid(c)
    op(c, "POST", f"/api/streams/{s}/offers/{B}/remove")
    op(c, "POST", f"/api/streams/{s}/push")
    op(c, "POST", f"/api/streams/{s}/offers/{B}/bring-back")
    v = op(c, "POST", f"/api/streams/{s}/cancel")
    assert shares(v) == [(A, 50), (C, 50)] and removed(v) == [B]


# ---------- 5. Remove / Bring back ----------

def test_remove_then_push_archives_offer(c):
    fetch(c)
    s = sid(c)
    v = op(c, "POST", f"/api/streams/{s}/offers/{B}/remove")
    assert shares(v, include_removed=True) == [(A, 50), (B, 0), (C, 50)]
    assert removed(v) == [B] and v["dirty"] is True
    v = op(c, "POST", f"/api/streams/{s}/push")
    assert c.kt.kt_offers() == [(A, 50), (C, 50)]  # gone from Keitaro
    assert removed(v) == [B] and v["dirty"] is False  # still with us, grey
    b = next(o for o in v["offers"] if o["offer_id"] == B)
    assert b["in_keitaro"] is False and b["share"] == 0


def test_bring_back_then_push_returns_offer(c):
    fetch(c)
    s = sid(c)
    op(c, "POST", f"/api/streams/{s}/offers/{B}/remove")
    op(c, "POST", f"/api/streams/{s}/push")
    v = op(c, "POST", f"/api/streams/{s}/offers/{B}/bring-back")
    assert v["dirty"] is True and removed(v) == []
    assert sorted(shares(v)) == [(A, 34), (B, 33), (C, 33)]
    op(c, "POST", f"/api/streams/{s}/push")
    assert sorted(c.kt.kt_offers()) == [(A, 34), (B, 33), (C, 33)]


def test_remove_and_bring_back_without_push_is_not_dirty(c):
    fetch(c)
    s = sid(c)
    op(c, "POST", f"/api/streams/{s}/offers/{B}/remove")
    v = op(c, "POST", f"/api/streams/{s}/offers/{B}/bring-back")
    assert v["dirty"] is False  # same as published


def test_cannot_remove_last_active(c):
    fetch(c)
    s = sid(c)
    op(c, "POST", f"/api/streams/{s}/offers/{A}/remove")
    op(c, "POST", f"/api/streams/{s}/offers/{B}/remove")
    r = c.post(f"/api/streams/{s}/offers/{C}/remove")
    assert r.status_code == 409 and r.json()["error"] == "no_active_offers"
    v = c.get(f"/api/campaigns/{OUR}/streams").json()
    assert shares(offer_stream(v)) == [(C, 100)]  # unchanged


def test_remove_errors(c):
    fetch(c)
    s = sid(c)
    assert c.post(f"/api/streams/{s}/offers/{D}/remove").json()["error"] == "offer_not_in_stream"
    assert c.post(f"/api/streams/{s}/offers/{A}/bring-back").json()["error"] == "not_removed"
    op(c, "POST", f"/api/streams/{s}/offers/{A}/remove")
    assert c.post(f"/api/streams/{s}/offers/{A}/remove").json()["error"] == "already_removed"
    assert c.post(f"/api/streams/{s}/offers", json={"offer_id": A}).json()["error"] == "offer_removed_use_bring_back"


# ---------- 6. Offer deleted directly in Keitaro ----------

def test_offer_deleted_in_kt_becomes_grey_after_fetch(c):
    fetch(c)
    c.kt.drop_offer_in_kt(B)
    v = offer_stream(fetch(c))
    assert removed(v) == [B]
    assert shares(v) == [(A, 50), (C, 50)] and v["dirty"] is False
    s = sid(c)
    v = op(c, "POST", f"/api/streams/{s}/offers/{B}/bring-back")
    assert v["dirty"] is True
    op(c, "POST", f"/api/streams/{s}/push")
    assert sorted(o for o, _ in c.kt.kt_offers()) == [A, B, C]


def test_fetch_does_not_overwrite_dirty_stream(c):
    fetch(c)
    s = sid(c)
    op(c, "POST", f"/api/streams/{s}/offers", json={"offer_id": D})
    c.kt.drop_offer_in_kt(B)
    v = fetch(c)
    assert any("Push or Cancel first" in w for w in v["warnings"])
    st = offer_stream(v)
    assert st["dirty"] is True and len(st["offers"]) == 4 and removed(st) == []  # draft kept as is


def test_offer_added_in_kt_appears_after_fetch(c):
    fetch(c)
    st = c.kt.offer_stream()
    st["offers"] = st["offers"] + [{**st["offers"][0], "offer_id": E, "share": 0}]
    v = offer_stream(fetch(c))
    assert (E, 0) in shares(v)
    # Keitaro data are shown as is: an active offer with 0% -> stream marked invalid (red), Push refused
    assert v["share_valid"] is False and v["dirty"] is False
    r = c.post(f"/api/streams/{sid(c)}/push")
    assert r.status_code == 409 and r.json()["error"] == "invalid_shares"
    # any draft operation rebalances and fixes it
    v = op(c, "POST", f"/api/streams/{sid(c)}/offers/{B}/remove")
    assert v["share_valid"] is True and v["share_total"] == 100


# ---------- 7. Pin ----------

def test_video_pin_25_then_bring_back(c):
    fetch(c)
    s = sid(c)
    op(c, "POST", f"/api/streams/{s}/offers", json={"offer_id": D})           # 25 x 4
    op(c, "POST", f"/api/streams/{s}/offers/{A}/pin")                          # A pinned at 25
    v = op(c, "POST", f"/api/streams/{s}/offers/{B}/remove")                   # 25 | 0 | 38 | 37
    assert shares(v) == [(A, 25), (C, 38), (D, 37)]
    v = op(c, "POST", f"/api/streams/{s}/offers/{B}/bring-back")               # A keeps 25
    assert shares(v) == [(A, 25), (B, 25), (C, 25), (D, 25)]
    assert next(o for o in v["offers"] if o["offer_id"] == A)["pinned"] is True


def test_pin_remainder_not_divisible(c):
    fetch(c)
    s = sid(c)
    v = op(c, "POST", f"/api/streams/{s}/offers/{A}/pin")   # A pinned at 34
    assert shares(v) == [(A, 34), (B, 33), (C, 33)]
    v = op(c, "POST", f"/api/streams/{s}/offers", json={"offer_id": D})  # 66 / 3 = 22
    assert shares(v) == [(A, 34), (B, 22), (C, 22), (D, 22)]
    v = op(c, "POST", f"/api/streams/{s}/offers", json={"offer_id": E})  # 66 / 4 -> 17,17,16,16
    assert shares(v) == [(A, 34), (B, 17), (C, 17), (D, 16), (E, 16)]


def test_all_pinned_cannot_remove_or_add(c):
    fetch(c)
    s = sid(c)
    for o in (A, B, C):
        op(c, "POST", f"/api/streams/{s}/offers/{o}/pin")
    r = c.post(f"/api/streams/{s}/offers/{B}/remove")
    assert r.status_code == 409 and r.json()["error"] == "all_pinned_not_100"
    r = c.post(f"/api/streams/{s}/offers", json={"offer_id": D})
    assert r.status_code == 409 and r.json()["error"] == "not_enough_for_unpinned"
    v = offer_stream(c.get(f"/api/campaigns/{OUR}/streams").json())
    assert shares(v) == [(A, 34), (B, 33), (C, 33)] and len(v["offers"]) == 3  # nothing changed


def test_unpin_rebalances(c):
    fetch(c)
    s = sid(c)
    op(c, "POST", f"/api/streams/{s}/offers/{B}/remove")       # 50 / 50
    op(c, "POST", f"/api/streams/{s}/offers/{A}/pin")          # A=50 pinned
    v = op(c, "POST", f"/api/streams/{s}/offers/{B}/bring-back")  # 50 | 25 | 25
    assert shares(v) == [(A, 50), (B, 25), (C, 25)]
    v = op(c, "POST", f"/api/streams/{s}/offers/{A}/unpin")
    assert shares(v) == [(A, 34), (B, 33), (C, 33)]


def test_pin_errors(c):
    fetch(c)
    s = sid(c)
    op(c, "POST", f"/api/streams/{s}/offers/{B}/remove")
    assert c.post(f"/api/streams/{s}/offers/{B}/pin").json()["error"] == "offer_removed"
    v = op(c, "POST", f"/api/streams/{s}/offers/{A}/pin")
    assert op(c, "POST", f"/api/streams/{s}/offers/{A}/pin") == v  # idempotent


def test_remove_pinned_unpins_it(c):
    fetch(c)
    s = sid(c)
    op(c, "POST", f"/api/streams/{s}/offers/{A}/pin")
    v = op(c, "POST", f"/api/streams/{s}/offers/{A}/remove")
    a = next(o for o in v["offers"] if o["offer_id"] == A)
    assert a["pinned"] is False and a["share"] == 0
    assert shares(v) == [(B, 50), (C, 50)]


def test_shares_always_100_through_random_sequence(c):
    import random
    rnd = random.Random(7)
    fetch(c)
    s = sid(c)
    for _ in range(80):
        v = offer_stream(c.get(f"/api/campaigns/{OUR}/streams").json())
        o = rnd.choice([A, B, C, D, E])
        row = next((x for x in v["offers"] if x["offer_id"] == o), None)
        if row is None:
            c.post(f"/api/streams/{s}/offers", json={"offer_id": o})
        elif row["removed"]:
            c.post(f"/api/streams/{s}/offers/{o}/bring-back")
        else:
            c.post(f"/api/streams/{s}/offers/{o}/{rnd.choice(['remove', 'pin', 'unpin'])}")
        v = offer_stream(c.get(f"/api/campaigns/{OUR}/streams").json())
        assert v["share_total"] == 100, v
        assert all(x["share"] == 0 for x in v["offers"] if x["removed"])
    assert c.kt.writes() == []


# ---------- campaign delete ----------

def test_delete_our_campaign(c):
    fetch(c)
    r = op(c, "DELETE", f"/api/campaigns/{OUR}")
    assert r == {"id": OUR, "deleted": True, "already_deleted_in_keitaro": False}
    assert c.kt.campaigns[OUR]["state"] == "deleted"
    assert c.get(f"/api/campaigns/{OUR}/streams").json()["streams"] == []


def test_delete_foreign_forbidden(c):
    r = c.delete(f"/api/campaigns/{FOREIGN}")
    assert r.status_code == 403
    assert c.kt.writes() == []


def test_delete_already_deleted_uses_local_record(c):
    # like campaign 900003: deleted in Keitaro by a script, local record still "created"
    c.kt.campaigns[OUR]["state"] = "deleted"
    with c.db() as db:
        db.add(CreatedCampaign(keitaro_id=OUR, name="test_example_seed", alias="a", geo="MX",
                               offer_id=A, status="created"))
        db.commit()
    r = op(c, "DELETE", f"/api/campaigns/{OUR}")
    assert r["already_deleted_in_keitaro"] is True
    assert c.kt.writes() == []  # no DELETE sent for an already deleted campaign
    with c.db() as db:
        assert db.query(CreatedCampaign).one().status == "deleted"


def test_delete_unknown_404(c):
    assert c.delete("/api/campaigns/999").status_code == 404


# ---------- review findings 6, 7, 10 ----------

def test_pin_on_zero_share_is_refused(c):
    st = c.kt.offer_stream()
    for o, sh in zip(st["offers"], [0, 50, 50]):
        o["share"] = sh
    fetch(c)
    r = c.post(f"/api/streams/{sid(c)}/offers/{A}/pin")
    assert r.status_code == 409 and r.json()["error"] == "bad_pinned_share"


def test_duplicate_offer_in_keitaro_does_not_crash_fetch(c):
    st = c.kt.offer_stream()
    st["offers"].append(dict(st["offers"][0]))
    v = fetch(c)
    assert any("appears twice" in w for w in v["warnings"])
    assert [o["offer_id"] for o in offer_stream(v)["offers"]] == [A, B, C]


def test_malformed_stream_from_keitaro_is_502_not_500(c):
    c.kt.offer_stream()["offers"][0].pop("offer_id")
    r = c.post(f"/api/campaigns/{OUR}/fetch")
    assert r.status_code == 502 and r.json()["error"] == "keitaro_error"
    assert c.get(f"/api/campaigns/{OUR}/streams").json()["streams"] == []  # nothing half-written


def test_bring_back_checks_offer_still_exists(c):
    fetch(c)
    op(c, "POST", f"/api/streams/{sid(c)}/offers/{B}/remove")
    c.kt.offers = [o for o in c.kt.offers if o["id"] != B]
    r = c.post(f"/api/streams/{sid(c)}/offers/{B}/bring-back")
    assert r.status_code == 422 and r.json()["error"] == "offer_not_found"


def test_deleted_offer_cannot_be_added(c):
    fetch(c)
    next(o for o in c.kt.offers if o["id"] == D)["state"] = "deleted"
    r = c.post(f"/api/streams/{sid(c)}/offers", json={"offer_id": D})
    assert r.status_code == 422


# ---------- review finding 4: CSRF / Host ----------

def test_state_changing_call_without_csrf_header_is_rejected(c):
    fetch(c)
    for path in (f"/api/streams/{sid(c)}/push", f"/api/campaigns/{OUR}/fetch", f"/api/streams/{sid(c)}/cancel"):
        r = c.post(path, headers={"X-Requested-With": ""})
        assert r.status_code == 403 and r.json()["error"] == "csrf"
    assert c.delete(f"/api/campaigns/{OUR}", headers={"X-Requested-With": "x"}).status_code == 403
    assert c.kt.writes() == []
    assert c.get(f"/api/campaigns/{OUR}/streams", headers={"X-Requested-With": ""}).status_code == 200  # reads ok


def test_foreign_host_header_is_rejected(c):
    r = c.get("/api/geo", headers={"Host": "evil.example"})
    assert r.status_code == 400 and r.json()["error"] == "bad_host"
    assert c.get("/api/geo", headers={"Host": "127.0.0.1:8000"}).status_code == 200


# ---------- second review round ----------

def test_duplicate_in_keitaro_then_push_works_and_stores_once(c):
    st = c.kt.offer_stream()
    st["offers"].append(dict(st["offers"][0]))
    fetch(c)
    op(c, "POST", f"/api/streams/{sid(c)}/offers", json={"offer_id": D})
    v = op(c, "POST", f"/api/streams/{sid(c)}/push")
    assert v["dirty"] is False
    assert c.kt.kt_offers() == [(A, 25), (B, 25), (C, 25), (D, 25)]  # duplicate gone


def test_push_5xx_is_reported_as_answer_not_timeout(c):
    _draft_d(c)
    c.kt.put_failure = "error500_before_save"
    r = c.post(f"/api/streams/{sid(c)}/push")
    assert r.status_code == 502 and r.json()["error"] == "push_not_applied"
    assert "answered the PUT with an error" in r.json()["detail"]


def test_push_nonjson_answer_is_rechecked(c):
    _draft_d(c)
    c.kt.put_failure = "nonjson_after_save"
    v = op(c, "POST", f"/api/streams/{sid(c)}/push")
    assert v["dirty"] is False and "confirmed" in v["note"]


def test_push_stream_gone_after_timeout(c):
    _draft_d(c)
    c.kt.put_failure = "stream_gone_timeout"
    r = c.post(f"/api/streams/{sid(c)}/push")
    assert r.status_code == 404 and r.json()["error"] == "stream_gone"
    assert offer_stream(c.get(f"/api/campaigns/{OUR}/streams").json())["dirty"] is True


@pytest.mark.parametrize("bad", [{"x": 1}, "a" * 40, "bad state!", "актив", "Active"])
def test_malformed_state_from_keitaro_is_502(c, bad):
    c.kt.offer_stream()["offers"][0]["state"] = bad
    r = c.post(f"/api/campaigns/{OUR}/fetch")
    assert r.status_code == 502


@pytest.mark.parametrize("with_fetch", [False, True])
def test_bring_back_restores_last_known_state_either_path(c, with_fetch):
    c.kt.offer_stream()["offers"][1]["state"] = "disabled"
    fetch(c)
    op(c, "POST", f"/api/streams/{sid(c)}/offers/{B}/remove")
    op(c, "POST", f"/api/streams/{sid(c)}/push")
    if with_fetch:
        fetch(c)
    v = op(c, "POST", f"/api/streams/{sid(c)}/offers/{B}/bring-back")
    assert next(o for o in v["offers"] if o["offer_id"] == B)["state"] == "disabled"


def test_openapi_declares_csrf_header(c):
    spec = c.get("/openapi.json").json()
    schemes = spec["components"]["securitySchemes"]
    assert any(s.get("name") == "X-Requested-With" and s.get("in") == "header" for s in schemes.values())
