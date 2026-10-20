"""报表解析复核标记测试（P0-2）。

验证：规则未通过但大模型兜底结果通过时，``needs_review`` 应为 False，
不再出现“置信度 1.0 却仍需复核”的矛盾。
"""

import pytest
from tortoise import Tortoise

from app.enums import StatementType
from app.services import pipeline
from app.services.extractor.result import ParsedItem, ParseResult


@pytest.fixture
async def db():
    await Tortoise.init(
        db_url="sqlite://:memory:", modules={"models": ["app.models"]}
    )
    await Tortoise.generate_schemas()
    yield
    await Tortoise.close_connections()


class _FakeLocator:
    """只返回资产负债表合并口径，避免依赖真实 PDF。"""

    def __init__(self, path):
        self.path = path

    def locate(self):
        return {("bs", "consolidated"): (1, 1)}

    def page_text(self, start, end, key=None):
        return ""


def _failing_rule() -> ParseResult:
    r = ParseResult(statement_type=StatementType.bs, scope="consolidated", page_range="1-1")
    r.raw_rows = 0
    return r


def _passing_bs(source: str = "llm") -> ParseResult:
    r = ParseResult(
        statement_type=StatementType.bs, scope="consolidated", unit="元",
        page_range="1-1", source=source,
    )
    r.items = [
        ParsedItem("cash", "货币资金", "货币资金", 100, None),
        ParsedItem("current_assets_total", "流动资产合计", "流动资产合计", 300, None),
        ParsedItem("total_assets", "资产总计", "资产总计", 1000, None),
        ParsedItem("current_liabilities_total", "流动负债合计", "流动负债合计", 200, None),
        ParsedItem("total_liabilities", "负债合计", "负债合计", 400, None),
        ParsedItem("equity_attributable_to_parent", "归属于母公司所有者权益合计", "归属于母公司所有者权益合计", 600, None),
        ParsedItem("total_equity", "所有者权益合计", "所有者权益合计", 600, None),
        ParsedItem("non_current_assets_total", "非流动资产合计", "非流动资产合计", 700, None),
    ]
    r.raw_rows = 8
    return r


async def _make_report(tmp_path):
    from app.models import Company, Report

    pdf = tmp_path / "fake.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    company = await Company.create(stock_code="999999", sec_name="测试公司")
    return await Report.create(
        company=company,
        report_type="annual",
        report_year=2023,
        local_path=str(pdf),
        file_hash="fake-hash",
    )


async def test_llm_pass_clears_needs_review(db, tmp_path, monkeypatch):
    report = await _make_report(tmp_path)

    monkeypatch.setattr(pipeline, "StatementLocator", _FakeLocator)
    monkeypatch.setattr(pipeline, "extract_rule", lambda *a, **k: _failing_rule())

    async def fake_llm(*a, **k):
        return _passing_bs()

    monkeypatch.setattr(pipeline, "extract_with_llm", fake_llm)

    summary = await pipeline.parse_report(report)
    await report.refresh_from_db()

    assert summary["confidence"] == 1.0
    assert report.needs_review is False
    assert report.parse_status == "done"


async def test_llm_fail_keeps_needs_review(db, tmp_path, monkeypatch):
    report = await _make_report(tmp_path)

    monkeypatch.setattr(pipeline, "StatementLocator", _FakeLocator)
    monkeypatch.setattr(pipeline, "extract_rule", lambda *a, **k: _failing_rule())

    async def fake_llm(*a, **k):
        return _failing_rule()

    monkeypatch.setattr(pipeline, "extract_with_llm", fake_llm)

    await pipeline.parse_report(report)
    await report.refresh_from_db()

    assert report.needs_review is True
    assert report.parse_status == "needs_review"
