"""Read-only live smoke test of KeitaroClient against the tracker in .env. Prints counts only.

Usage: .venv/bin/python scripts/smoke_client.py [deleted_campaign_id]
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.config import Settings  # noqa: E402
from app.keitaro.client import KeitaroClient  # noqa: E402
from app.keitaro.errors import KeitaroError  # noqa: E402


async def main() -> None:
    s = Settings()
    async with KeitaroClient(s.keitaro_api_base, s.keitaro_api_key.get_secret_value(), timeout=s.keitaro_timeout) as c:
        print("offers:", len(await c.list_offers()))
        print("campaigns:", len(await c.list_campaigns()))
        print("groups (campaigns):", len(await c.list_campaign_groups()))
        print("domain ids:", [d["id"] for d in await c.list_domains()])
        print("traffic source ids:", [t["id"] for t in await c.list_traffic_sources()])
        if len(sys.argv) > 1:  # e.g. a soft-deleted test campaign -> expect KeitaroNotFound
            try:
                await c.get_campaign(int(sys.argv[1]))
            except KeitaroError as e:
                print("campaign ->", type(e).__name__, e.status, str(e))


asyncio.run(main())
