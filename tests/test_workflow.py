from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from app.main import create_app
from app.models import Offer, StreamOffer
from conftest import change, stream


def add_offer(client, offer_id=104):
    return client.post(
        "/api/streams/2002/offers",
        json={"revision": stream(client)["revision"], "offer_id": offer_id},
    )


def weights(s):
    return {o["id"]: o["share"] for o in s["offers"] if o["state"] == "active"}


def test_video_workflow_draft_cancel_publish_archive_restore_and_sync(loaded):
    c, tracker, app, _ = loaded
    original = tracker.request("GET", "streams/2002")["offers"]
    s = add_offer(c).json()
    assert s["dirty"] and set(weights(s).values()) == {25}
    assert tracker.request("GET", "streams/2002")["offers"] == original
    assert change(c, "cancel").status_code == 200
    assert 104 not in {o["id"] for o in stream(c)["offers"]}
    with app.state.sessions() as db:
        assert db.get(Offer, 104) is not None  # Only the relation is removed.
        assert db.scalar(select(StreamOffer).where(StreamOffer.offer_id == 104)) is None
    add_offer(c)
    assert not change(c, "publish").json()["dirty"]
    assert len(tracker.request("GET", "streams/2002")["offers"]) == 4
    assert change(c, "pin", 101, pinned=True, share=25).status_code == 200
    s = change(c, "remove", 102).json()
    assert weights(s) == {101: 25, 103: 38, 104: 37}
    assert next(o for o in s["offers"] if o["id"] == 102)["share"] == 0
    assert change(c, "publish").status_code == 200
    assert 102 not in {
        o["offer_id"] for o in tracker.request("GET", "streams/2002")["offers"]
    }
    assert next(o for o in stream(c)["offers"] if o["id"] == 102)["state"] == "removed"
    assert set(weights(change(c, "restore", 102).json()).values()) == {25}
    change(c, "cancel")
    assert next(o for o in stream(c)["offers"] if o["id"] == 102)["state"] == "removed"
    change(c, "restore", 102)
    change(c, "publish")
    tracker.request(
        "PUT",
        "streams/2002",
        json={
            "offers": [
                {"offer_id": 101, "share": 25, "state": "active"},
                {"offer_id": 104, "share": 75, "state": "active"},
            ]
        },
    )
    assert c.post("/api/campaigns/1001/sync", json={}).status_code == 200
    s = stream(c)
    assert not s["dirty"]
    assert {o["id"] for o in s["offers"] if o["state"] == "removed"} == {102, 103}
    assert weights(change(c, "restore", 102).json()) == {101: 25, 102: 38, 104: 37}


def test_drafts_persist_across_application_restart(loaded):
    c, tracker, _, settings = loaded
    add_offer(c)
    with TestClient(create_app(settings, tracker)) as second:
        s = stream(second)
        assert s["dirty"] and 104 in weights(s)
        assert len(tracker.request("GET", "streams/2002")["offers"]) == 3


def test_sync_refuses_to_discard_draft_unless_explicit(loaded):
    c, _, _, _ = loaded
    add_offer(c)
    assert c.post("/api/campaigns/1001/sync", json={}).status_code == 409
    assert stream(c)["dirty"]
    assert (
        c.post("/api/campaigns/1001/sync", json={"discard_drafts": True}).status_code
        == 200
    )
    assert 104 not in {o["id"] for o in stream(c)["offers"]}


@pytest.mark.parametrize("kind", ["offers", "filters", "schema", "landings"])
def test_remote_conflict_preserves_draft(loaded, kind):
    c, tracker, _, _ = loaded
    add_offer(c)
    mutations = {
        "offers": [{"offer_id": 101, "share": 100, "state": "active"}],
        "filters": [{"name": "country", "mode": "accept", "payload": ["US"]}],
        "schema": "redirect",
        "landings": [{"landing_id": 9, "share": 100}],
    }
    tracker.request("PUT", "streams/2002", json={kind: mutations[kind]})
    r = change(c, "publish")
    assert r.status_code == 409 and r.json()["code"] == "remote_conflict"
    assert stream(c)["dirty"]
    assert tracker.request("GET", "streams/2002")[kind] == mutations[kind]


def test_redirect_flow_untouched(loaded):
    c, tracker, _, _ = loaded
    before = tracker.request("GET", "streams/2001")
    add_offer(c)
    change(c, "publish")
    assert tracker.request("GET", "streams/2001") == before
    assert (
        c.post(
            "/api/streams/2001/offers", json={"revision": 1, "offer_id": 104}
        ).status_code
        == 400
    )


def test_stale_tab_and_duplicate_offer(loaded):
    c, _, _, _ = loaded
    revision = stream(c)["revision"]
    assert add_offer(c).status_code == 200
    assert (
        c.post(
            "/api/streams/2002/offers", json={"revision": revision, "offer_id": 105}
        ).status_code
        == 409
    )
    assert add_offer(c).status_code == 409
    assert add_offer(c, 9999).status_code == 404
    assert weights(stream(c)) == {101: 25, 102: 25, 103: 25, 104: 25}


def test_concurrent_requests_do_not_lose_updates(loaded):
    c, _, _, _ = loaded
    revision = stream(c)["revision"]
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda offer_id: c.post(
                    "/api/streams/2002/offers",
                    json={"revision": revision, "offer_id": offer_id},
                ),
                [104, 105],
            )
        )
    assert sorted(r.status_code for r in responses) == [200, 409]
    assert len(weights(stream(c))) == 4


def test_last_offer_can_be_removed_in_draft_but_not_published(loaded):
    c, tracker, _, _ = loaded
    for offer_id in (101, 102, 103):
        assert change(c, "remove", offer_id).status_code == 200
    assert stream(c)["total_share"] == 0
    assert change(c, "publish").status_code == 400
    assert len(tracker.request("GET", "streams/2002")["offers"]) == 3
    assert change(c, "cancel").json()["total_share"] == 100


def test_invalid_pins_roll_back_entire_edit(loaded):
    c, _, _, _ = loaded
    assert change(c, "pin", 101, pinned=True, share=80).status_code == 200
    before = stream(c)
    assert change(c, "pin", 102, pinned=True, share=50).status_code == 400
    assert stream(c) == before
    assert change(c, "pin", 102, pinned=True, share=10).status_code == 200
    assert change(c, "pin", 103, pinned=True, share=5).status_code == 400
    assert change(c, "pin", 103, pinned=True, share=10).status_code == 200
    assert change(c, "remove", 103).status_code == 400
    assert sum(weights(stream(c)).values()) == 100


@pytest.mark.parametrize("share", [-1, 101, 12.5, "25"])
def test_pin_requires_integer_percentage(loaded, share):
    c, _, _, _ = loaded
    assert change(c, "pin", 101, pinned=True, share=share).status_code == 422


def test_non_test_campaign_is_read_only(loaded):
    c, tracker, _, _ = loaded
    tracker.request("PUT", "campaigns/1001", json={"name": "production_campaign"})
    assert c.post("/api/campaigns/1001/sync", json={}).json()["writable"] is False
    assert add_offer(c).status_code == 403


def test_deleted_stream_is_kept_as_history(loaded):
    c, tracker, _, _ = loaded
    tracker.request("PUT", "streams/2002", json={"state": "deleted"})
    assert c.post("/api/campaigns/1001/sync", json={}).status_code == 200
    s = stream(c)
    assert not s["remote_present"] and all(o["state"] == "removed" for o in s["offers"])


def test_disabled_offer_not_silently_enabled(loaded):
    c, tracker, _, _ = loaded
    tracker.request(
        "PUT",
        "streams/2002",
        json={
            "offers": [
                {"offer_id": 101, "share": 100, "state": "active"},
                {"offer_id": 102, "share": 0, "state": "disabled"},
            ]
        },
    )
    c.post("/api/campaigns/1001/sync", json={})
    add_offer(c)
    change(c, "publish")
    remote = tracker.request("GET", "streams/2002")["offers"]
    assert next(o for o in remote if o["offer_id"] == 102)["state"] == "disabled"


def creation_payload(**overrides):
    return {
        "request_id": str(uuid4()),
        "name": "test_terentev_new_campaign",
        "country": "au",
        "offer_id": 101,
        "domain_id": 1,
        "group_id": 1,
        "traffic_source_id": 1,
        **overrides,
    }


def test_create_campaign_two_flows_and_idempotency(environment):
    c, tracker, _, _ = environment
    payload = creation_payload()
    r = c.post("/api/campaigns", json=payload)
    assert r.status_code == 201, r.text
    campaign = r.json()
    raw = tracker.request("GET", f"campaigns/{campaign['id']}")
    assert raw["state"] == "active" and raw["type"] == "position"
    assert (raw["domain_id"], int(raw["group_id"]), raw["traffic_source_id"]) == (
        1,
        1,
        1,
    )
    streams = tracker.request("GET", f"campaigns/{campaign['id']}/streams")
    first, second = streams
    assert first["position"] < second["position"]
    assert first["filters"] == [
        {"name": "country", "mode": "accept", "payload": ["AU"]}
    ]
    assert (
        first["action_payload"] == "https://www.google.com/"
        and first["schema"] == "redirect"
    )
    assert second["type"] == "default" and second["offers"] == [
        {"offer_id": 101, "share": 100, "state": "active"}
    ]
    again = c.post("/api/campaigns", json=payload)
    assert again.status_code == 201 and again.json()["id"] == campaign["id"]
    assert len(tracker.request("GET", "campaigns")) == 2
    assert (
        c.post("/api/campaigns", json={**payload, "country": "US"}).status_code == 409
    )


@pytest.mark.parametrize(
    "change_data,expected",
    [
        ({"name": "campaign"}, 403),
        ({"name": "test_terentev_"}, 400),
        ({"country": "ZZ"}, 422),
        ({"offer_id": 9999}, 404),
        ({"group_id": 9999}, 400),
        ({"domain_id": 0}, 422),
    ],
)
def test_create_validation_does_not_mutate_tracker(environment, change_data, expected):
    c, tracker, _, _ = environment
    assert (
        c.post("/api/campaigns", json=creation_payload(**change_data)).status_code
        == expected
    )
    assert len(tracker.request("GET", "campaigns")) == 1


def test_search_by_offer_id_and_name(loaded):
    c, _, _, _ = loaded
    assert c.get("/api/offers?q=104").json()["items"][0]["id"] == 104
    assert c.get("/api/offers?q=product").json()["total"] == 2
    assert c.get("/api/offers?q=not-found").json()["items"] == []


def test_request_origin_and_content_type_protection(environment):
    c, _, _, _ = environment
    assert c.post("/api/campaigns/1001/sync", data={}).status_code == 415
    assert (
        c.post(
            "/api/campaigns/1001/sync",
            json={},
            headers={"Origin": "https://other.example"},
        ).status_code
        == 403
    )
    assert c.get("/api/config").headers["cache-control"] == "no-store"
