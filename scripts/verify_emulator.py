"""Opt-in end-to-end check. Creates ONE test campaign on the named emulator.

Run a live-configured local app first, then:
    uv run python -m scripts.verify_emulator
Only the newly created campaign is changed; it is retained for inspection.
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

import httpx

from app.config import Settings
from app.tracker import KeitaroClient, as_list

EMULATOR = "https://keitaro-for-tests.etolegko.com"


def verify(api, tracker, settings):
    checks = []

    def check(condition, label):
        if not condition:
            raise AssertionError(label)
        checks.append(label)
        print(f"PASS: {label}", flush=True)

    def call(method, path, **kwargs):
        response = api.request(method, path, **kwargs)
        response.raise_for_status()
        return response.json()

    config = call("GET", "/api/config")
    check(
        config["mode"] == "live" and config.get("tracker_url") == EMULATOR,
        "Local application is connected to the external emulator",
    )
    catalog = call("GET", "/api/catalog")
    offers = as_list(tracker.request("GET", "offers"))
    ids = sorted(int(o["id"]) for o in offers if o.get("state", "active") == "active")[
        :4
    ]
    check(len(ids) == 4, "Offer catalog is available")
    first, second, third, fourth = ids
    payload = {
        "request_id": str(uuid4()),
        "name": settings.campaign_prefix
        + "emulator_check_"
        + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"),
        "country": "AU",
        "offer_id": first,
        "domain_id": catalog["domains"][0]["id"],
        "group_id": catalog["groups"][0]["id"],
        "traffic_source_id": catalog["traffic_sources"][0]["id"],
    }
    campaign = call("POST", "/api/campaigns", json=payload)
    campaign_id = campaign["id"]
    print(f"Campaign: {campaign_id}", flush=True)
    remote = tracker.request("GET", f"campaigns/{campaign_id}")
    flows = as_list(tracker.request("GET", f"campaigns/{campaign_id}/streams"))
    check(
        len(flows) == 2 and remote["state"] == "active",
        "Created an active campaign with two flows",
    )
    check(
        all(
            int(remote[k]) == payload[k]
            for k in ("domain_id", "group_id", "traffic_source_id")
        ),
        "Domain, group and traffic source match the selected catalog entries",
    )
    geo = next(s for s in flows if s["type"] == "regular")
    default = next(s for s in flows if s["type"] == "default")
    check(
        geo["position"] == 1
        and geo["schema"] == "redirect"
        and geo["action_payload"] == "https://www.google.com/"
        and [{k: f[k] for k in ("name", "mode", "payload")} for f in geo["filters"]]
        == [{"name": "country", "mode": "accept", "payload": ["AU"]}],
        "Priority-one country filter redirects to Google",
    )
    check(
        default["position"] == 2
        and default["offers"][0]["offer_id"] == first
        and default["offers"][0]["share"] == 100,
        "Default flow starts with the selected offer at 100 percent",
    )
    check(
        call("POST", "/api/campaigns", json=payload)["id"] == campaign_id,
        "Repeating the creation request does not duplicate the campaign",
    )
    sid = default["id"]

    def stream():
        return next(
            s
            for s in call("GET", f"/api/campaigns/{campaign_id}")["streams"]
            if s["id"] == sid
        )

    def weights(s):
        return {o["id"]: o["share"] for o in s["offers"] if o["state"] == "active"}

    def mutate(action, offer_id=None, **extra):
        suffix = action if offer_id is None else f"offers/{offer_id}/{action}"
        return call(
            "POST",
            f"/api/streams/{sid}/{suffix}",
            json={"revision": stream()["revision"], **extra},
        )

    def add(offer_id):
        return call(
            "POST",
            f"/api/streams/{sid}/offers",
            json={"revision": stream()["revision"], "offer_id": offer_id},
        )

    add(second)
    s = add(third)
    check(
        weights(s) == {first: 34, second: 33, third: 33},
        "Three offers receive exact 34/33/33 shares",
    )
    check(
        tracker.request("GET", f"streams/{sid}")["offers"] == default["offers"],
        "Unpublished drafts do not change the remote flow",
    )
    mutate("cancel")
    check(
        weights(stream()) == {first: 100},
        "Cancel restores the baseline and removes new relations",
    )
    add(second)
    add(third)
    mutate("publish")
    mutate("pin", first, pinned=True, share=25)
    add(fourth)
    mutate("publish")
    s = mutate("remove", second)
    check(
        weights(s) == {first: 25, third: 38, fourth: 37},
        "Pinned share is retained when another offer is removed",
    )
    mutate("publish")
    check(
        second
        not in {
            o["offer_id"] for o in tracker.request("GET", f"streams/{sid}")["offers"]
        }
        and next(o for o in stream()["offers"] if o["id"] == second)["state"]
        == "removed",
        "Publish removes the remote relation and retains the local archive",
    )
    mutate("restore", second)
    mutate("publish")
    check(
        set(weights(stream()).values()) == {25}, "Bring back restores four equal shares"
    )
    # Simulate editing our just-created flow directly in the tracker.
    tracker.request(
        "PUT",
        f"streams/{sid}",
        json={
            "offers": [
                {"offer_id": first, "share": 25, "state": "active"},
                {"offer_id": fourth, "share": 75, "state": "active"},
            ]
        },
    )
    call("POST", f"/api/campaigns/{campaign_id}/sync", json={})
    check(
        {o["id"] for o in stream()["offers"] if o["state"] == "removed"}
        == {second, third},
        "Fetch detects external removals and archives them locally",
    )
    mutate("restore", second)
    mutate("restore", third)
    mutate("publish")
    check(
        weights(stream()) == dict.fromkeys(ids, 25) and not stream()["dirty"],
        "Final draft and remote offers are synchronized at 100 percent",
    )
    check(
        tracker.request("GET", f"streams/{geo['id']}") == geo,
        "Offer edits leave the redirect flow unchanged",
    )
    return {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "tracker": EMULATOR,
        "campaign_id": campaign_id,
        "campaign_name": payload["name"],
        "stream_ids": [geo["id"], sid],
        "offer_ids": ids,
        "checks": checks,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-url", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    settings = Settings()
    if (
        settings.adrobot_mode != "live"
        or settings.keitaro_base_url.rstrip("/") != EMULATOR
    ):
        raise SystemExit(
            "This check only runs against the named test emulator in live mode."
        )
    tracker = KeitaroClient(settings)
    auth = (
        (settings.app_username, settings.app_password.get_secret_value())
        if settings.app_username
        else None
    )
    try:
        with httpx.Client(
            base_url=args.app_url, timeout=120, auth=auth, follow_redirects=False
        ) as api:
            result = verify(api, tracker, settings)
        output = Path("artifacts/emulator-verification.json")
        output.parent.mkdir(exist_ok=True)
        output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"Evidence: {output}")
    finally:
        tracker.close()


if __name__ == "__main__":
    main()
