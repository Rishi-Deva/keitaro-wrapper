import pytest

from app.services.shares import ShareError, apply, compute, equal_shares, is_valid


def R(share=0, pinned=False, removed=False):
    return {"share": share, "pinned": pinned, "removed": removed}


# ---------- equal split ----------

@pytest.mark.parametrize("n", range(1, 101))
def test_equal_shares_always_100_and_balanced(n):
    s = equal_shares(n)
    assert len(s) == n and sum(s) == 100 and min(s) >= 1
    assert max(s) - min(s) <= 1
    assert s == sorted(s, reverse=True)  # remainder goes to first offers


def test_known_splits():
    assert equal_shares(1) == [100]
    assert equal_shares(3) == [34, 33, 33]
    assert equal_shares(4) == [25, 25, 25, 25]
    assert equal_shares(7) == [15, 15, 14, 14, 14, 14, 14]
    assert equal_shares(3, 75) == [25, 25, 25]
    assert equal_shares(2, 75) == [38, 37]


# ---------- no pins ----------

def test_video_3_to_4_offers():
    rows = [R(34), R(33), R(33), R(0)]  # 4th just added
    assert compute(rows) == [25, 25, 25, 25]


def test_removed_get_zero_and_rest_rebalance():
    assert compute([R(25), R(25, removed=True), R(25), R(25)]) == [34, 0, 33, 33]


def test_cannot_remove_last_active():
    with pytest.raises(ShareError) as e:
        compute([R(100, removed=True), R(0, removed=True)])
    assert e.value.code == "no_active_offers"


# ---------- pins ----------

def test_video_pin_25_then_bring_back():
    # pinned 25 stays 25, others share 75
    rows = [R(25, pinned=True), R(25), R(25), R(25, removed=True)]
    rows[3]["removed"] = False  # bring back
    assert compute(rows) == [25, 25, 25, 25]
    rows.append(R(0))  # one more offer
    assert compute(rows) == [25, 19, 19, 19, 18]
    assert sum(compute(rows)) == 100


def test_pinned_keeps_value_remainder_not_divisible():
    # 100-30 = 70 among 3 -> 24/23/23, remainder to first unpinned
    assert compute([R(30, pinned=True), R(0), R(0), R(0)]) == [30, 24, 23, 23]
    # pinned not first: remainder still goes to first *unpinned*
    assert compute([R(0), R(40, pinned=True), R(0), R(0), R(0)]) == [15, 40, 15, 15, 15]
    assert compute([R(0), R(41, pinned=True), R(0), R(0), R(0)]) == [15, 41, 15, 15, 14]


def test_pinned_over_100_rejected():
    with pytest.raises(ShareError) as e:
        compute([R(60, pinned=True), R(50, pinned=True), R(0)])
    assert e.value.code == "pinned_over_100"


def test_pinned_exactly_100_with_unpinned_rejected():
    with pytest.raises(ShareError) as e:
        compute([R(100, pinned=True), R(0)])
    assert e.value.code == "not_enough_for_unpinned"


def test_not_enough_for_each_unpinned():
    with pytest.raises(ShareError) as e:
        compute([R(99, pinned=True), R(0), R(0)])
    assert e.value.code == "not_enough_for_unpinned"
    assert compute([R(98, pinned=True), R(0), R(0)]) == [98, 1, 1]


def test_all_pinned_sum_100_ok():
    assert compute([R(60, pinned=True), R(40, pinned=True)]) == [60, 40]


def test_all_pinned_sum_not_100_rejected():
    with pytest.raises(ShareError) as e:
        compute([R(60, pinned=True), R(30, pinned=True)])
    assert e.value.code == "all_pinned_not_100"


def test_all_pinned_after_removing_one_rejected():
    # 50/50 both pinned, remove one -> 50 left, nobody to take it
    rows = [R(50, pinned=True), R(50, pinned=True, removed=True)]
    with pytest.raises(ShareError) as e:
        compute(rows)
    assert e.value.code == "all_pinned_not_100"


def test_removed_pinned_is_ignored():
    # a removed offer's pin does not reserve anything
    assert compute([R(70, pinned=True, removed=True), R(0), R(0)]) == [0, 50, 50]


@pytest.mark.parametrize("bad", [-5, 0, 101, None, 1.5])
def test_bad_pinned_share_rejected(bad):
    with pytest.raises(ShareError) as e:
        compute([R(bad, pinned=True), R(0)])
    assert e.value.code == "bad_pinned_share"


def test_apply_is_all_or_nothing():
    rows = [R(60, pinned=True), R(50, pinned=True), R(7)]
    with pytest.raises(ShareError):
        apply(rows)
    assert [r["share"] for r in rows] == [60, 50, 7]
    rows = [R(30, pinned=True), R(0), R(0)]
    apply(rows)
    assert [r["share"] for r in rows] == [30, 35, 35]


@pytest.mark.parametrize("pinned_val", range(1, 100))
@pytest.mark.parametrize("n_free", [1, 2, 3, 7])
def test_property_sum_always_100(pinned_val, n_free):
    rows = [R(pinned_val, pinned=True)] + [R(0) for _ in range(n_free)]
    if 100 - pinned_val < n_free:
        with pytest.raises(ShareError):
            compute(rows)
        return
    out = compute(rows)
    assert sum(out) == 100 and out[0] == pinned_val and min(out[1:]) >= 1
    assert max(out[1:]) - min(out[1:]) <= 1


def test_is_valid():
    assert is_valid([R(50), R(50), R(0, removed=True)])
    assert not is_valid([R(50)])
    assert not is_valid([R(100), R(0)])  # active offer with 0%
    assert not is_valid([R(0, removed=True)])
