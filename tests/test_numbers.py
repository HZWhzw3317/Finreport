"""金额解析与单位识别单元测试。

覆盖：千分位、空格、括号负数、各种空值符号、单位识别。
"""

from app.services.extractor.numbers import detect_unit, parse_amount


def test_parse_amount_basic():
    """常规数字与千分位、内部空格。"""
    assert parse_amount("1,234.56") == 1234.56
    assert parse_amount("1234") == 1234.0
    assert parse_amount(" 1 234 ") == 1234.0  # 去除内部空格


def test_parse_amount_negative():
    """括号（半角/全角）与负号均表示负数。"""
    assert parse_amount("(1,234.56)") == -1234.56
    assert parse_amount("（1234.56）") == -1234.56
    assert parse_amount("-1234.56") == -1234.56


def test_parse_amount_empty():
    """空值/占位符号应解析为 None。"""
    assert parse_amount("") is None
    assert parse_amount("-") is None
    assert parse_amount("—") is None
    assert parse_amount(None) is None
    assert parse_amount("不适用") is None


def test_detect_unit():
    """单位识别应按最长匹配，默认元。"""
    assert detect_unit("单位：万元") == "万元"
    assert detect_unit("单位:千元") == "千元"
    assert detect_unit("随便一些文字") == "元"
