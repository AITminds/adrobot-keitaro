import json

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import inspect

from app.config import Settings
from app.errors import TrackerError
from app.main import create_app
from app.tracker import KeitaroClient
from conftest import change, stream
from test_workflow import add_offer, creation_payload


def test_create_failure_disables_partial_campaign_and_deduplicates(
    environment, monkeypatch
):
    c, tracker, _, _ = environment
    real = tracker.request

    def fail_second_stream(method, path, **kwargs):
        if (
            method == "POST"
            and path == "streams"
            and kwargs["json"]["type"] == "default"
        ):
            raise TrackerError("Simulated tracker failure")
        return real(method, path, **kwargs)

    monkeypatch.setattr(tracker, "request", fail_second_stream)
    payload = creation_payload()
    r = c.post("/api/campaigns", json=payload)
    assert r.status_code == 502
    campaign_id = r.json()["campaign_id"]
    assert real("GET", f"campaigns/{campaign_id}")["state"] == "disabled"
    operation = c.get(f"/api/operations/{payload['request_id']}").json()
    assert operation["status"] == "failed" and operation["campaign_id"] == campaign_id
    assert c.post("/api/campaigns", json=payload).status_code == 409
    assert len(real("GET", "campaigns")) == 2


def test_create_timeout_does_not_retry_ambiguous_post(environment, monkeypatch):
    c, tracker, _, _ = environment
    real = tracker.request

    def ambiguous(method, path, **kwargs):
        result = real(method, path, **kwargs)
        if method == "POST" and path == "campaigns":
            raise TrackerError("Simulated timeout", uncertain=True)
        return result

    monkeypatch.setattr(tracker, "request", ambiguous)
    payload = creation_payload()
    r = c.post("/api/campaigns", json=payload)
    assert r.status_code == 502 and r.json()["uncertain"]
    assert (
        c.get(f"/api/operations/{payload['request_id']}").json()["status"]
        == "uncertain"
    )
    assert c.post("/api/campaigns", json=payload).status_code == 409
    assert len(real("GET", "campaigns")) == 2
    assert real("GET", "campaigns")[-1]["state"] == "disabled"


def test_publish_timeout_can_be_reconciled_without_duplicate_write(loaded, monkeypatch):
    c, tracker, _, _ = loaded
    add_offer(c)
    real = tracker.request
    writes = []

    def timeout_after_write(method, path, **kwargs):
        result = real(method, path, **kwargs)
        if method == "PUT" and path == "streams/2002":
            writes.append(path)
            raise TrackerError("Simulated lost response", uncertain=True)
        return result

    monkeypatch.setattr(tracker, "request", timeout_after_write)
    assert change(c, "publish").status_code == 502
    assert stream(c)["dirty"]
    r = change(c, "publish")
    assert r.status_code == 200 and not r.json()["dirty"]
    assert writes == ["streams/2002"]


def test_sync_failure_rolls_back_discard(loaded, monkeypatch):
    c, tracker, _, _ = loaded
    add_offer(c)
    before = stream(c)
    real = tracker.request

    def fail(method, path, **kwargs):
        if path == "campaigns/1001/streams":
            raise TrackerError("Simulated failure")
        return real(method, path, **kwargs)

    monkeypatch.setattr(tracker, "request", fail)
    assert (
        c.post("/api/campaigns/1001/sync", json={"discard_drafts": True}).status_code
        == 502
    )
    assert stream(c) == before


def test_http_contract_and_no_credentials_in_body():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"id": 12})

    settings = Settings(
        _env_file=None,
        adrobot_mode="live",
        keitaro_base_url="https://tracker.example.com",
        keitaro_api_key="unit-test-secret",
    )
    client = KeitaroClient(settings, transport=httpx.MockTransport(handle))
    assert client.request("PUT", "streams/12", json={"offers": []}) == {"id": 12}
    request = requests[0]
    assert str(request.url) == "https://tracker.example.com/admin_api/v1/streams/12"
    assert request.headers["Api-Key"] == "unit-test-secret"
    assert json.loads(request.content) == {"offers": []}
    client.close()


@pytest.mark.parametrize("status", [301, 401, 402, 403, 404, 429, 500])
def test_http_errors_redact_server_body_and_never_follow_redirects(status):
    count = 0

    def handle(request):
        nonlocal count
        count += 1
        return httpx.Response(
            status,
            text="PRIVATE-UPSTREAM-DATA",
            headers={"Location": "https://other.example"},
        )

    client = KeitaroClient(
        Settings(_env_file=None), transport=httpx.MockTransport(handle)
    )
    with pytest.raises(TrackerError) as error:
        client.request("POST", "campaigns", json={"name": "x"})
    assert "PRIVATE-UPSTREAM-DATA" not in str(error.value) and count == 1
    assert error.value.uncertain == (status >= 500)
    client.close()


def test_http_timeout_is_uncertain_only_for_writes():
    def handle(request):
        raise httpx.ReadTimeout("timeout", request=request)

    client = KeitaroClient(
        Settings(_env_file=None), transport=httpx.MockTransport(handle)
    )
    for method in ("GET", "POST", "PUT"):
        with pytest.raises(TrackerError) as error:
            client.request(method, "campaigns")
        assert error.value.uncertain == (method != "GET")
    client.close()


def test_workspace_cannot_mix_demo_and_live(environment):
    _, tracker, _, settings = environment
    live = settings.model_copy(
        update={"adrobot_mode": "live", "keitaro_base_url": "https://other.example"}
    )
    with pytest.raises(RuntimeError, match="another tracker"):
        with TestClient(create_app(live, tracker)):
            pass


def test_optional_basic_auth_protects_ui_and_api(environment):
    _, tracker, _, settings = environment
    secured = Settings(
        _env_file=None,
        database_url=settings.database_url,
        demo_database_url=settings.demo_database_url,
        app_username="reviewer",
        app_password="test-password",
    )
    with TestClient(create_app(secured, tracker)) as c:
        assert c.get("/").status_code == 401
        assert c.get("/api/config").status_code == 401
        assert c.get("/healthz").status_code == 200
        assert (
            c.get("/api/config", auth=("reviewer", "test-password")).status_code == 200
        )
        assert c.get("/api/config", auth=("reviewer", "wrong")).status_code == 401


def test_migrations_upgrade_downgrade(environment):
    _, _, app, settings = environment
    cfg = Config("alembic.ini")
    cfg.attributes["database_url"] = settings.database_url
    command.check(cfg)
    command.downgrade(cfg, "base")
    assert "stream_offers" not in inspect(app.state.engine).get_table_names()
    command.upgrade(cfg, "head")
    assert "stream_offers" in inspect(app.state.engine).get_table_names()
