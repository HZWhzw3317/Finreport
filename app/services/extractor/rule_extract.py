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


def derive_missing(result: ParseResult) -> None:
    """用勾稽关系补齐缺失的合计数，并标记 ``is_derived=True``。

    规则：
        - 资产负债：资产总计 = 负债合计 + 所有者权益合计（缺哪补哪）
        - 利润表：  净利润 = 利润总额 - 所得税费用
        - 现金流量：分项净额 = 流入小计 - 流出小计；
                    净增加额 = 经营 + 投资 + 筹资 + 汇率影响

    注意：这里判断的是“值是否为空”，而不是“科目是否存在”。抽取器经常
    会先匹配到科目、但金额列没取到（``value_current=None``），此时仍应补齐。
    """
    cm = result.code_map
    stype = result.statement_type

    def value(code: str):
        item = cm.get(code)
        return item.value_current if item is not None else None

    def previous(code: str):
        item = cm.get(code)
        return item.value_previous if item is not None else None

    def add(code: str, name: str, cur, prev=None) -> None:
        """写入推算值；若科目已存在但值为空，则原地补值。"""
        if cur is None:
            return
        existing = cm.get(code)
        if existing is not None:
            if existing.value_current is None:
                existing.value_current = cur
                existing.value_previous = prev
                existing.is_derived = True
            return
        result.items.append(
            ParsedItem(code, name, name, cur, prev, len(result.items), True)
        )
        # 同步更新索引，后续推算（如先补三项净额、再补净增加额）才能看到新值
        cm[code] = result.items[-1]

    if stype == StatementType.bs:
        ta = value("total_assets")
        tl = value("total_liabilities")
        te = value("total_equity")
        if te is None and ta is not None and tl is not None:
            prev = (
                previous("total_assets") - previous("total_liabilities")
                if previous("total_assets") is not None
                and previous("total_liabilities") is not None
                else None
            )
            add("total_equity", "所有者权益合计", ta - tl, prev)
        if ta is None and tl is not None and te is not None:
            prev = (
                previous("total_liabilities") + previous("total_equity")
                if previous("total_liabilities") is not None
                and previous("total_equity") is not None
                else None
            )
            add("total_assets", "资产总计", tl + te, prev)
    elif stype == StatementType.is_:
        tp = value("total_profit")
        tax = value("income_tax_expense")
        np_ = value("net_profit")
        if np_ is None and tp is not None and tax is not None:
            prev = (
                previous("total_profit") - previous("income_tax_expense")
                if previous("total_profit") is not None
                and previous("income_tax_expense") is not None
                else None
            )
            add("net_profit", "净利润", tp - tax, prev)
    elif stype == StatementType.cf:
        # 分项净额 = 流入小计 - 流出小计
        for target, name, inflow, outflow in (
            ("net_cash_flow_operating", "经营活动产生的现金流量净额", "operating_cash_inflow", "operating_cash_outflow"),
            ("net_cash_flow_investing", "投资活动产生的现金流量净额", "investing_cash_inflow", "investing_cash_outflow"),
            ("net_cash_flow_financing", "筹资活动产生的现金流量净额", "financing_cash_inflow", "financing_cash_outflow"),
        ):
            if value(target) is None:
                in_value = value(inflow)
                out_value = value(outflow)
                if in_value is not None and out_value is not None:
                    prev = (
                        previous(inflow) - previous(outflow)
                        if previous(inflow) is not None
                        and previous(outflow) is not None
                        else None
                    )
                    add(target, name, in_value - out_value, prev)

        # 净增加额 = 经营 + 投资 + 筹资 + 汇率影响
        inc = value("net_increase_in_cash")
        parts = [
            value(c)
            for c in (
                "net_cash_flow_operating",
                "net_cash_flow_investing",
                "net_cash_flow_financing",
            )
        ]
        if inc is None and all(p is not None for p in parts):
            fx = value("exchange_rate_effect") or 0
            add("net_increase_in_cash", "现金及现金等价物净增加额", sum(parts) + fx)


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
