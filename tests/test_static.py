import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.keitaro.client import KeitaroClient
from app.main import create_app
from tests.conftest import CSRF, FAKE_API, FAKE_KEY

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture
def tc():
    app = create_app(settings=get_settings(), client=KeitaroClient(FAKE_API, FAKE_KEY))
    with TestClient(app, headers=CSRF) as c:
        yield c


@pytest.mark.parametrize("path,marker", [
    ("/", 'id="create"'), ("/index.html", "Create"), ("/editor.html", 'id="campaign"'),
    ("/app.js", "function offerAutocomplete"), ("/style.css", ".card"),
])
def test_pages_served(tc, path, marker):
    r = tc.get(path)
    assert r.status_code == 200 and marker in r.text


def test_api_not_shadowed_by_static(tc):
    assert tc.get("/health").json() == {"status": "ok"}
    assert tc.get("/api/geo").status_code == 200


def test_no_innerhtml_in_frontend():
    # tracker data (campaign / offer names) must be rendered as text only
    for f in STATIC.glob("*.*"):
        assert not re.search(r"\.innerHTML|outerHTML|insertAdjacentHTML|document\.write", f.read_text()), f.name


def test_editor_has_required_buttons_and_no_stats():
    html = (STATIC / "editor.html").read_text()
    for label in ["Fetch Streams from KT", "View in KT", "Show All Offers", "Push to KT", "Cancel", "Remove", "Bring back"]:
        assert label in html, label
    assert "Stats" not in html and "Trends" not in html


def test_no_browser_dialogs_in_frontend():
    for f in STATIC.glob("*.*"):
        assert not re.search(r"\b(alert|confirm|prompt)\(", f.read_text()), f.name


@pytest.mark.parametrize("raw,host", [
    ("127.0.0.1:8000", "127.0.0.1"), ("LOCALHOST", "localhost"), ("[::1]:8000", "::1"), ("[::1]", "::1"),
    ("::1", "::1"), ("", ""), ("[broken", ""),
])
def test_host_only(raw, host):
    from app.main import _host_only
    assert _host_only(raw) == host
