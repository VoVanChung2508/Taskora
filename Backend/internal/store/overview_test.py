"""
Unit tests for `pct_delta` — Python port of the Go test in
`store/pct_delta_test.go`.

`pct_delta` itself is not defined in the Go snippet provided (it presumably
lives elsewhere in the `store` package), so a matching implementation is
included here, inferred from the test cases: percent change from `prev` to
`cur`, with `prev == 0` treated as a special case to avoid division by zero
(0 -> 0 is 0%, anything else from 0 counts as +100%).

Run with: pytest test_pct_delta.py
"""

import pytest


def pct_delta(cur: int, prev: int) -> float:
    """
    Percent change from `prev` to `cur`.

    Guards against division by zero: no previous data. A rise from zero
    counts as +100%; zero to zero is 0%, not NaN.
    """
    if prev == 0:
        return 100.0 if cur != 0 else 0.0
    return (cur - prev) / prev * 100


@pytest.mark.parametrize(
    "name, cur, prev, want",
    [
        ("growth", 150, 100, 50),
        ("decline", 50, 100, -50),
        ("flat", 100, 100, 0),
        # Guard against division by zero: no previous data.
        ("from zero counts as +100%", 5, 0, 100),
        ("both empty is 0%, not NaN", 0, 0, 0),
        ("dropped to zero", 0, 20, -100),
    ],
)
def test_pct_delta(name, cur, prev, want):
    got = pct_delta(cur, prev)
    assert got == want, f"pct_delta({cur}, {prev}) = {got}, want {want} [{name}]"