from app.errors import AppError


def allocate(rows):
    """Integer percentages. Keep pins; distribute the remainder deterministically."""
    active = sorted((r for r in rows if r.state == "active"), key=lambda r: r.offer_id)
    for row in rows:
        if row.state != "active":
            row.share, row.pinned = 0, False
    if not active:
        return  # An empty draft can still be cancelled; publishing rejects it.
    pinned = [r for r in active if r.pinned]
    if any(not 0 <= r.share <= 100 for r in pinned):
        raise AppError("Закреплённая доля должна быть от 0 до 100%.")
    remaining = 100 - sum(r.share for r in pinned)
    free = [r for r in active if not r.pinned]
    if remaining < 0:
        raise AppError(
            "Сумма закреплённых долей превышает 100%. Уменьшите долю или снимите закрепление."
        )
    if not free and remaining:
        raise AppError(
            "Все доли закреплены, но сумма не равна 100%. Освободите хотя бы одну долю."
        )
    if free:
        base, extra = divmod(remaining, len(free))
        for index, row in enumerate(free):
            row.share = base + (index < extra)


def validate_publish(rows):
    active = [r for r in rows if r.state == "active"]
    if not active:
        raise AppError(
            "Нельзя опубликовать поток без активных офферов. Верните или добавьте оффер."
        )
    if (
        any(not 0 <= r.share <= 100 for r in active)
        or sum(r.share for r in active) != 100
    ):
        raise AppError("Сумма долей активных офферов должна быть ровно 100%.")
