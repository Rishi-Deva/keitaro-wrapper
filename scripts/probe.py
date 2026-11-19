"""Read-only probe of Keitaro Admin API for step 0.

Usage: .venv/bin/python scripts/probe.py <path> [<path> ...]
  path is relative to KEITARO_URL, e.g. admin_api/v1/campaigns

Prints status + redacted body preview. Full raw body goes to scripts/out/ (gitignored).
Tracker host and API key are masked in everything printed.
"""
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
env = dotenv_values(ROOT / ".env")
BASE = env["KEITARO_URL"].rstrip("/")
KEY = env["KEITARO_API_KEY"]
HOST = urlparse(BASE).hostname or ""
OUT = ROOT / "scripts" / "out"
OUT.mkdir(parents=True, exist_ok=True)


def redact(text: str) -> str:
    if KEY:
        text = text.replace(KEY, "<API_KEY>")
    if HOST:
        text = re.sub(re.escape(HOST), "<TRACKER_HOST>", text, flags=re.I)
    # any other host (campaign domains, offer URLs) except google.com
    text = re.sub(
        r"(https?://)(?!(?:www\.)?google\.com)[^/\s\"']+",
        r"\1<HOST>",
        text,
        flags=re.I,
    )
    # domain names in plain fields and campaign tokens
    text = re.sub(r'("(?:name|domain|token)"\s*:\s*")([^"]*\.[a-z]{2,}/?)(")', r"\1<HOST>\3", text, flags=re.I)
    text = re.sub(r'("token"\s*:\s*")[^"]+(")', r"\1<TOKEN>\2", text)
    return text


def main() -> None:
    limit = 1500
    with httpx.Client(base_url=BASE, headers={"Api-Key": KEY}, timeout=15) as c:
        for path in sys.argv[1:]:
            if path.startswith("--limit="):
                limit = int(path.split("=", 1)[1])
                continue
            try:
                r = c.get("/" + path.lstrip("/"))
            except httpx.HTTPError as e:
                print(f"GET {path} -> ERROR {type(e).__name__}: {redact(str(e))}")
                continue
            fname = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_") + ".raw.json"
            (OUT / fname).write_text(r.text, encoding="utf-8")
            ctype = r.headers.get("content-type", "")
            print(f"GET {path} -> {r.status_code} [{ctype}] {len(r.content)}b saved {fname}")
            body = r.text
            if "json" in ctype:
                try:
                    body = json.dumps(r.json(), ensure_ascii=False, indent=1)
                except ValueError:
                    pass
            print(redact(body[:limit]))
            print("-" * 60)


if __name__ == "__main__":
    main()
