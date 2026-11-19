"""Single write request to Keitaro for step 0 verification.

Usage: .venv/bin/python scripts/write_probe.py METHOD path ['json body']
Refuses POST /campaigns whose name lacks CAMPAIGN_PREFIX. Output is redacted.
"""
import json
import sys

import httpx

from probe import BASE, KEY, redact, env  # noqa: E402

PREFIX = env.get("CAMPAIGN_PREFIX") or ""


def main() -> None:
    method, path = sys.argv[1].upper(), sys.argv[2].lstrip("/")
    body = json.loads(sys.argv[3]) if len(sys.argv) > 3 else None
    if not PREFIX:
        sys.exit("CAMPAIGN_PREFIX is empty")
    if path.rstrip("/").split("/")[-1] in ("campaigns", "offers") and method == "POST":
        if not str(body.get("name", "")).startswith(PREFIX):
            sys.exit("refused: name must start with CAMPAIGN_PREFIX")
    with httpx.Client(base_url=BASE, headers={"Api-Key": KEY}, timeout=15) as c:
        r = c.request(method, "/" + path, json=body)
    print(f"{method} {path} -> {r.status_code}")
    try:
        print(redact(json.dumps(r.json(), ensure_ascii=False, indent=1)))
    except ValueError:
        print(redact(r.text[:800]))


if __name__ == "__main__":
    main()
