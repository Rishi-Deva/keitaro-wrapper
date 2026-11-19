"""Stateful in-memory fake of the Keitaro Admin API endpoints we use.

Seeded from tests/fixtures (real response shapes, sanitized). The real tracker has one offer and the
test key cannot create offers, so extra offers are copies of the real one with new ids/names.
Used by tests (via httpx.MockTransport) and by scripts/fake_tracker.py for clicking through the UI.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import httpx

FX = Path(__file__).parent / "fixtures"
OUR, FOREIGN = 5001, 5002
OFFER_NAMES = ["Offer Alpha", "Offer Beta", "Offer Gamma", "Offer Delta", "Offer Epsilon"]
FIRST_OFFER = 7001  # example ids 7001..7005


def load(name: str):
    return json.loads((FX / name).read_text())


class FakeKeitaro:
    def __init__(self, prefix: str = "test_example_", offers_in_stream: int = 3) -> None:
        base = load("offers_list.json")[0]
        self.offers = []
        for i, name in enumerate(OFFER_NAMES):
            o = copy.deepcopy(base)
            o.update(id=FIRST_OFFER + i, name=name)
            self.offers.append(o)
        self.campaigns: dict[int, dict] = {}
        self.streams: dict[int, dict] = {}
        self.requests: list[tuple[str, str]] = []
        # PUT failure simulation (one-shot):
        #   "timeout_after_save"  - Keitaro saves the change, the response is lost (ReadTimeout)
        #   "timeout_before_save" - nothing saved, ReadTimeout
        #   "garbage_after_save"  - saved, but the response body has wrong offers
        #   "other_state"         - Keitaro ends up with something else, response lost
        self.put_failure: str | None = None
        self.fail_reads: bool = False  # all GETs time out (to test "cannot re-check")
        self._next = 9000
        tmpl_c = load("campaign_created.json")
        for cid, name in ((OUR, prefix + "seed"), (FOREIGN, "someone_else_campaign")):
            c = copy.deepcopy(tmpl_c)
            c.update(id=cid, name=name, alias=f"alias{cid}")
            self.campaigns[cid] = c
            geo, offer_stream = copy.deepcopy(load("streams_two.json"))
            for s in (geo, offer_stream):
                s["id"], s["campaign_id"] = self.new_id(), cid
                self.streams[s["id"]] = s
            tmpl_o = offer_stream["offers"][0]
            shares = _equal(offers_in_stream)
            offer_stream["offers"] = [
                {**tmpl_o, "id": self.new_id(), "stream_id": offer_stream["id"], "offer_id": FIRST_OFFER + i,
                 "share": shares[i]}
                for i in range(offers_in_stream)
            ]

    # ---------- helpers for tests ----------

    def new_id(self) -> int:
        self._next += 1
        return self._next

    def offer_stream(self, campaign_id: int = OUR) -> dict:
        return next(s for s in self.streams.values() if s["campaign_id"] == campaign_id and s["schema"] == "landings")

    def geo_stream(self, campaign_id: int = OUR) -> dict:
        return next(s for s in self.streams.values() if s["campaign_id"] == campaign_id and s["schema"] == "redirect")

    def kt_offers(self, campaign_id: int = OUR) -> list[tuple[int, int]]:
        return [(o["offer_id"], o["share"]) for o in self.offer_stream(campaign_id)["offers"]]

    def drop_offer_in_kt(self, offer_id: int, campaign_id: int = OUR) -> None:
        """Somebody deletes an offer directly in Keitaro (and fixes shares there)."""
        st = self.offer_stream(campaign_id)
        st["offers"] = [o for o in st["offers"] if o["offer_id"] != offer_id]
        for o, sh in zip(st["offers"], _equal(len(st["offers"])) if st["offers"] else []):
            o["share"] = sh

    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] != "GET"]

    # ---------- transport ----------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def handle(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path.split("/admin_api/v1/", 1)[1]
        m = req.method
        self.requests.append((m, p))
        body = json.loads(req.content) if req.content else None
        if m == "GET" and self.fail_reads:
            raise httpx.ReadTimeout("fake read timeout", request=req)
        if m == "GET" and p == "offers":
            return httpx.Response(200, json=self.offers)
        if m == "GET" and p == "campaigns":
            return httpx.Response(200, json=[c for c in self.campaigns.values() if c["state"] != "deleted"])
        if mm := re.fullmatch(r"campaigns/(\d+)", p):
            c = self.campaigns.get(int(mm[1]))
            if not c or c["state"] == "deleted":
                return httpx.Response(404, text="<html>404 Not Found</html>", headers={"content-type": "text/html"})
            if m == "DELETE":
                c["state"] = "deleted"
                return httpx.Response(200, json=[c])
            return httpx.Response(200, json=c)
        if m == "GET" and (mm := re.fullmatch(r"campaigns/(\d+)/streams", p)):
            return httpx.Response(200, json=[s for s in self.streams.values() if s["campaign_id"] == int(mm[1])])
        if m == "POST" and p == "campaigns":
            if not body.get("alias"):
                return httpx.Response(422, json={"alias": ["Alias is required"]})
            c = {**load("campaign_created.json"), **body, "id": self.new_id()}
            self.campaigns[c["id"]] = c
            return httpx.Response(200, json=c)
        if m == "POST" and p == "streams":
            s = {**body, "id": self.new_id(), "landings": [], "triggers": []}
            s["offers"] = [{**o, "id": self.new_id(), "stream_id": s["id"]} for o in body.get("offers", [])]
            self.streams[s["id"]] = s
            return httpx.Response(200, json=s)
        if m == "PUT" and (mm := re.fullmatch(r"streams/(\d+)", p)):
            s = self.streams.get(int(mm[1]))
            if s is None:
                return httpx.Response(404, text="<html>404</html>", headers={"content-type": "text/html"})
            mode, self.put_failure = self.put_failure, None
            if mode == "error500_before_save":
                return httpx.Response(500, text="Internal error", headers={"content-type": "text/plain"})
            if mode == "stream_gone_timeout":
                del self.streams[s["id"]]
                raise httpx.ReadTimeout("fake timeout", request=req)
            if mode == "timeout_before_save":
                raise httpx.ReadTimeout("fake timeout", request=req)
            if mode == "other_state":
                s["offers"] = [{**s["offers"][0], "share": 100}] if s["offers"] else []
                raise httpx.ReadTimeout("fake timeout", request=req)
            if "offers" in body:  # verified: PUT replaces the whole list
                s["offers"] = [{**o, "id": self.new_id(), "stream_id": s["id"]} for o in body["offers"]]
            if mode == "timeout_after_save":
                raise httpx.ReadTimeout("fake timeout", request=req)
            if mode == "timeout_after_save_reads_down":
                self.fail_reads = True
                raise httpx.ReadTimeout("fake timeout", request=req)
            if mode == "nonjson_after_save":
                return httpx.Response(200, text="<html>ok</html>", headers={"content-type": "text/html"})
            if mode == "garbage_after_save":
                return httpx.Response(200, json={"id": s["id"], "offers": [{"offer_id": 1, "share": 100}]})
            return httpx.Response(200, json=s)
        return httpx.Response(404, text="not mocked", headers={"content-type": "text/plain"})


def _equal(n: int) -> list[int]:
    base, rest = divmod(100, n)
    return [base + 1 if i < rest else base for i in range(n)]
