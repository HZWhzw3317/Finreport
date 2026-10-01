"""校验器单元测试。

覆盖：资产负债表勾稽通过 / 失败两种情形。
"""

from app.enums import StatementType
from app.services.extractor.normalizer import get_normalizer
from app.services.extractor.result import ParsedItem, ParseResult
from app.services.extractor.validator import Validator


def _item(code, name, cur, prev=None):
    """构造一条测试用科目行。"""
    return ParsedItem(
        item_code=code,
        item_name_raw=name,
        item_name_std=name,
        value_current=cur,
        value_previous=prev,
    )


def test_validator_bs_passes():
    """资产(1000) = 负债(400) + 权益(600)，且核心科目齐全 -> 通过。"""
    result = ParseResult(statement_type=StatementType.bs, scope="consolidated", page_range="10-11")
    result.items = [
        _item("cash", "货币资金", 100),
        _item("current_assets_total", "流动资产合计", 300),
        _item("total_assets", "资产总计", 1000),
        _item("current_liabilities_total", "流动负债合计", 200),
        _item("total_liabilities", "负债合计", 400),
        _item("equity_attributable_to_parent", "归属于母公司所有者权益合计", 600),
        _item("total_equity", "所有者权益合计", 600),
        _item("non_current_assets_total", "非流动资产合计", 700),
    ]
    verdict = Validator(get_normalizer()).evaluate(result)
    assert verdict.passed, verdict.reasons
    assert verdict.confidence >= 0.8


def test_validator_bs_balance_failed():
    """资产(1000) ≠ 负债(400) + 权益(700) -> 应报告 bs_balance_failed。"""
    result = ParseResult(statement_type=StatementType.bs, scope="consolidated", page_range="10-11")
    result.items = [
        _item("cash", "货币资金", 100),
        _item("current_assets_total", "流动资产合计", 300),
        _item("total_assets", "资产总计", 1000),
        _item("current_liabilities_total", "流动负债合计", 200),
        _item("total_liabilities", "负债合计", 400),
        _item("equity_attributable_to_parent", "归属于母公司所有者权益合计", 600),
        _item("total_equity", "所有者权益合计", 700),
        _item("non_current_assets_total", "非流动资产合计", 700),
    ]
    verdict = Validator(get_normalizer()).evaluate(result)
    assert not verdict.passed
    assert "bs_balance_failed" in verdict.reasons
