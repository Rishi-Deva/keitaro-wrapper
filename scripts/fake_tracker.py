"""Run the app against an in-memory fake Keitaro (no network) to click through the UI with several
offers, which the real test tracker does not have.

    .venv/bin/python scripts/fake_tracker.py        # http://127.0.0.1:8766

Fake data: tests/fake_keitaro.py (real response shapes from fixtures). Extra control endpoint to
simulate "offer deleted directly in Keitaro":
    curl -X POST http://127.0.0.1:8766/__fake/campaigns/5001/drop-offer/7002
"""
import sys
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.db import Base, make_engine  # noqa: E402
from app.keitaro.client import KeitaroClient  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fake_keitaro import FakeKeitaro  # noqa: E402

PREFIX = "test_demo_"
DB = ROOT / "scripts" / "out" / "fake.db"  # gitignored


def main() -> None:
    settings = Settings(
        _env_file=None, keitaro_url="https://fake-tracker.invalid", keitaro_api_key="fake",
        default_domain_id=101, default_group_id=202, default_traffic_source_id=303,
        campaign_prefix=PREFIX, database_url=f"sqlite:///{DB}", allowed_hosts="127.0.0.1,localhost",
    )
    DB.parent.mkdir(parents=True, exist_ok=True)
    DB.unlink(missing_ok=True)
    eng = make_engine(settings.database_url)
    Base.metadata.create_all(eng)
    eng.dispose()

    fake = FakeKeitaro(prefix=PREFIX)
    client = KeitaroClient(settings.keitaro_api_base, "fake", transport=fake.transport())
    app = create_app(settings=settings, client=client)

    @app.post("/__fake/campaigns/{cid}/drop-offer/{oid}")
    def drop(cid: int, oid: int) -> dict:
        fake.drop_offer_in_kt(oid, cid)
        return {"kt_offers": fake.kt_offers(cid)}

    @app.post("/__fake/campaigns/{cid}/offer-state/{oid}/{state}")
    def offer_state(cid: int, oid: int, state: str) -> dict:
        for o in fake.offer_stream(cid)["offers"]:
            if o["offer_id"] == oid:
                o["state"] = state
        return {"kt_offers": fake.offer_stream(cid)["offers"]}

    @app.post("/__fake/put-failure/{mode}")
    def put_failure(mode: str) -> dict:
        fake.put_failure = mode  # timeout_after_save | timeout_before_save | garbage_after_save | other_state
        return {"put_failure": mode}

    @app.get("/__fake/campaigns/{cid}/kt-offers")
    def kt_offers(cid: int) -> dict:
        return {"kt_offers": fake.kt_offers(cid), "writes": fake.writes()}

    # control routes must win over the static mount at "/"
    for _ in range(4):
        app.router.routes.insert(0, app.router.routes.pop())
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning")


if __name__ == "__main__":
    main()
