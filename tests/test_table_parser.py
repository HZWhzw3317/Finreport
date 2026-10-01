"""有框线表格解析单元测试。

覆盖两条取值路径：
    1. 能识别列头（期末余额/期初余额）时按列号取值
    2. 无列头时按“科目名后紧邻两列”按位置取值（保留空单元格位置）
"""

from app.enums import StatementType
from app.services.extractor.normalizer import get_normalizer
from app.services.extractor.table_parser import parse_table_page


def test_by_header_columns():
    """有列头时按“期末列/期初列”取值，不受附注列影响。"""
    table = [
        ["项目", "附注", "期末余额", "期初余额"],
        ["货币资金", "1", "100", "200"],
    ]
    items = parse_table_page(table, StatementType.bs, get_normalizer())
    assert len(items) == 1
    assert items[0].item_code == "cash"
    assert items[0].value_current == 100
    assert items[0].value_previous == 200


def test_by_position_when_no_header():
    """无列头时按位置取科目名后的前两个金额。"""
    table = [
        ["货币资金", "100", "200"],
    ]
    items = parse_table_page(table, StatementType.bs, get_normalizer())
    assert len(items) == 1
    assert items[0].value_current == 100
    assert items[0].value_previous == 200


def test_empty_cell_keeps_position():
    """空单元格保留位置：本期为空时 value_current 应为 None 而非错位。"""
    table = [
        ["货币资金", "", "200"],
    ]
    items = parse_table_page(table, StatementType.bs, get_normalizer())
    assert len(items) == 1
    assert items[0].value_current is None
    assert items[0].value_previous == 200
