"""Scan project files (and git history, if any) for real tracker data that must not be published.

Collected from .env and raw dumps in scripts/out (both gitignored); the values are NEVER printed:
- API key, tracker host, real domains (+ their base name), campaign tokens;
- CAMPAIGN_PREFIX and the surname inside it;
- names / aliases of campaigns, offers, groups, traffic sources, domains;
- string values inside offers' action_options (e.g. landing folder names);
- real ids from id-like fields (>= 4 digits) and DEFAULT_*_ID values (matched as whole numbers).
Exit code 1 if anything is found.

Usage: .venv/bin/python scripts/check_secrets.py [files or dirs...]
"""
import json
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "scripts" / "out"
SKIP_DIRS = {".git", ".venv", "scripts/out", "__pycache__", ".pytest_cache"}
SKIP_FILES = {".env", "app.db"}
SKIP_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache"}  # at any depth
ID_KEYS = {"id", "oid", "stream_id", "campaign_id", "group_id", "domain_id", "traffic_source_id",
           "affiliate_network_id", "offer_id", "default_campaign_id"}
# entity list dumps whose names/aliases are real data (stream names are generic like "Geo AU -> Google")
NAME_DUMPS = ("admin_api_v1_campaigns.raw.json", "admin_api_v1_offers.raw.json", "admin_api_v1_domains.raw.json",
              "admin_api_v1_traffic_sources.raw.json", "admin_api_v1_groups_type_campaigns.raw.json")
GENERIC = {"active", "disabled", "deleted", "campaigns", "position", "regular", "landings", "redirect",
           "google", "https", "admin"}
MIN_ID = 100  # smaller DEFAULT_*_ID values would match everywhere; such ids are reported as not checked


def _walk(o, key=""):
    if isinstance(o, dict):
        for k, v in o.items():
            yield from _walk(v, k)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v, key)
    else:
        yield key, o


def _load(f: Path):
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def collect() -> tuple[dict[str, str], dict[str, int]]:
    """-> (substring secrets, whole-number secrets). Keys are labels safe to print."""
    env = dotenv_values(ROOT / ".env")
    subs: dict[str, str] = {
        "api_key": env.get("KEITARO_API_KEY") or "",
        "tracker_host": urlparse(env.get("KEITARO_URL") or "").hostname or "",
        "campaign_prefix": env.get("CAMPAIGN_PREFIX") or "",
    }
    prefix = (env.get("CAMPAIGN_PREFIX") or "").strip("_")
    if prefix.startswith("test_"):
        subs["surname"] = prefix[len("test_"):]
    nums: dict[str, int] = {}
    for k in ("DEFAULT_DOMAIN_ID", "DEFAULT_GROUP_ID", "DEFAULT_TRAFFIC_SOURCE_ID"):
        if (env.get(k) or "").isdigit():
            if int(env[k]) >= MIN_ID:
                nums[k.lower()] = int(env[k])
            else:
                print(f"note: {k} is shorter than 3 digits and is not searched for (too many false hits)")

    for f in sorted(OUT.glob("*.raw.json")) if OUT.exists() else []:
        data = _load(f)
        if data is None:
            continue
        items = data if isinstance(data, list) else [data]
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                continue
            label = f"{f.stem.replace('.raw', '')}[{i}]"
            if it.get("token"):
                subs[f"{label}.token"] = str(it["token"])
            if f.name in NAME_DUMPS or re.fullmatch(r"admin_api_v1_campaigns_\d+\.raw\.json", f.name):
                for k in ("name", "alias"):
                    if isinstance(it.get(k), str):
                        subs[f"{label}.{k}"] = it[k]
                if f.name == "admin_api_v1_domains.raw.json" and isinstance(it.get("name"), str):
                    subs[f"{label}.domain_base"] = it["name"].split(".")[0]
                if isinstance(it.get("group"), str):
                    subs[f"{label}.group"] = it["group"]
            for k, v in _walk(it.get("action_options") or {}):
                if isinstance(v, str):
                    subs[f"{label}.action_options.{k}"] = v
            for k, v in _walk(it):
                if k in ID_KEYS and isinstance(v, int) and not isinstance(v, bool) and v >= 1000:
                    nums[f"{label}.{k}={'*' * len(str(v))}"] = v
    subs = {k: v.lower() for k, v in subs.items() if v and len(v) >= 5 and v.lower() not in GENERIC}
    return subs, nums


def files_to_scan(args: list[str]) -> list[Path]:
    if args:
        out = []
        for a in args:
            p = Path(a)
            out += [x for x in p.rglob("*") if x.is_file()] if p.is_dir() else [p]
        return out
    files = []
    for p in ROOT.rglob("*"):
        rel = p.relative_to(ROOT).as_posix()
        if (p.is_file() and p.name not in SKIP_FILES and not set(p.relative_to(ROOT).parts) & SKIP_PARTS
                and not any(rel == d or rel.startswith(d + "/") for d in SKIP_DIRS)):
            files.append(p)
    return files


def scan_text(text: str, subs: dict[str, str], nums: dict[str, int]) -> list[str]:
    low = text.lower()
    hits = [k for k, v in subs.items() if v in low]
    hits += [k for k, v in nums.items() if re.search(rf"(?<![\w.]){v}(?![\w])", text)]
    return hits


def main() -> int:
    subs, nums = collect()
    files = files_to_scan(sys.argv[1:])
    total = 0
    for f in files:
        try:
            text = f.read_text(errors="ignore")
        except OSError:
            continue
        for h in scan_text(text, subs, nums):
            print(f"LEAK {f.relative_to(ROOT) if f.is_relative_to(ROOT) else f}: {h}")
            total += 1
    if (ROOT / ".git").exists():
        log = subprocess.run(["git", "-C", str(ROOT), "log", "-p", "--all"], capture_output=True, text=True).stdout
        for h in scan_text(log, subs, nums):
            print(f"LEAK git history: {h}")
            total += 1
        git_note = "git history checked"
    else:
        git_note = "no .git yet: history NOT checked (run again after the first commit)"
    print(f"check_secrets: {len(subs)} strings + {len(nums)} ids, {len(files)} files, hits={total}; {git_note}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
