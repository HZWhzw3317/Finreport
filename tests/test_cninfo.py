"""巨潮采集客户端的纯函数单元测试（不联网）。

覆盖：交易所/板块推断、默认检索时间窗、公告筛选逻辑。
"""

from app.services.crawler.cninfo import (
    CninfoClient,
    default_window,
    infer_exchange,
    infer_market,
)
from app.enums import ReportType


def test_infer_exchange():
    """代码前缀 -> 交易所映射。"""
    assert infer_exchange("600519") == "sse"
    assert infer_exchange("000001") == "szse"
    assert infer_exchange("002594") == "szse"
    assert infer_exchange("830799") == "bse"


def test_infer_market():
    """代码前缀 -> 板块映射。"""
    assert infer_market("688111") == "科创板"
    assert infer_market("300750") == "创业板"
    assert infer_market("600519") == "主板"


def test_default_window():
    """默认检索时间窗：年报在次年，一季报在当年 4~6 月。"""
    assert default_window(ReportType.annual, 2023) == ("2024-01-01", "2024-12-31")
    assert default_window(ReportType.q1, 2023) == ("2023-04-01", "2023-06-30")


def test_select_announcement_filters_and_picks_latest():
    """应排除摘要/英文版，并在多个候选中取最新公告（更正后）。"""
    # 绕过 __init__ 直接构造实例（避免创建目录等副作用）
    client = CninfoClient.__new__(CninfoClient)
    anns = [
        {"title": "2023年年度报告", "ann_date": "2024-03-15"},
        {"title": "2023年年度报告（更正后）", "ann_date": "2024-04-20"},
        {"title": "2023年年度报告摘要", "ann_date": "2024-03-15"},
        {"title": "2023年年度报告（英文版）", "ann_date": "2024-03-15"},
    ]
    selected = client.select_announcement(anns, ReportType.annual)
    assert selected["title"] == "2023年年度报告（更正后）"


def test_select_announcement_none():
    """无匹配正文时返回 None。"""
    client = CninfoClient.__new__(CninfoClient)
    assert client.select_announcement([{"title": "董事会决议"}], ReportType.annual) is None
