from copy import deepcopy
from threading import RLock

import httpx
from sqlalchemy import Column, Integer, JSON, MetaData, String, Table, func, select

from app.config import Settings
from app.db import make_engine, now
from app.errors import TrackerError


class KeitaroClient:
    def __init__(self, settings: Settings, transport=None):
        self.client = httpx.Client(
            base_url=settings.keitaro_base_url.rstrip("/") + "/admin_api/v1/",
            headers={
                "Api-Key": settings.keitaro_api_key.get_secret_value(),
                "Accept": "application/json",
            },
            timeout=settings.keitaro_timeout_seconds,
            follow_redirects=False,
            transport=transport,
        )

    def request(self, method, path, *, json=None, params=None):
        try:
            response = self.client.request(
                method, path.lstrip("/"), json=json, params=params
            )
        except httpx.RequestError as exc:
            # Never automatically retry a write: the server may have applied it.
            raise TrackerError(
                "Нет ответа от Keitaro. Проверьте соединение и состояние операции в трекере.",
                uncertain=method != "GET",
            ) from exc
        if response.is_error or response.is_redirect:
            messages = {
                401: "Keitaro отклонил API-ключ.",
                403: "Keitaro запретил доступ (403). Проверьте права и доступность сервера.",
                402: "В этой лицензии Keitaro недоступен Admin API.",
                404: "Объект не найден в Keitaro.",
                429: "Keitaro ограничил частоту запросов. Повторите позже.",
            }
            raise TrackerError(
                messages.get(
                    response.status_code,
                    f"Keitaro вернул HTTP {response.status_code}. Изменение не подтверждено.",
                ),
                status=404 if response.status_code == 404 else 502,
                uncertain=method != "GET" and response.status_code >= 500,
            )
        if response.status_code == 204:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise TrackerError(
                "Keitaro вернул некорректный JSON.", uncertain=method != "GET"
            ) from exc

    def close(self):
        self.client.close()


class DemoKeitaro:
    """Persistent, isolated tracker simulator. No live network requests."""

    def __init__(self, settings: Settings):
        self.engine = make_engine(settings.demo_database_url)
        self.lock = RLock()
        meta = MetaData()
        self.table = Table(
            "entities",
            meta,
            Column("kind", String(32), primary_key=True),
            Column("id", Integer, primary_key=True),
            Column("data", JSON, nullable=False),
        )
        meta.create_all(self.engine)
        with self.engine.begin() as conn:
            if conn.scalar(select(func.count()).select_from(self.table)) == 0:
                self._seed(conn, settings.campaign_prefix)

    def _seed(self, conn, prefix):
        items = {
            "domains": [
                {"id": 1, "name": "demo.example.com", "state": "active", "is_ssl": True}
            ],
            "groups": [{"id": 1, "name": "Тестовые кампании", "type": "campaigns"}],
            "traffic_sources": [
                {"id": 1, "name": "Google Ads", "state": "active"},
                {"id": 2, "name": "Direct", "state": "active"},
            ],
            "offers": [
                {"id": 101, "name": "Design Academy · Базовый курс", "state": "active"},
                {"id": 102, "name": "Design Academy · Интенсив", "state": "active"},
                {"id": 103, "name": "Design Academy · Мастер-класс", "state": "active"},
                {"id": 104, "name": "Design Academy · Подписка", "state": "active"},
                {"id": 105, "name": "Product Lab · Пробный доступ", "state": "active"},
                {"id": 106, "name": "Product Lab · Командный тариф", "state": "active"},
            ],
            "campaigns": [
                {
                    "id": 1001,
                    "name": prefix + "design_academy",
                    "domain_id": 1,
                    "group_id": 1,
                    "traffic_source_id": 1,
                    "alias": "design-academy",
                    "type": "position",
                    "state": "active",
                }
            ],
            "streams": [
                {
                    "id": 2001,
                    "campaign_id": 1001,
                    "name": "Гео → Google",
                    "position": 1,
                    "type": "regular",
                    "schema": "redirect",
                    "action_type": "http",
                    "action_payload": "https://www.google.com/",
                    "state": "active",
                    "filters": [
                        {"name": "country", "mode": "accept", "payload": ["AU"]}
                    ],
                    "offers": [],
                    "landings": [],
                },
                {
                    "id": 2002,
                    "campaign_id": 1001,
                    "name": "Основные офферы",
                    "position": 2,
                    "type": "default",
                    "schema": "landings",
                    "action_type": "http",
                    "state": "active",
                    "filters": [],
                    "landings": [],
                    "offers": [
                        {"offer_id": 101, "share": 34, "state": "active"},
                        {"offer_id": 102, "share": 33, "state": "active"},
                        {"offer_id": 103, "share": 33, "state": "active"},
                    ],
                },
            ],
        }
        for kind, rows in items.items():
            for row in rows:
                self._write(conn, kind, row)

    def _write(self, conn, kind, data):
        data = {**deepcopy(data), "updated_at": now().isoformat()}
        where = (self.table.c.kind == kind) & (self.table.c.id == data["id"])
        exists = conn.execute(select(self.table.c.id).where(where)).first()
        if exists:
            conn.execute(self.table.update().where(where).values(data=data))
        else:
            conn.execute(
                self.table.insert().values(kind=kind, id=data["id"], data=data)
            )
        return data

    def request(self, method, path, *, json=None, params=None):
        parts = path.strip("/").split("/")
        with self.lock, self.engine.begin() as conn:
            kind = parts[0]
            rows = list(
                conn.scalars(
                    select(self.table.c.data)
                    .where(self.table.c.kind == kind)
                    .order_by(self.table.c.id)
                )
            )
            if method == "GET" and len(parts) == 1:
                if kind == "groups":
                    rows = [
                        r
                        for r in rows
                        if r.get("type") == (params or {}).get("type", "campaigns")
                    ]
                return deepcopy(rows)
            if (
                kind == "campaigns"
                and len(parts) == 3
                and parts[2] == "streams"
                and method == "GET"
            ):
                streams = conn.scalars(
                    select(self.table.c.data).where(self.table.c.kind == "streams")
                )
                return deepcopy(
                    sorted(
                        [
                            r
                            for r in streams
                            if r["campaign_id"] == int(parts[1])
                            and r.get("state") != "deleted"
                        ],
                        key=lambda r: r["position"],
                    )
                )
            if (
                method == "POST"
                and len(parts) == 1
                and kind in {"campaigns", "streams"}
            ):
                row = {
                    **deepcopy(json),
                    "id": max(
                        [r["id"] for r in rows]
                        + [1000 if kind == "campaigns" else 2000]
                    )
                    + 1,
                }
                return self._write(conn, kind, row)
            if len(parts) == 2:
                row = next((r for r in rows if r["id"] == int(parts[1])), None)
                if row is None or row.get("state") == "deleted":
                    raise TrackerError("Объект не найден в демотрекере.", status=404)
                if method == "GET":
                    return deepcopy(row)
                if method == "PUT" and kind in {"campaigns", "streams"}:
                    return self._write(
                        conn, kind, {**row, **deepcopy(json), "id": row["id"]}
                    )
            raise TrackerError("Этот метод не поддерживается демотрекером.")

    def close(self):
        self.engine.dispose()


def as_list(value):
    if not isinstance(value, list):
        raise TrackerError("Неожиданный формат списка в ответе Keitaro.")
    return value
