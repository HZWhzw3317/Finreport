"""大模型兜底缓存测试（P0-1）。

验证：提供 file_hash 时，第二次调用命中 LlmCache，不再触发视觉接口。
"""

import json

import pytest
from tortoise import Tortoise

from app.enums import StatementType
from app.services.extractor import llm_fallback
from app.services.extractor.normalizer import get_normalizer


@pytest.fixture
async def db():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"models": ["app.models"]}
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


def _fake_vision(images, prompt):
    """返回一份合法 JSON 的假视觉调用。"""
    return json.dumps(
        {
            "unit": "元",
            "items": [
                {"item_code": "cash", "item_name": "货币资金", "value_current": 100, "value_previous": 200}
            ],
        }
    )


async def _patch(monkeypatch):
    calls = {"vision": 0}

    async def counted_vision(images, prompt):
        calls["vision"] += 1
        return _fake_vision(images, prompt)

    monkeypatch.setattr(llm_fallback, "_render_pages", lambda *a, **k: ["fake-img"])
    monkeypatch.setattr(llm_fallback, "_call_vision", counted_vision)
    return calls


async def test_second_call_hits_cache(db, monkeypatch):
    from app.models import LlmCache

    calls = await _patch(monkeypatch)
    n = get_normalizer()

    r1 = await llm_fallback.extract_with_llm(
        "x.pdf", StatementType.bs, "consolidated", n, (1, 1), "元", file_hash="hash-abc"
    )
    r2 = await llm_fallback.extract_with_llm(
        "x.pdf", StatementType.bs, "consolidated", n, (1, 1), "元", file_hash="hash-abc"
    )

    assert calls["vision"] == 1  # 第二次命中缓存，未再调用模型
    assert await LlmCache.all().count() == 1
    assert r1.code_map["cash"].value_current == 100
    assert r2.code_map["cash"].value_current == 100


async def test_no_hash_skips_cache(db, monkeypatch):
    calls = await _patch(monkeypatch)
    n = get_normalizer()

    await llm_fallback.extract_with_llm(
        "x.pdf", StatementType.bs, "consolidated", n, (1, 1), "元"
    )
    await llm_fallback.extract_with_llm(
        "x.pdf", StatementType.bs, "consolidated", n, (1, 1), "元"
    )

    assert calls["vision"] == 2  # 未提供 file_hash，缓存不生效
