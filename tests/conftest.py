from pathlib import Path

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app
from app.tracker import DemoKeitaro


@pytest.fixture
def environment(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite:///{tmp_path / 'app.db'}",
        demo_database_url=f"sqlite:///{tmp_path / 'tracker.db'}",
    )
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"))
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")
    tracker = DemoKeitaro(settings)
    app = create_app(settings, tracker)
    with TestClient(app) as client:
        yield client, tracker, app, settings
    tracker.close()


@pytest.fixture
def loaded(environment):
    client, tracker, app, settings = environment
    response = client.post("/api/campaigns/1001/sync", json={})
    assert response.status_code == 200, response.text
    return environment


def stream(client):
    return next(
        s
        for s in client.get("/api/campaigns/1001").json()["streams"]
        if s["id"] == 2002
    )


def change(client, action, offer_id=None, **extra):
    current = stream(client)
    path = (
        f"/api/streams/2002/{action}"
        if offer_id is None
        else f"/api/streams/2002/offers/{offer_id}/{action}"
    )
    return client.post(path, json={"revision": current["revision"], **extra})
