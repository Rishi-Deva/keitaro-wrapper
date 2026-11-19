"""Test isolation.

Tests must NEVER read the real .env and NEVER write to the working app.db:
- every Settings env var is removed from the environment and replaced with fake values;
- the working directory is a fresh tmp dir (no .env there; Settings reads .env relative to cwd);
- DATABASE_URL points to a temp SQLite file inside that tmp dir;
- a guard fails the test if the effective settings point at the project's .env values or app.db.
"""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Fake values only.
FAKE_BASE = "https://tracker.test"
FAKE_API = FAKE_BASE + "/admin_api/v1"
FAKE_KEY = "fake-api-key-for-tests"
CSRF = {"X-Requested-With": "keitaro-wrapper"}

FAKE_ENV = {
    "KEITARO_URL": FAKE_BASE,
    "KEITARO_API_KEY": FAKE_KEY,
    "DEFAULT_DOMAIN_ID": "101",
    "DEFAULT_GROUP_ID": "202",
    "DEFAULT_TRAFFIC_SOURCE_ID": "303",
    "CAMPAIGN_PREFIX": "test_example_",
    "ALLOWED_HOSTS": "testserver,127.0.0.1,localhost",
}


def load(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch, tmp_path):
    from app.config import Settings, get_settings

    for field in Settings.model_fields:  # drop anything inherited from the shell
        monkeypatch.delenv(field.upper(), raising=False)
    for k, v in FAKE_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.chdir(tmp_path)  # Settings reads ".env" relative to cwd -> nothing here
    get_settings.cache_clear()

    s = get_settings()
    assert str(s.keitaro_url).startswith(FAKE_BASE), "tests must not use the real KEITARO_URL"
    assert s.keitaro_api_key.get_secret_value() == FAKE_KEY, "tests must not use the real API key"
    assert str(PROJECT_ROOT) not in s.database_url and "app.db" not in s.database_url, "tests must not touch app.db"
    yield
    get_settings.cache_clear()
