"""Runs against the disposable PostgreSQL service in CI."""

from concurrent.futures import ThreadPoolExecutor
import os

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.main import create_app
from app.tracker import DemoKeitaro
from conftest import change, stream
from test_workflow import add_offer, creation_payload


@pytest.mark.skipif(
    not os.getenv("TEST_DATABASE_URL"), reason="Requires disposable PostgreSQL service"
)
def test_postgres_migrations_drafts_and_concurrency(tmp_path):
    settings = Settings(
        _env_file=None,
        database_url=os.environ["TEST_DATABASE_URL"],
        demo_database_url=f"sqlite:///{tmp_path / 'tracker.db'}",
    )
    config = Config("alembic.ini")
    config.attributes["database_url"] = settings.database_url
    command.upgrade(config, "head")
    command.check(config)
    tracker = DemoKeitaro(settings)
    with TestClient(create_app(settings, tracker)) as c:
        assert c.post("/api/campaigns/1001/sync", json={}).status_code == 200
        assert add_offer(c).status_code == 200
        assert change(c, "publish").status_code == 200
        assert change(c, "pin", 101, pinned=True, share=25).status_code == 200
        assert change(c, "remove", 102).status_code == 200
        assert change(c, "publish").status_code == 200
        assert stream(c)["total_share"] == 100
        revision = stream(c)["revision"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda offer_id: c.post(
                        "/api/streams/2002/offers",
                        json={"revision": revision, "offer_id": offer_id},
                    ),
                    [105, 106],
                )
            )
        assert sorted(r.status_code for r in responses) == [200, 409]
        assert c.post("/api/campaigns", json=creation_payload()).status_code == 201
    tracker.close()
    command.downgrade(config, "base")
    command.upgrade(config, "head")
