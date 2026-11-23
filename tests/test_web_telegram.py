import httpx
import pytest
import respx
from fastapi.testclient import TestClient

from flug.monitor import run_all
from flug.telegram import TelegramError, TelegramNotifier
from flug.web import create_app

from .conftest import TODAY, FakeProvider, make_watch


@pytest.fixture
def client(session_factory, settings):
    run_all(session_factory, [make_watch()], [FakeProvider({"FRA": "199", "MUC": "250"})], settings, None, today=TODAY)
    return TestClient(create_app(settings, session_factory))


def test_auth_required(client):
    assert client.get("/").status_code == 401
    assert client.get("/", auth=("admin", "wrong")).status_code == 401
    assert client.get("/healthz").status_code == 200


def test_dashboard_pages(client):
    r = client.get("/", auth=("admin", "pw"))
    assert r.status_code == 200 and "199.00 EUR" in r.text
    r = client.get("/watch/1", auth=("admin", "pw"))
    assert r.status_code == 200 and "FRA" in r.text and "MUC" in r.text
    h = client.get("/api/watch/1/history", auth=("admin", "pw")).json()
    assert h["target"] == 200.0 and set(h["series"]) == {"FRA", "MUC"}


def test_set_target(client):
    r = client.post("/watch/1/target", data={"target_price": "175,50"}, auth=("admin", "pw"), follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/api/watch/1/history", auth=("admin", "pw")).json()["target"] == 175.5
    bad = client.post("/watch/1/target", data={"target_price": "abc"}, auth=("admin", "pw"))
    assert bad.status_code == 400
    csrf = client.post("/watch/1/target", data={"target_price": "1"}, auth=("admin", "pw"),
                       headers={"origin": "https://evil.example"})
    assert csrf.status_code == 403


def test_refuses_to_start_without_password(session_factory, settings):
    settings.dashboard_password = ""
    with pytest.raises(RuntimeError):
        create_app(settings, session_factory)


@respx.mock
def test_telegram_send_and_errors():
    url = "https://api.telegram.org/botT0K/sendMessage"
    route = respx.post(url)
    route.side_effect = [
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 1}}),
        httpx.Response(200, json={"ok": True}),
    ]
    n = TelegramNotifier("T0K", "42", sleep=lambda s: None)
    n.send("<b>hi</b>")
    assert route.call_count == 2
    assert b'"chat_id":"42"' in route.calls.last.request.content.replace(b" ", b"")

    respx.post(url).mock(return_value=httpx.Response(400, json={"ok": False, "description": "chat not found"}))
    with pytest.raises(TelegramError) as exc:
        n.send("x")
    assert "chat not found" in str(exc.value) and "T0K" not in str(exc.value)
