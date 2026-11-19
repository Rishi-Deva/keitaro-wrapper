from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings, get_settings


def test_settings_from_env():
    s = get_settings()
    assert s.campaign_prefix == "test_example_"
    assert s.keitaro_api_base == "https://tracker.test/admin_api/v1"
    assert s.default_group_id == 202
    # key is SecretStr: not shown in repr
    assert "fake-api-key" not in repr(s)


@pytest.mark.parametrize("bad", ["<surname>_", "test_x", "prod_"])
def test_prefix_must_be_test_underscore(monkeypatch, bad):
    monkeypatch.setenv("CAMPAIGN_PREFIX", bad)
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_have_no_defaults(monkeypatch):
    monkeypatch.delenv("KEITARO_API_KEY")
    monkeypatch.delenv("KEITARO_URL")
    with pytest.raises(ValidationError):
        Settings()


# ---------- View in KT link ----------

def test_kt_link_default_template():
    # verified admin format: /admin/#!/campaign/{id}/main (singular "campaign")
    assert get_settings().campaign_url(900003) == "https://tracker.test/admin/#!/campaign/900003/main"


def test_kt_link_trailing_slash_in_base(monkeypatch):
    monkeypatch.setenv("KEITARO_URL", "https://tracker.test/")
    assert Settings().campaign_url(7) == "https://tracker.test/admin/#!/campaign/7/main"


def test_kt_link_explicit_host_template(monkeypatch):
    monkeypatch.setenv("KEITARO_CAMPAIGN_URL_TEMPLATE", "https://tracker.example/admin/#!/campaign/{id}/main")
    assert Settings().campaign_url(42) == "https://tracker.example/admin/#!/campaign/42/main"


def test_kt_link_empty_hides(monkeypatch):
    monkeypatch.setenv("KEITARO_CAMPAIGN_URL_TEMPLATE", "")
    assert Settings().campaign_url(1) is None


@pytest.mark.parametrize("bad", ["https://x/admin/campaigns", "https://x/{id}/{secret}", "https://x/{0}/{id}"])
def test_kt_link_bad_template(monkeypatch, bad):
    monkeypatch.setenv("KEITARO_CAMPAIGN_URL_TEMPLATE", bad)
    with pytest.raises(ValidationError):
        Settings()


def test_kt_link_id_must_be_int():
    with pytest.raises((TypeError, ValueError)):
        get_settings().campaign_url("1/../x")  # type: ignore[arg-type]


def test_env_example_does_not_override_default_template(tmp_path):
    from dotenv import dotenv_values
    example = Path(__file__).resolve().parent.parent / ".env.example"
    assert "KEITARO_CAMPAIGN_URL_TEMPLATE" not in dotenv_values(example)  # commented -> default is used
    line = next(x for x in example.read_text().splitlines() if "tracker.example/admin" in x)
    assert line.startswith("# ")
    uncommented = tmp_path / "e"
    uncommented.write_text(line[2:] + "\n")
    assert dotenv_values(uncommented)["KEITARO_CAMPAIGN_URL_TEMPLATE"] == "https://tracker.example/admin/#!/campaign/{id}/main"
