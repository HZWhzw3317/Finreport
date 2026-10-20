"""无框线报表的文本行解析器。

很多年报的报表并非标准表格，pdfplumber 抽不出 table，但按行文本
（``科目名  附注  金额1  金额2``）是有规律的。本模块按行解析：

    - 从行尾向前收集“金额 token”，遇到非金额、非附注 token 停止
    - 取最后两个金额作为 本期/上期（金额列固定为两期）
    - 名称为去除尾部分量与附注后剩余的前缀文本

这样可正确处理带“附注”列的行，例如::

    货币资金  1  274,663  285,277   -> cur=274663, prev=285277
    其中：营业收入  164,699  179,895 -> cur=164699, prev=179895
"""

import re

from app.enums import StatementType
from app.services.extractor.locator import StatementLocator
from app.services.extractor.normalizer import AccountNormalizer
from app.services.extractor.numbers import detect_unit, parse_amount
from app.services.extractor.result import ParsedItem, ParseResult

_CJK_RE = re.compile(r"[\u4e00-\u9fa5]")  # 科目名需含中文
_LABEL_RE = re.compile(r"[\u4e00-\u9fa5A-Za-z]")
_LEADING_NOISE_RE = re.compile(r"^[\s\d]+")  # 去掉行首的页码/序号
# 附注列形态：中文数字编号（“五、1”）或纯数字；单独“附注/注”也算
_NOTE_RE = re.compile(r"^第?[一二三四五六七八九十]+[、.．]?\d*$")


def _is_note(token: str) -> bool:
    """判断 token 是否为附注列（位于金额之前，需忽略）。"""
    return bool(_NOTE_RE.match(token)) or token in {"附注", "注"}


def _split_line(line: str):
    """从行尾向前收集金额/附注 token，返回 (科目名, 金额列表)。

    金额按出现顺序排列；附注 token 被跳过但不计入金额。
    """
    tokens = line.split()
    amounts: list[float] = []
    idx = len(tokens)
    while idx > 0:
        token = tokens[idx - 1]
        value = parse_amount(token)
        if value is not None:
            amounts.insert(0, value)  # 头插，保持从左到右的顺序
            idx -= 1
            continue
        if _is_note(token):
            idx -= 1
            continue
        break  # 遇到正常文字，说明金额区结束
    name = "".join(tokens[:idx])  # 拼接剩余 token 得到科目名
    return name, amounts


def parse_text(
    pdf_path: str,
    stype: StatementType,
    scope: str,
    normalizer: AccountNormalizer,
    page_range: tuple[int, int],
    locator: StatementLocator | None = None,
) -> ParseResult:
    """按行解析给定页区间的报表文本。

    :param locator: 可复用的定位器实例，避免重复加载整份 PDF。
    """
    start, end = page_range
    locator = locator or StatementLocator(pdf_path)
    text = locator.page_text(start, end, key=(stype.value, scope))
    result = ParseResult(
        statement_type=stype,
        scope=scope,
        unit=detect_unit(text),
        page_range=f"{start}-{end}",
    )
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        line = _LEADING_NOISE_RE.sub("", line)  # 去掉行首页码/序号
        if not _LABEL_RE.search(line):
            continue
        name, amounts = _split_line(line)
        if not amounts or not _CJK_RE.search(name):  # 无金额或名称非中文则跳过
            continue
        # 金额列固定两期：最后两个分别为 本期、上期
        if len(amounts) >= 2:
            cur, prev = amounts[-2], amounts[-1]
        else:
            cur, prev = amounts[-1], None
        mapped = normalizer.lookup(stype, name)
        result.items.append(
            ParsedItem(
                item_code=mapped[0] if mapped else None,
                item_name_raw=name,
                item_name_std=mapped[1] if mapped else None,
                value_current=cur,
                value_previous=prev,
                sort_order=len(result.items),
            )
        )
        result.raw_rows += 1
    return result
