from types import SimpleNamespace
import random

import pytest

from app.weights import allocate, validate_publish


@pytest.mark.parametrize("count", [1, 2, 3, 6, 7, 13, 99, 100, 101, 200])
def test_rounding_is_exact_and_deterministic(count):
    rows = [
        SimpleNamespace(offer_id=i, state="active", share=0, pinned=False)
        for i in range(count)
    ]
    random.Random(17).shuffle(rows)
    allocate(rows)
    validate_publish(rows)
    shares = {r.offer_id: r.share for r in rows}
    assert sum(shares.values()) == 100
    assert max(shares.values()) - min(shares.values()) <= 1
    assert shares[0] >= shares[count - 1]


@pytest.mark.parametrize("pinned_share", [0, 1, 25, 67, 99, 100])
def test_pins_keep_their_value(pinned_share):
    rows = [
        SimpleNamespace(offer_id=1, state="active", share=pinned_share, pinned=True)
    ]
    rows += [
        SimpleNamespace(offer_id=i, state="active", share=0, pinned=False)
        for i in range(2, 9)
    ]
    allocate(rows)
    validate_publish(rows)
    assert rows[0].share == pinned_share
    assert sum(r.share for r in rows) == 100
