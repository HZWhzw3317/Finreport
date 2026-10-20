"""有框线表格解析器。

用 pdfplumber 的 ``extract_tables`` 抽取规则表格，并做两件事：
    1. 识别“本期/上期”两列的列号（列头可能是 期末余额/期初余额，
       也可能是 2023年12月31日 这种纯日期）
    2. 逐行提取“科目名 + 两个金额”，做科目归一化

适用场景：表格有完整框线、能被 pdfplumber 正确还原。无框线的报表
由 ``text_parser`` 兜底，二者在 ``rule_extract`` 中合并。
"""

import re

import pdfplumber

from app.enums import StatementType
from app.services.extractor.locator import StatementLocator
from app.services.extractor.normalizer import AccountNormalizer
from app.services.extractor.numbers import detect_unit, parse_amount
from app.services.extractor.result import ParsedItem, ParseResult

# 资产负债表列头关键词
_CUR_BS = ("期末余额", "期末数", "年末余额", "年末数")
_PREV_BS = ("期初余额", "期初数", "年初余额", "年初数")
# 利润表/现金流量表列头关键词
_CUR_IS_CF = ("本期发生额", "本期金额", "本期数", "本年金额", "本年发生额")
_PREV_IS_CF = ("上期发生额", "上期金额", "上期数", "上年金额", "上年发生额")

_LABEL_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z]")  # 含中文或字母即视为“像科目名”
_YEAR_RE = re.compile(r"(20\d{2})")


def _tokens(stype: StatementType) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """返回该报表的 (本期列头关键词, 上期列头关键词)。"""
    if stype == StatementType.bs:
        return _CUR_BS, _PREV_BS
    return _CUR_IS_CF, _PREV_IS_CF


def _cell_kind(cell: str, tokens: tuple[tuple[str, ...], tuple[str, ...]]) -> str | None:
    """判断单元格属于本期列还是上期列。"""
    cur_tokens, prev_tokens = tokens
    if any(t in cell for t in cur_tokens):
        return "cur"
    if any(t in cell for t in prev_tokens):
        return "prev"
    return None


def _detect_columns(table, stype: StatementType):
    """在前几行中寻找列头，返回 (本期列号, 上期列号)，找不到返回 (None, None)。

    策略一：匹配“期末余额/本期金额”等文字列头。
    策略二（退路）：匹配年份/日期列头，年份大者视为本期。
    """
    tokens = _tokens(stype)
    for row in table[:5]:
        cells = [(c or "").replace("\n", "").strip() for c in row]
        cur_idx = prev_idx = None
        for i, c in enumerate(cells):
            kind = _cell_kind(c, tokens)
            if kind == "cur" and cur_idx is None:
                cur_idx = i
            elif kind == "prev" and prev_idx is None:
                prev_idx = i
        if cur_idx is not None or prev_idx is not None:
            return cur_idx, prev_idx

    # 退路：按年份列头判断
    for row in table[:5]:
        cells = [(c or "").replace("\n", "").strip() for c in row]
        years: list[tuple[int, int]] = []
        for i, c in enumerate(cells):
            if "年" not in c and len(c) > 6:
                continue
            m = _YEAR_RE.search(c)
            if m:
                years.append((int(m.group(1)), i))
        if len(years) >= 2:
            years.sort(key=lambda x: -x[0])  # 年份大的在前 = 本期
            return years[0][1], years[1][1]
    return None, None


def _is_header_label(label: str, stype: StatementType) -> bool:
    """判断某“科目名”其实是列头/分区标题，应跳过。"""
    cur_tokens, prev_tokens = _tokens(stype)
    if any(t in label for t in (*cur_tokens, *prev_tokens)):
        return True
    if _YEAR_RE.search(label) and ("年" in label or len(label) <= 6):
        return True
    return label in {"项目", "资产", "负债和所有者权益", "负债和股东权益", "所有者权益", "附注"}


def parse_table_page(
    table, stype: StatementType, normalizer: AccountNormalizer
) -> list[ParsedItem]:
    """解析单个表格对象，返回科目行列表。"""
    cur_idx, prev_idx = _detect_columns(table, stype)
    items: list[ParsedItem] = []
    for row in table:
        if not row:
            continue
        cells = [(c or "").replace("\n", "").strip() for c in row]
        if not any(cells):
            continue

        # 科目名 = 第一个“非数字且含中文/字母”的单元格（跳过序号/附注列）
        label_idx = None
        for i, c in enumerate(cells):
            if c and parse_amount(c) is None and _LABEL_RE.search(c):
                label_idx = i
                break
        if label_idx is None:
            continue
        label = cells[label_idx]
        if _is_header_label(label, stype):
            continue

        # 取值：有明确列号则按列号取；否则取科目名后的前两个数字
        if cur_idx is not None or prev_idx is not None:
            cur = parse_amount(cells[cur_idx]) if cur_idx is not None and cur_idx < len(cells) else None
            prev = parse_amount(cells[prev_idx]) if prev_idx is not None and prev_idx < len(cells) else None
        else:
            # 无列头时按“科目名后紧邻的两列”定位本期/上期。
            # 注意：这里刻意保留空单元格的位置（不跳过 None），因为在
            # 报表跨页/同页拼接时，位置对齐比“跳过空值”更安全；缺值由
            # rule_extract.derive_missing 用勾稽关系补齐。
            nums = [parse_amount(c) for c in cells[label_idx + 1 :]]
            cur = nums[0] if len(nums) >= 1 else None
            prev = nums[1] if len(nums) >= 2 else None

        if cur is None and prev is None:  # 无金额的行（分区标题）跳过
            continue

        mapped = normalizer.lookup(stype, label)
        items.append(
            ParsedItem(
                item_code=mapped[0] if mapped else None,
                item_name_raw=label,
                item_name_std=mapped[1] if mapped else None,
                value_current=cur,
                value_previous=prev,
                sort_order=len(items),
            )
        )
    return items


def _page_crop_bounds(page, key: tuple[str, str]):
    """计算一页中属于当前报表的纵向范围。

    同一页可能同时包含上一张表的尾部与下一张表的开头；按标题行裁剪，
    避免把别的报表数据混进来。银行“合并xx表和xx表”并排页不裁剪。
    """
    current_top = None
    other_top = None
    combined = False
    for line in page.extract_text_lines():
        text = line.get("text", "")
        keys, line_combined = StatementLocator._line_keys(text)
        if not keys:
            # 权益变动表开始处作为现金流量表的结束边界
            if any(k in text for k in ("所有者权益变动表", "股东权益变动表")):
                if other_top is None or line["top"] < other_top:
                    other_top = line["top"]
            continue
        others = keys - {key}
        if key in keys and current_top is None:
            current_top = line["top"]
        if others:
            if line_combined and all(k[0] == key[0] for k in others):
                combined = True
            elif other_top is None:
                other_top = line["top"]
    if combined:
        return None
    top = current_top if current_top is not None else 0
    if other_top is not None and (current_top is None or other_top > current_top):
        bottom = other_top
    else:
        bottom = page.height
    if top >= bottom:
        return None
    return (0, top, page.width, bottom)


def parse_tables(
    pdf_path: str,
    stype: StatementType,
    scope: str,
    normalizer: AccountNormalizer,
    page_range: tuple[int, int],
    locator: StatementLocator | None = None,
) -> ParseResult:
    """在给定页区间内抽取所有表格并汇总为 ParseResult。

    :param locator: 可复用已加载的定位器，避免重复解析整份 PDF。
    """
    start, end = page_range
    result = ParseResult(statement_type=stype, scope=scope, page_range=f"{start}-{end}")
    raw_rows = 0
    key = (stype.value, scope)
    try:
        with pdfplumber.open(pdf_path) as pdf:
            for idx in range(start - 1, min(end, len(pdf.pages))):
                page = pdf.pages[idx]
                bbox = _page_crop_bounds(page, key)
                if bbox:
                    page = page.crop(bbox)
                    if page.height <= 1 or page.width <= 1:
                        continue
                tables = page.extract_tables() or []
                for table in tables:
                    raw_rows += len(table)
                    result.items.extend(parse_table_page(table, stype, normalizer))
    except Exception as exc:  # noqa: BLE001 - 记录异常，交由校验/兜底处理
        result.items.append(
            ParsedItem(item_code=None, item_name_raw=f"__parse_error__:{exc}", item_name_std=None)
        )

    locator = locator or StatementLocator(pdf_path)
    result.unit = detect_unit(locator.page_text(start, end, key=key))
    result.raw_rows = raw_rows
    return result
