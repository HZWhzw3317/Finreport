"""规则结果校验器。

综合“数量、覆盖率、勾稽关系”等信号，判断一张报表的规则抽取是否可信，
并给出置信度与问题原因（原因码同时用于决定是否触发大模型兜底）。

问题码（reasons）：
    parse_exception  解析过程抛异常
    empty_items      没有任何条目
    too_few_items    归一化条目数 < RULE_MIN_ITEMS
    no_page_range    未定位到页码
    core_missing     核心科目缺失比例超阈值
    row_align        有效条目 / 原始行数 过低（列错位迹象）
    bs_balance_failed  资产总计 ≠ 负债合计 + 所有者权益合计
    is_profit_failed   净利润 ≠ 利润总额 - |所得税费用|
    cf_failed          现金净增加额 ≠ 经营 + 投资 + 筹资 净额
"""

from app.config import settings
from app.enums import StatementType
from app.services.extractor.normalizer import AccountNormalizer
from app.services.extractor.result import ParseResult, Verdict


def _close(a: float, b: float, rel: float = 0.01, abs_tol: float = 1.0) -> bool:
    """近似相等：允许 1% 相对误差或 1 个单位的绝对误差（财报四舍五入）。"""
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs_tol, abs(b) * rel)


def _val(result: ParseResult, code: str):
    """取某科目的本期值（缺失返回 None）。"""
    item = result.code_map.get(code)
    if not item:
        return None
    return item.value_current


class Validator:
    """报表校验器。"""

    def __init__(self, normalizer: AccountNormalizer):
        self.normalizer = normalizer

    def evaluate(self, result: ParseResult) -> Verdict:
        """执行全部校验，返回 Verdict。"""
        stype = result.statement_type
        reasons: list[str] = []
        core = self.normalizer.core_codes(stype)
        codes = set(result.code_map.keys())
        missing = sorted(core - codes)  # 缺失的核心科目

        # ---- 数量/覆盖类检查 ----
        if any(i.item_name_raw.startswith("__parse_error__") for i in result.items):
            reasons.append("parse_exception")
        if not result.items:
            reasons.append("empty_items")
        if len(result.code_map) < settings.rule_min_items:
            reasons.append("too_few_items")
        if not result.page_range:
            reasons.append("no_page_range")
        if core and (len(missing) / len(core)) > settings.core_missing_ratio:
            reasons.append("core_missing")
        if result.raw_rows and len(result.code_map) / max(result.raw_rows, 1) < 0.15:
            reasons.append("row_align")

        # ---- 勾稽关系检查 ----
        if stype == StatementType.bs:
            ta = _val(result, "total_assets")
            tl = _val(result, "total_liabilities")
            te = _val(result, "total_equity")
            if ta is not None and tl is not None and te is not None and not _close(ta, tl + te):
                reasons.append("bs_balance_failed")
        elif stype == StatementType.is_:
            np_ = _val(result, "net_profit")
            tp = _val(result, "total_profit")
            tax = _val(result, "income_tax_expense")
            # 所得税费用取绝对值，兼容报表中以负号列示的写法
            if np_ is not None and tp is not None and tax is not None and not _close(np_, tp - abs(tax)):
                reasons.append("is_profit_failed")
        elif stype == StatementType.cf:
            inc = _val(result, "net_increase_in_cash")
            net = sum(
                v
                for v in (
                    _val(result, "net_cash_flow_operating"),
                    _val(result, "net_cash_flow_investing"),
                    _val(result, "net_cash_flow_financing"),
                )
                if v is not None
            )
            if inc is not None and net != 0 and not _close(inc, net):
                reasons.append("cf_failed")

        # ---- 由问题码累计扣分得到置信度 ----
        penalties = {
            "parse_exception": 0.5,
            "empty_items": 0.6,
            "too_few_items": 0.3,
            "no_page_range": 0.4,
            "core_missing": 0.3,
            "row_align": 0.2,
            "bs_balance_failed": 0.3,
            "is_profit_failed": 0.3,
            "cf_failed": 0.3,
        }
        confidence = 1.0 - sum(penalties.get(r, 0.1) for r in reasons)
        confidence = max(0.0, round(confidence, 3))
        # 无任何问题且置信度达标才算通过；否则触发兜底
        passed = not reasons and confidence >= settings.llm_confidence_threshold
        return Verdict(passed=passed, reasons=reasons, missing_core=missing, confidence=confidence)
