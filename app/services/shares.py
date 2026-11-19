"""Offer share math with pinning. Pure functions, no I/O.

Rules (from the customer's editor video):
- removed (archived, grey) offers get share 0;
- pinned active offers keep their share;
- the rest R = 100 - sum(pinned) is split equally between unpinned active offers, in integers,
  the remainder goes to the first ones in stream order (3 offers -> 34/33/33);
- the sum of active shares is always exactly 100. If that is impossible the operation is refused
  (ShareError), nothing is changed.

Keitaro itself does NOT validate the sum (verified: share=50 for a single offer was accepted),
so this module is the only guard.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

TOTAL = 100


class ShareError(ValueError):
    """Business rule violation. `code` is a stable machine-readable reason."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ShareRow(Protocol):
    share: int
    pinned: bool
    removed: bool


def equal_shares(n: int, total: int = TOTAL) -> list[int]:
    if n < 1:
        raise ShareError("no_offers", "No offers to share between")
    base, rest = divmod(total, n)
    return [base + 1 if i < rest else base for i in range(n)]


def _get(row: Any, k: str) -> Any:
    return row[k] if isinstance(row, dict) else getattr(row, k)


def compute(rows: Sequence[Any]) -> list[int]:
    """Return the new share for every row (same order). Raises ShareError if 100% is impossible."""
    active = [i for i, r in enumerate(rows) if not _get(r, "removed")]
    if not active:
        raise ShareError("no_active_offers", "A stream must keep at least one active offer")
    pinned = [i for i in active if _get(rows[i], "pinned")]
    free = [i for i in active if not _get(rows[i], "pinned")]

    for i in pinned:
        s = _get(rows[i], "share")
        if not isinstance(s, int) or s < 1 or s > TOTAL:
            # 0% pinned would make the stream unpublishable (every active offer needs >= 1%)
            raise ShareError("bad_pinned_share", f"Pinned share must be 1..{TOTAL}%, got {s!r}")
    pinned_sum = sum(_get(rows[i], "share") for i in pinned)
    if pinned_sum > TOTAL:
        raise ShareError("pinned_over_100", f"Pinned shares sum to {pinned_sum}% (> {TOTAL}%). Unpin something")
    rest = TOTAL - pinned_sum

    out = [0] * len(rows)
    for i in pinned:
        out[i] = _get(rows[i], "share")
    if not free:
        if rest != 0:
            raise ShareError(
                "all_pinned_not_100",
                f"All active offers are pinned and sum to {pinned_sum}%, not {TOTAL}%. Unpin at least one",
            )
        return out
    if rest < len(free):
        raise ShareError(
            "not_enough_for_unpinned",
            f"Only {rest}% left for {len(free)} unpinned offer(s); each needs at least 1%. Unpin something",
        )
    for i, s in zip(free, equal_shares(len(free), rest), strict=True):
        out[i] = s
    return out


def apply(rows: Sequence[Any]) -> None:
    """compute() and write shares into rows (ORM objects or dicts). All-or-nothing."""
    new = compute(rows)
    for r, s in zip(rows, new, strict=True):
        if isinstance(r, dict):
            r["share"] = s
        else:
            r.share = s


def active_total(rows: Sequence[Any]) -> int:
    return sum(_get(r, "share") or 0 for r in rows if not _get(r, "removed"))


def is_valid(rows: Sequence[Any]) -> bool:
    act = [r for r in rows if not _get(r, "removed")]
    return bool(act) and active_total(rows) == TOTAL and all((_get(r, "share") or 0) >= 1 for r in act)
