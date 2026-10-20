"""推导/合并/校验的空值处理测试。

覆盖三个容易回归的点：
    1. 科目存在但金额为空时，仍应参与勾稽推导；
    2. 规则抽到空值时，LLM 的值应能回填；
    3. 空值核心科目不能被计入覆盖率。
"""

from app.enums import StatementType
from app.services.extractor.normalizer import get_normalizer
from app.services.extractor.result import ParsedItem, ParseResult
from app.services.extractor.rule_extract import derive_missing
from app.services.extractor.validator import Validator
from app.services.pipeline import _merge_results


def test_derive_missing_fills_equity_from_assets_and_liabilities():
    result = ParseResult(statement_type=StatementType.bs, scope="consolidated", page_range="1-1")
    result.items = [
        ParsedItem("total_assets", "资产总计", "资产总计", 100.0),
        ParsedItem("total_liabilities", "负债合计", "负债合计", 40.0),
        ParsedItem("total_equity", "所有者权益合计", "所有者权益合计", None),
    ]

    derive_missing(result)

    item = result.code_map["total_equity"]
    assert item.value_current == 60.0
    assert item.is_derived is True


def test_derive_missing_fills_operating_cash_flow_from_subtotals():
    result = ParseResult(statement_type=StatementType.cf, scope="consolidated", page_range="1-1")
    result.items = [
        ParsedItem("operating_cash_inflow", "经营活动现金流入小计", None, 100.0),
        ParsedItem("operating_cash_outflow", "经营活动现金流出小计", None, 60.0),
        ParsedItem("net_cash_flow_operating", "经营活动产生的现金流量净额", None, None),
    ]

    derive_missing(result)

    item = result.code_map["net_cash_flow_operating"]
    assert item.value_current == 40.0
    assert item.is_derived is True


def test_merge_fills_null_rule_value_from_llm():
    rule = ParseResult(statement_type=StatementType.is_, scope="consolidated", page_range="1-1")
    rule.items = [ParsedItem("net_profit", "净利润", "净利润", None)]
    llm = ParseResult(statement_type=StatementType.is_, scope="consolidated", page_range="1-1")
    llm.items = [ParsedItem("net_profit", "净利润", "净利润", 100.0)]

    merged = _merge_results(rule, llm)

    assert merged.code_map["net_profit"].value_current == 100.0


def test_validator_does_not_count_null_core_values():
    result = ParseResult(statement_type=StatementType.bs, scope="consolidated", page_range="1-1")
    result.items = [
        ParsedItem("cash", "货币资金", "货币资金", 10.0),
        ParsedItem("total_assets", "资产总计", "资产总计", 100.0),
        ParsedItem("total_liabilities", "负债合计", "负债合计", 40.0),
        ParsedItem("equity_attributable_to_parent", "归母权益", "归母权益", None),
        ParsedItem("total_equity", "所有者权益合计", "所有者权益合计", None),
    ]
    result.raw_rows = len(result.items)

    verdict = Validator(get_normalizer()).evaluate(result)

    assert "core_missing" in verdict.reasons
    assert "total_equity" in verdict.missing_core
