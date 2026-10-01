"""ORM 模型与约束单元测试。

使用内存 SQLite，验证：模型可建表、外键/Decimal 正常、唯一约束生效。
不依赖 MySQL，适合快速本地回归。
"""

import pytest
from tortoise import Tortoise


@pytest.fixture
async def db():
    """为每个测试提供独立的内存数据库与表结构。"""
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"models": ["app.models"]}
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


async def test_models_and_unique_constraint(db):
    """验证基本 CRUD、外键列名、Decimal 精度与唯一约束。"""
    from app.models import Company, Report, Statement, StatementItem

    # 建公司 -> 报告
    company = await Company.create(stock_code="000001", sec_name="平安银行")
    report = await Report.create(
        company=company, report_type="annual", report_year=2023
    )
    assert report.company_id == company.id  # 外键列名为 company_id

    # 建报表 -> 科目
    statement = await Statement.create(
        report=report, statement_type="bs", scope="consolidated"
    )
    await StatementItem.create(
        statement=statement,
        item_code="total_assets",
        item_name_std="资产总计",
        value_current="1000.0000",
        sort_order=0,
    )
    items = await StatementItem.filter(statement_id=statement.id)
    assert len(items) == 1
    assert float(items[0].value_current) == 1000.0

    # 违反 (company, report_type, report_year) 唯一约束应抛异常
    with pytest.raises(Exception):
        await Report.create(
            company=company, report_type="annual", report_year=2023
        )
