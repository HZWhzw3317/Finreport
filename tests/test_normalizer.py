"""科目名归一化单元测试。

覆盖：标点清理、标准名/同义词映射、核心科目集合。
"""

from app.enums import StatementType
from app.services.extractor.normalizer import get_normalizer, normalize_name


def test_normalize_name_strips_punct():
    """归一化应去除空白与全/半角标点。"""
    assert normalize_name("货币资金") == "货币资金"
    assert normalize_name(" 货 币 资 金 ") == "货币资金"
    # 全角/半角括号应归一为同一结果
    assert normalize_name("实收资本（股本）") == normalize_name("实收资本(股本)")


def test_lookup_standard_and_synonym():
    """标准名与同义词都应映射到正确编码；未知科目返回 None。"""
    n = get_normalizer()
    assert n.lookup(StatementType.bs, "货币资金")[0] == "cash"
    assert n.lookup(StatementType.bs, "资产总计")[0] == "total_assets"
    assert n.lookup(StatementType.bs, "资产合计")[0] == "total_assets"  # 同义词
    assert n.lookup(StatementType.is_, "归属于母公司股东的净利润")[0] == (
        "net_profit_attributable_to_parent"
    )
    assert n.lookup(StatementType.bs, "不存在的科目") is None


def test_core_codes():
    """核心科目集合应包含三大合计，不包含普通科目。"""
    n = get_normalizer()
    core = n.core_codes(StatementType.bs)
    assert "total_assets" in core
    assert "total_liabilities" in core
    assert "total_equity" in core
    assert "goodwill" not in core
