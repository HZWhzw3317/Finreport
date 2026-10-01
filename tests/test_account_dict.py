"""account_dict 字典加载测试（P0-3）。

验证 ``AccountNormalizer.load_from_db()`` 能把数据库中人工扩展的
科目/同义词并入归一化映射。
"""

import pytest
from tortoise import Tortoise


@pytest.fixture
async def db():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"models": ["app.models"]}
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


async def test_load_from_db_extends_dictionary(db):
    from app.enums import StatementType
    from app.models import AccountDict
    from app.services.extractor.normalizer import AccountNormalizer

    await AccountDict.create(
        item_code="custom_item",
        item_name_std="测试科目",
        statement_type="bs",
        synonyms=["测试科目别名", "另一种叫法"],
        is_core=False,
        sort_order=999,
    )

    n = AccountNormalizer()
    await n.load_from_db()

    assert n.is_known("custom_item")
    assert n.lookup(StatementType.bs, "测试科目别名")[0] == "custom_item"
    assert n.lookup(StatementType.bs, "另一种叫法")[0] == "custom_item"
    assert n.std_name("custom_item") == "测试科目"
