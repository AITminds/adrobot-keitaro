"""Read-only connectivity check. Does not print API keys or tracker data."""

from app.config import Settings
from app.errors import AppError
from app.tracker import KeitaroClient, as_list


def main():
    settings = Settings()
    if settings.adrobot_mode != "live":
        raise SystemExit("Set ADROBOT_MODE=live in .env to check a real tracker.")
    client = KeitaroClient(settings)
    try:
        for resource in ("campaigns", "domains", "groups", "traffic_sources", "offers"):
            rows = as_list(
                client.request(
                    "GET",
                    resource,
                    params={"type": "campaigns"} if resource == "groups" else None,
                )
            )
            print(f"{resource}: OK ({len(rows)} records)")
    except AppError as exc:
        raise SystemExit(exc.message) from None
    finally:
        client.close()


if __name__ == "__main__":
    main()
