"""订单计价服务的测试。

规格见 specs/pricing.md。
其中 test_target_* 是本次任务的目标测试：校验满减与会员折扣不叠加。
"""

from pricing import total


def _items(*prices):
    return [{"name": "item-%d" % i, "price": p} for i, p in enumerate(prices)]


# ------------------------------------------------------------------ 回归测试
def test_total_of_empty_order_is_zero():
    assert total([], is_member=False) == 0


def test_total_sums_the_prices_of_the_items():
    assert total(_items(30, 20, 12.5), is_member=False) == 62.5


def test_total_without_discount_below_the_threshold():
    assert total(_items(100, 99), is_member=False) == 199


def test_total_applies_the_full_reduction():
    assert total(_items(250), is_member=False) == 220


def test_total_applies_the_member_discount():
    assert total(_items(100), is_member=True) == 90


def test_total_applies_the_member_discount_below_the_threshold():
    assert total(_items(199), is_member=True) == 179.1


# ------------------------------------------------------------------ 目标测试
def test_target_discounts_do_not_stack_when_the_full_reduction_is_larger():
    """满减 30 大于会员折扣 25 时，只能优惠 30。"""
    assert total(_items(250), is_member=True) == 220


def test_target_discounts_do_not_stack_when_the_member_discount_is_larger():
    """会员折扣 100 大于满减 30 时，只能优惠 100。"""
    assert total(_items(1000), is_member=True) == 900
