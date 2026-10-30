"""订单计价服务。

计价规格见 specs/pricing.md：
  - 满 200 减 30
  - 会员 9 折
  - 两者不叠加，取优惠金额更大的那个
"""


def total(items, is_member):
    """计算订单应付金额。

    :param items: 订单条目，每个条目是 {"name": str, "price": float} 字典
    :param is_member: 订单是否属于会员
    :return: 应付金额，保留两位小数
    """
    amount = sum(float(item["price"]) for item in items)
    discount = 0.0
    if amount >= 200:
        discount = 30.0
    if is_member:
        discount = max(discount, amount * 0.1)
    return round(amount - discount, 2)
