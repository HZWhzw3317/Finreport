"""金额解析与单位识别工具。

财报 PDF 中的数字格式多样，需统一处理：
    - 千分位：``1,234.56`` / 全角逗号
    - 空值符号：``-`` ``—`` ``不适用`` 等（视为 None）
    - 负数：括号写法 ``(123)``、全角括号 ``（123）`` 或前缀负号
    - 单位：元 / 千元 / 万元 / 百万元 / 亿元
"""

import re

# 视为“无数据”的符号集合
_EMPTY_TOKENS = {
    "",
    "-",
    "--",
    "—",
    "——",
    "－",
    "–",
    "不适用",
    "无",
    "n/a",
    "N/A",
    "nil",
    "NULL",
}

# 合法数字（已去掉千分位与空格后）
_NUM_RE = re.compile(r"^\d+(\.\d+)?$")


def parse_amount(text) -> float | None:
    """把单元格文本解析为金额（float）；无法解析或空值返回 None。

    处理步骤：去空格/千分位 -> 判断空值 -> 识别括号/负号 -> 校验数字格式。
    """
    if text is None:
        return None
    s = str(text).strip()
    # 去掉半角/全角千分位逗号与各类空白
    s = s.replace(",", "").replace("，", "")
    s = s.replace(" ", "").replace("\u3000", "").replace("\xa0", "")
    if s in _EMPTY_TOKENS:
        return None

    neg = False
    # 括号表示负数（半角或全角）
    if (s.startswith("(") and s.endswith(")")) or (
        s.startswith("（") and s.endswith("）")
    ):
        neg = True
        s = s[1:-1]
    # 前缀负号
    if s.startswith("-") or s.startswith("－"):
        neg = True
        s = s[1:]

    if not _NUM_RE.match(s):
        return None
    val = float(s)
    return -val if neg else val


# 单位 -> 换算成“元”的倍率（当前仅用于展示/后续换算）
_UNIT_FACTORS = {
    "元": 1,
    "千元": 1_000,
    "万元": 10_000,
    "百万元": 1_000_000,
    "亿元": 100_000_000,
}

# 识别顺序：先长后短，避免“万元”抢先匹配到“百万元”中的“万元”
_UNIT_ORDER = ("百万元", "亿元", "万元", "千元", "元")


def detect_unit(pages_text: str) -> str:
    """从报表页文本中识别金额单位；按最长匹配，默认元。"""
    text = pages_text or ""
    for unit in _UNIT_ORDER:
        if unit in text:
            return unit
    return "元"


def unit_factor(unit: str) -> int:
    """返回单位对应的“元”倍率，未知单位按 1 处理。"""
    return _UNIT_FACTORS.get(unit, 1)
