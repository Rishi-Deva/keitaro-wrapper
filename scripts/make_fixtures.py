"""Build sanitized test fixtures from raw step-0 dumps (scripts/out, gitignored).

No real values live in this file. Everything is anonymized while building:
- hosts, tokens, notes, SSL data -> placeholders / null;
- DEFAULT_DOMAIN_ID / DEFAULT_GROUP_ID / DEFAULT_TRAFFIC_SOURCE_ID (from .env) -> 101 / 202 / 303;
- the first offer id -> 7001; every other id-like value -> a sequential example id;
- group / campaign / offer names -> example names.

Usage: .venv/bin/python scripts/make_fixtures.py <streams dump with offers> <streams dump of a deleted campaign>
       (file names inside scripts/out, e.g. admin_api_v1_campaigns_<id>_streams.raw.json)
NOTE: tests reference the resulting example ids (101/202/303/7001/800001...). Re-generating may require
updating test constants.
"""
import json
import sys
from itertools import count
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
RAW, FX = ROOT / "scripts" / "out", ROOT / "tests" / "fixtures"
env = dotenv_values(ROOT / ".env")
ID_KEYS = {"id", "oid", "stream_id", "campaign_id", "group_id", "domain_id", "traffic_source_id",
           "affiliate_network_id", "offer_id"}
NULL_KEYS = {"ssl_data", "default_campaign", "default_campaign_id", "error_description", "error_solution",
             "cloudflare_id", "notes", "action_payload", "local_path", "preview_path", "campaigns_count",
             "action_options"}  # action_options may hold landing folder names
FIXED_TS = "2026-01-01 00:00:00"
GROUP_NAMES = {"offer": "Example offer group", "": "Example domain group", "campaign": "EXAMPLE_GROUP"}

fixed = {int(env[k]): v for k, v in (("DEFAULT_DOMAIN_ID", 101), ("DEFAULT_GROUP_ID", 202),
                                     ("DEFAULT_TRAFFIC_SOURCE_ID", 303)) if env.get(k)}
seq = {"offer": count(7001), "stream": count(800001), "campaign": count(900001), "filter": count(600001),
       "row": count(700001), "other": count(400001)}
mapping: dict[tuple[str, int], int] = {}


def fake_id(kind: str, v: int) -> int:
    if v in fixed and kind == "other":
        return fixed[v]
    key = (kind, v)
    if key not in mapping:
        mapping[key] = fixed.get(v) if kind == "other" and v in fixed else next(seq[kind])
    return mapping[key]


def kind_of(key: str, parent: str) -> str:
    if key == "offer_id" or (key == "id" and parent == "offer"):
        return "offer"
    if key == "stream_id" or (key == "id" and parent == "stream"):
        return "stream"
    if key == "campaign_id" or (key == "id" and parent == "campaign"):
        return "campaign"
    if key in ("oid",) or (key == "id" and parent == "filter"):
        return "filter"
    if key == "id" and parent == "stream_offer":
        return "row"
    return "other"


def clean(o, parent: str = ""):
    if isinstance(o, list):
        return [clean(x, parent) for x in o]
    if not isinstance(o, dict):
        return o
    r = {}
    for k, v in o.items():
        if k in NULL_KEYS:
            r[k] = None
        elif k == "token":
            r[k] = "TOKEN"
        elif k == "domain" and isinstance(v, str):
            r[k] = "https://campaign-domain.example/"
        elif k in ID_KEYS and isinstance(v, int) and not isinstance(v, bool):
            r[k] = fake_id(kind_of(k, parent), v)
        elif k == "affiliate_network" and isinstance(v, (str, int)):
            r[k] = "5"
        elif k == "group" and isinstance(v, str):
            r[k] = GROUP_NAMES.get(parent, "Example group")
        elif k == "country" and isinstance(v, list):
            r[k] = ["XX"]
        elif k.endswith("_at") and isinstance(v, str):
            r[k] = FIXED_TS
        elif k == "conversion_timezone" and isinstance(v, str):
            r[k] = "UTC"
        elif k == "position" and isinstance(v, int) and v > 1000 and v != 9999:
            r[k] = 1
        elif k == "filters":
            r[k] = clean(v, "filter")
        elif k == "offers":
            r[k] = clean(v, "stream_offer")
        else:
            r[k] = clean(v, parent)
    return r


def load(name: str):
    return json.loads((RAW / name).read_text())


def dump(name: str, data, parent: str) -> None:
    (FX / name).write_text(json.dumps(clean(data, parent), ensure_ascii=False, indent=2) + "\n")


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    FX.mkdir(parents=True, exist_ok=True)
    offers = load("admin_api_v1_offers.raw.json")
    for o in offers:
        o["name"] = "Example Offer A"
    dump("offers_list.json", offers, "offer")
    camps = load("admin_api_v1_campaigns.raw.json")[:3]
    for i, c in enumerate(camps):
        c["name"], c["alias"] = f"test_example_campaign{i + 1}", f"alias{i + 1}"
    dump("campaigns_list.json", camps, "campaign")
    doms = load("admin_api_v1_domains.raw.json")
    for d in doms:
        d["name"] = "campaign-domain.example"
    dump("domains_list.json", doms, "")
    gid = int(env["DEFAULT_GROUP_ID"])
    groups = [g for g in load("admin_api_v1_groups_type_campaigns.raw.json") if g["id"] == gid]
    for g in groups:
        g["name"] = "EXAMPLE_GROUP"
    dump("groups_campaigns.json", groups + [{"id": 2, "name": "other_group", "position": 2, "type": "campaigns"}], "")
    ts = load("admin_api_v1_traffic_sources.raw.json")
    for t in ts:
        t["name"] = "TEST_SOURCE"
    dump("traffic_sources_list.json", ts, "")
    dump("streams_two.json", load(sys.argv[1]), "stream")
    dump("campaign_deleted_streams.json", load(sys.argv[2]), "stream")
    print("fixtures written; review them and run scripts/check_secrets.py")


if __name__ == "__main__":
    main()
