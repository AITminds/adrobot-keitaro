import hashlib
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import now
from app.errors import AppError, TrackerError
from app.models import AuditEvent, Campaign, Creation, Offer, Stream, StreamOffer
from app.tracker import as_list
from app.weights import allocate, validate_publish


def is_dirty(stream):
    return any(
        r.draft_only
        or (r.state, r.share, r.pinned)
        != (r.baseline_state, r.baseline_share, r.baseline_pinned)
        for r in stream.offers
    )


def remote_offers(raw):
    result = [
        {
            "offer_id": int(r["offer_id"]),
            "share": int(r.get("share", 0)),
            "state": r.get("state", "active"),
        }
        for r in raw.get("offers", [])
    ]
    if len({r["offer_id"] for r in result}) != len(result) or any(
        r["state"] not in {"active", "disabled"} or not 0 <= r["share"] <= 100
        for r in result
    ):
        raise TrackerError("Keitaro вернул некорректный список офферов.")
    return sorted(result, key=lambda r: r["offer_id"])


def guard(raw):
    """Only editable semantics; ignore counters, timestamps and association IDs."""
    return {
        k: raw.get(k)
        for k in (
            "campaign_id",
            "type",
            "schema",
            "state",
            "filters",
            "landings",
            "action_type",
            "action_payload",
            "offer_selection",
        )
    }


def wanted_offers(stream):
    return sorted(
        [
            {"offer_id": r.offer_id, "share": r.share, "state": r.state}
            for r in stream.offers
            if r.state != "removed"
        ],
        key=lambda r: r["offer_id"],
    )


def stream_view(stream):
    return {
        "id": stream.id,
        "name": stream.name,
        "position": stream.position,
        "editable": stream.editable,
        "remote_present": stream.remote_present,
        "revision": stream.revision,
        "dirty": is_dirty(stream),
        "total_share": sum(r.share for r in stream.offers if r.state == "active"),
        "offers": [
            {
                "id": r.offer_id,
                "name": r.offer.name,
                "state": r.state,
                "share": r.share,
                "pinned": r.pinned,
                "is_new": r.draft_only,
                "changed": r.draft_only
                or (r.state, r.share, r.pinned)
                != (r.baseline_state, r.baseline_share, r.baseline_pinned),
            }
            for r in stream.offers
        ],
    }


def campaign_view(campaign, prefix):
    return {
        "id": campaign.id,
        "name": campaign.name,
        "domain_id": campaign.domain_id,
        "group_id": campaign.group_id,
        "traffic_source_id": campaign.traffic_source_id,
        "state": campaign.state,
        "synced_at": campaign.synced_at.isoformat(),
        "writable": campaign.name.startswith(prefix),
        "streams": [stream_view(s) for s in campaign.streams],
    }


class CampaignService:
    def __init__(self, db: Session, tracker, settings):
        self.db, self.tracker, self.settings = db, tracker, settings

    def campaign(self, campaign_id, lock=False):
        query = select(Campaign).where(Campaign.id == campaign_id)
        if lock:
            query = query.with_for_update()
        item = self.db.scalar(query)
        if item is None:
            raise AppError(
                "Кампания ещё не загружена. Получите её потоки из Keitaro.",
                404,
                "not_found",
            )
        return item

    def editable_stream(self, stream_id, revision):
        campaign_id = self.db.scalar(
            select(Stream.campaign_id).where(Stream.id == stream_id)
        )
        if campaign_id is None:
            raise AppError("Поток не найден.", 404, "not_found")
        campaign = self.campaign(campaign_id, lock=True)
        self.check_prefix(campaign.name)
        stream = self.db.get(Stream, stream_id)
        if stream.revision != revision:
            raise AppError(
                "Поток уже изменён в другой вкладке. Обновите страницу и повторите действие.",
                409,
                "stale_revision",
            )
        if not stream.remote_present:
            raise AppError(
                "Этот поток удалён в Keitaro. Редактирование недоступно.",
                409,
                "stream_removed",
            )
        if not stream.editable:
            raise AppError(
                "Этот поток не содержит офферов. Его настройки сохраняются без изменений."
            )
        return stream

    def check_prefix(self, name):
        if not name.startswith(self.settings.campaign_prefix):
            raise AppError(
                f"Изменять можно только кампании с префиксом {self.settings.campaign_prefix}.",
                403,
                "campaign_scope",
            )

    def audit(self, campaign_id, action, stream_id=None, **details):
        self.db.add(
            AuditEvent(
                campaign_id=campaign_id,
                stream_id=stream_id,
                action=action,
                details=details,
            )
        )

    def remember_offer(self, raw):
        offer = self.db.get(Offer, int(raw["id"]))
        if offer is None:
            offer = Offer(
                id=int(raw["id"]), name=raw.get("name") or f"Оффер #{raw['id']}"
            )
            self.db.add(offer)
        else:
            offer.name = raw.get("name") or offer.name
        return offer

    def merge_stream(self, stream, raw, catalog):
        incoming = remote_offers(raw)
        existing = {r.offer_id: r for r in stream.offers}
        seen = set()
        for item in incoming:
            offer_id = item["offer_id"]
            seen.add(offer_id)
            offer = self.remember_offer(
                catalog.get(offer_id, {"id": offer_id, "name": f"Оффер #{offer_id}"})
            )
            row = existing.get(offer_id)
            if row is None:
                row = StreamOffer(offer_id=offer_id, offer=offer, pinned=False, share=0)
                stream.offers.append(row)
            row.pinned = bool(
                row.pinned and row.share == item["share"] and item["state"] == "active"
            )
            row.state, row.share = item["state"], item["share"]
        for row in stream.offers:
            if row.offer_id not in seen:
                row.state, row.share, row.pinned = "removed", 0, False
            row.baseline_state, row.baseline_share, row.baseline_pinned = (
                row.state,
                row.share,
                row.pinned,
            )
            row.draft_only = False
        stream.name = raw.get("name") or f"Поток #{stream.id}"
        stream.position = raw.get("position", 0)
        stream.editable = bool(incoming) or raw.get("schema") == "landings"
        stream.remote_present = True
        stream.snapshot = raw
        stream.revision = (stream.revision or 0) + 1

    def discard(self, stream):
        for row in list(stream.offers):
            if row.draft_only:
                stream.offers.remove(row)
            else:
                row.state, row.share, row.pinned = (
                    row.baseline_state,
                    row.baseline_share,
                    row.baseline_pinned,
                )

    def sync(self, campaign_id, discard_drafts=False):
        campaign = self.db.scalar(
            select(Campaign).where(Campaign.id == campaign_id).with_for_update()
        )
        if campaign and any(is_dirty(s) for s in campaign.streams):
            if not discard_drafts:
                raise AppError(
                    "Есть неопубликованные изменения. Опубликуйте или отмените их перед синхронизацией.",
                    409,
                    "dirty_drafts",
                )
            for stream in campaign.streams:
                self.discard(stream)
        raw_campaign = self.tracker.request("GET", f"campaigns/{campaign_id}")
        raw_streams = as_list(
            self.tracker.request("GET", f"campaigns/{campaign_id}/streams")
        )
        catalog = {
            int(r["id"]): r for r in as_list(self.tracker.request("GET", "offers"))
        }
        if campaign is None:
            campaign = Campaign(id=campaign_id, name=raw_campaign["name"])
            self.db.add(campaign)
        campaign.name, campaign.state = (
            raw_campaign["name"],
            raw_campaign.get("state", "active"),
        )
        for key in ("domain_id", "group_id", "traffic_source_id"):
            setattr(
                campaign, key, int(raw_campaign[key]) if raw_campaign.get(key) else None
            )
        campaign.alias = raw_campaign.get("alias", "")
        existing = {s.id: s for s in campaign.streams}
        seen = set()
        for raw in raw_streams:
            if raw.get("state") == "deleted":
                continue
            stream_id = int(raw["id"])
            if (
                stream_id in seen
                or int(raw.get("campaign_id", campaign_id)) != campaign_id
            ):
                raise TrackerError(
                    "Keitaro вернул некорректную принадлежность потоков."
                )
            seen.add(stream_id)
            stream = existing.get(stream_id)
            if stream is None:
                stream = Stream(
                    id=stream_id,
                    campaign=campaign,
                    name=raw.get("name", ""),
                    revision=0,
                )
                self.db.add(stream)
            self.merge_stream(stream, raw, catalog)
        for stream in campaign.streams:
            if stream.id not in seen:
                stream.remote_present, stream.editable = False, False
                for row in stream.offers:
                    row.state = row.baseline_state = "removed"
                    row.share = row.baseline_share = 0
                    row.pinned = row.baseline_pinned = False
                    row.draft_only = False
                stream.revision += 1
        campaign.synced_at = now()
        self.audit(campaign_id, "sync", discarded_drafts=discard_drafts)
        self.db.flush()
        return campaign_view(campaign, self.settings.campaign_prefix)

    def mutate(
        self, stream_id, revision, action, offer_id=None, pinned=None, share=None
    ):
        stream = self.editable_stream(stream_id, revision)
        row = next((r for r in stream.offers if r.offer_id == offer_id), None)
        if action in {"add", "restore"}:
            if row and row.state == "active":
                raise AppError(
                    "Этот оффер уже активен в потоке.", 409, "duplicate_offer"
                )
            if action == "restore" and row is None:
                raise AppError("Оффер не найден в архиве потока.", 404, "not_found")
            raw = self.tracker.request("GET", f"offers/{offer_id}")
            if raw.get("state", "active") != "active":
                raise AppError("Оффер недоступен в Keitaro.")
            offer = self.remember_offer(raw)
            if row is None:
                row = StreamOffer(
                    offer_id=offer_id,
                    offer=offer,
                    baseline_state="removed",
                    baseline_share=0,
                    baseline_pinned=False,
                    draft_only=True,
                )
                stream.offers.append(row)
            row.state, row.share, row.pinned = "active", 0, False
            allocate(stream.offers)
        elif action in {"remove", "pin"}:
            if row is None or row.state != "active":
                raise AppError("Активный оффер не найден в потоке.", 404, "not_found")
            if action == "remove":
                row.state, row.share, row.pinned = "removed", 0, False
            else:
                row.pinned = pinned
                if pinned and share is not None:
                    row.share = share
            allocate(stream.offers)
        elif action == "cancel":
            self.discard(stream)
        elif action == "rebalance":
            allocate(stream.offers)
        else:
            raise AppError("Неизвестное действие.")
        stream.revision += 1
        self.audit(stream.campaign_id, action, stream_id=stream.id, offer_id=offer_id)
        self.db.flush()
        return stream_view(stream)

    def publish(self, stream_id, revision):
        stream = self.editable_stream(stream_id, revision)
        if not is_dirty(stream):
            return stream_view(stream)
        validate_publish(stream.offers)
        campaign = self.tracker.request("GET", f"campaigns/{stream.campaign_id}")
        self.check_prefix(campaign["name"])
        current = self.tracker.request("GET", f"streams/{stream.id}")
        desired = wanted_offers(stream)
        if guard(current) != guard(stream.snapshot):
            raise AppError(
                "Настройки потока изменились в Keitaro. Получите потоки заново перед публикацией.",
                409,
                "remote_conflict",
            )
        current_offers = remote_offers(current)
        if (
            current_offers != remote_offers(stream.snapshot)
            and current_offers != desired
        ):
            raise AppError(
                "Офферы изменились в Keitaro. Ваш черновик сохранён. Сверьте изменения и синхронизируйте кампанию.",
                409,
                "remote_conflict",
            )
        if current_offers != desired:
            # PUT changes only offers. Filters, redirects, landings and other fields are untouched.
            self.tracker.request(
                "PUT", f"streams/{stream.id}", json={"offers": desired}
            )
            current = self.tracker.request("GET", f"streams/{stream.id}")
            if remote_offers(current) != desired or guard(current) != guard(
                stream.snapshot
            ):
                raise TrackerError(
                    "Keitaro не подтвердил сохранённое состояние. Черновик сохранён; проверьте поток перед повторной публикацией.",
                    uncertain=True,
                )
        for row in stream.offers:
            row.baseline_state, row.baseline_share, row.baseline_pinned = (
                row.state,
                row.share,
                row.pinned,
            )
            row.draft_only = False
        stream.snapshot = current
        stream.revision += 1
        self.audit(
            stream.campaign_id, "publish", stream_id=stream.id, offer_count=len(desired)
        )
        self.db.flush()
        return stream_view(stream)

    def create(self, data):
        self.check_prefix(data.name)
        if data.name == self.settings.campaign_prefix:
            raise AppError("Добавьте название после префикса.")
        payload = data.model_dump(mode="json", exclude={"request_id"})
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        key = str(data.request_id)
        existing = self.db.get(Creation, key)
        if existing:
            if existing.fingerprint != fingerprint:
                raise AppError(
                    "Этот идентификатор запроса уже использован с другими данными.",
                    409,
                    "idempotency_conflict",
                )
            if existing.status == "success":
                return campaign_view(
                    self.campaign(existing.campaign_id), self.settings.campaign_prefix
                )
            raise AppError(
                existing.message
                or "Создание уже запущено. Проверьте статус операции перед повтором.",
                409,
                "creation_pending",
                operation_id=key,
                campaign_id=existing.campaign_id,
            )
        for path, selected, params in (
            ("domains", data.domain_id, None),
            ("groups", data.group_id, {"type": "campaigns"}),
            ("traffic_sources", data.traffic_source_id, None),
        ):
            rows = as_list(self.tracker.request("GET", path, params=params))
            if not any(
                int(r["id"]) == selected and r.get("state", "active") == "active"
                for r in rows
            ):
                raise AppError(f"Выбранный объект {path} недоступен в Keitaro.")
        offer = self.tracker.request("GET", f"offers/{data.offer_id}")
        if offer.get("state", "active") != "active":
            raise AppError("Выбранный оффер недоступен.")
        operation = Creation(key=key, fingerprint=fingerprint, status="pending")
        self.db.add(operation)
        self.db.commit()  # Durable intent BEFORE the first external write.
        campaign_id = None
        try:
            created = self.tracker.request(
                "POST",
                "campaigns",
                json={
                    "name": data.name,
                    "alias": uuid4().hex[:16],
                    "type": "position",
                    "state": "disabled",
                    "domain_id": data.domain_id,
                    "group_id": str(data.group_id),
                    "traffic_source_id": data.traffic_source_id,
                },
            )
            campaign_id = int(created["id"])
            operation.campaign_id = campaign_id
            self.db.commit()
            self.tracker.request(
                "POST",
                "streams",
                json={
                    "campaign_id": campaign_id,
                    "name": f"Гео {data.country} → Google",
                    "type": "regular",
                    "position": 1,
                    "schema": "redirect",
                    "action_type": "http",
                    "action_payload": "https://www.google.com/",
                    "state": "active",
                    "collect_clicks": True,
                    "filter_or": False,
                    "filters": [
                        {"name": "country", "mode": "accept", "payload": [data.country]}
                    ],
                },
            )
            self.tracker.request(
                "POST",
                "streams",
                json={
                    "campaign_id": campaign_id,
                    "name": "Основные офферы",
                    "type": "default",
                    "position": 2,
                    "schema": "landings",
                    "action_type": "http",
                    "state": "active",
                    "collect_clicks": True,
                    "landings": [],
                    "offers": [
                        {"offer_id": data.offer_id, "share": 100, "state": "active"}
                    ],
                },
            )
            self.tracker.request(
                "PUT", f"campaigns/{campaign_id}", json={"state": "active"}
            )
            result = self.sync(campaign_id)
            operation.status = "success"
            self.audit(campaign_id, "create", country=data.country)
            self.db.commit()
            return result
        except Exception as exc:
            self.db.rollback()
            disabled = False
            if campaign_id is not None:
                try:
                    self.tracker.request(
                        "PUT", f"campaigns/{campaign_id}", json={"state": "disabled"}
                    )
                    disabled = True
                except Exception:
                    pass
            operation = self.db.get(Creation, key)
            operation.status = (
                "uncertain"
                if isinstance(exc, TrackerError) and exc.uncertain
                else "failed"
            )
            detail = (
                f" Кампания #{campaign_id} отключена для проверки."
                if disabled
                else f" Проверьте кампанию #{campaign_id} в Keitaro."
                if campaign_id
                else " Проверьте список кампаний в Keitaro перед новой попыткой."
            )
            operation.message = "Создание не завершено." + detail
            self.db.commit()
            raise AppError(
                operation.message,
                502,
                "creation_failed",
                operation_id=key,
                campaign_id=campaign_id,
                uncertain=operation.status == "uncertain",
            ) from exc
