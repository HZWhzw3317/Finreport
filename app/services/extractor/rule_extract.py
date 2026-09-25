"""规则抽取编排。

把“表格解析”和“文本解析”两个结果合并为一张报表的最终规则结果，
并用勾稽关系补齐个别缺失的合计数。

合并优先级：文本解析在前（其金额列识别更可靠），表格解析在后
（用于补充文本漏掉的科目/名称）。同一科目按字段取有效值，不会互相覆盖。
"""

from app.enums import StatementType
from app.services.extractor.normalizer import AccountNormalizer
from app.services.extractor.result import ParsedItem, ParseResult


def _fill(base: ParsedItem, other: ParsedItem) -> None:
    """用 other 的空缺字段填充 base（仅填 None，不覆盖已有值）。"""
    if base.value_current is None and other.value_current is not None:
        base.value_current = other.value_current
    if base.value_previous is None and other.value_previous is not None:
        base.value_previous = other.value_previous
    if not base.item_name_std and other.item_name_std:
        base.item_name_std = other.item_name_std


def _merge(*sources: ParseResult) -> list[ParsedItem]:
    """按 item_code 合并多个来源；先出现的来源字段优先。

    未归一化（无 item_code）的行单独保留在末尾，便于排查。
    """
    merged: dict[str, ParsedItem] = {}
    order: list[str] = []
    unmapped: list[ParsedItem] = []
    for source in sources:
        for item in source.items:
            if not item.item_code:
                unmapped.append(item)
                continue
            if item.item_code not in merged:
                merged[item.item_code] = item
                order.append(item.item_code)
            else:
                _fill(merged[item.item_code], item)  # 补充缺失字段
    result = [merged[code] for code in order]
    for idx, item in enumerate(result):
        item.sort_order = idx
    return result + unmapped


def _sub(a, b):
    """安全相减：任一为 None 则返回 None。"""
    return None if a is None or b is None else a - b


def _add(a, b):
    """安全相加：任一为 None 则返回 None。"""
    return None if a is None or b is None else a + b


def derive_missing(result: ParseResult) -> None:
    """用勾稽关系补齐缺失的合计数，并标记 ``is_derived=True``。

    规则：
        - 资产负债：资产总计 = 负债合计 + 所有者权益合计（缺哪补哪）
        - 利润表：  净利润 = 利润总额 - |所得税费用|
        - 现金流量：净增加额 = 经营 + 投资 + 筹资 净额
    """
    cm = result.code_map
    stype = result.statement_type

    def add(code: str, name: str, cur, prev) -> None:
        # 已存在或本期值缺失时不补
        if code in cm or cur is None:
            return
        result.items.append(
            ParsedItem(code, name, name, cur, prev, len(result.items), True)
        )

    if stype == StatementType.bs:
        ta, tl, te = cm.get("total_assets"), cm.get("total_liabilities"), cm.get("total_equity")
        if te is None and ta and tl:  # 缺所有者权益 -> 资产 - 负债
            add(
                "total_equity",
                "所有者权益合计",
                _sub(ta.value_current, tl.value_current),
                _sub(ta.value_previous, tl.value_previous),
            )
        if ta is None and tl and te:  # 缺资产总计 -> 负债 + 权益
            add(
                "total_assets",
                "资产总计",
                _add(tl.value_current, te.value_current),
                _add(tl.value_previous, te.value_previous),
            )
    elif stype == StatementType.is_:
        tp, tax, np_ = cm.get("total_profit"), cm.get("income_tax_expense"), cm.get("net_profit")
        if np_ is None and tp and tax and tp.value_current is not None and tax.value_current is not None:
            # 所得税费用在不同报表中可能以负号呈现，这里取绝对值
            add("net_profit", "净利润", tp.value_current - abs(tax.value_current), None)
    elif stype == StatementType.cf:
        inc = cm.get("net_increase_in_cash")
        parts = [
            cm.get(c)
            for c in ("net_cash_flow_operating", "net_cash_flow_investing", "net_cash_flow_financing")
        ]
        if inc is None and any(p and p.value_current is not None for p in parts):
            add(
                "net_increase_in_cash",
                "现金及现金等价物净增加额",
                sum(p.value_current for p in parts if p and p.value_current is not None),
                None,
            )


def extract_rule(
    pdf_path: str,
    stype: StatementType,
    scope: str,
    normalizer: AccountNormalizer,
    page_range: tuple[int, int],
    locator=None,
) -> ParseResult:
    """对一张报表执行完整规则抽取（表格 + 文本 + 合并 + 补齐）。

    :param locator: 复用的定位器；为空则各自新建。
    """
    # 延迟导入，避免模块级循环依赖
    from app.services.extractor.table_parser import parse_tables
    from app.services.extractor.text_parser import parse_text

    table = parse_tables(pdf_path, stype, scope, normalizer, page_range, locator)
    text = parse_text(pdf_path, stype, scope, normalizer, page_range, locator)

    merged = ParseResult(
        statement_type=stype,
        scope=scope,
        unit=text.unit or table.unit,  # 优先文本页识别的单位
        source="rule",
        page_range=f"{page_range[0]}-{page_range[1]}",
        items=_merge(text, table),  # 文本优先，表格补充
        raw_rows=max(table.raw_rows, text.raw_rows),
    )
    derive_missing(merged)  # 补齐合计
    return merged
