"""抽取过程中的数据载体（dataclass）。

这些对象仅在解析流程内部使用，最终由 pipeline 写入数据库模型。
"""

from dataclasses import dataclass, field

from app.enums import StatementType


@dataclass
class ParsedItem:
    """单条科目数据。

    :param item_code: 标准科目编码；未能在字典中匹配时为 None
    :param item_name_raw: PDF 中的原始科目名
    :param item_name_std: 标准科目名
    :param value_current: 本期/期末金额
    :param value_previous: 上期/期初金额
    :param sort_order: 在报表中的顺序
    :param is_derived: 是否由勾稽关系推算（而非直接抽取）
    """

    item_code: str | None
    item_name_raw: str
    item_name_std: str | None
    value_current: float | None = None
    value_previous: float | None = None
    sort_order: int = 0
    is_derived: bool = False


@dataclass
class ParseResult:
    """一张报表的解析结果。

    :param unit: 金额单位（元/千元/万元...）
    :param source: 来源 rule/llm/manual
    :param page_range: 原 PDF 页码区间，如 "122-123"
    :param raw_rows: 原始行数，用于计算有效行占比
    """

    statement_type: StatementType
    scope: str
    unit: str = "元"
    items: list[ParsedItem] = field(default_factory=list)
    source: str = "rule"
    page_range: str | None = None
    raw_rows: int = 0

    @property
    def code_map(self) -> dict[str, ParsedItem]:
        """已归一化科目（code -> ParsedItem）的便捷索引。"""
        return {it.item_code: it for it in self.items if it.item_code}


@dataclass
class Verdict:
    """校验结论。

    :param passed: 是否通过（无问题且置信度达标）
    :param reasons: 触发的问题码列表（用于决定是否走 LLM 兜底）
    :param missing_core: 缺失的核心科目编码
    :param confidence: 置信度 0~1
    """

    passed: bool
    reasons: list[str] = field(default_factory=list)
    missing_core: list[str] = field(default_factory=list)
    confidence: float = 1.0
