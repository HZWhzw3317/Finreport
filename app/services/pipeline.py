"""业务编排：采集 -> 下载 -> 解析 -> 入库。

对外提供四个可复用函数：
    sync_stock_list    同步巨潮全量股票列表
    sync_reports       按条件查询公告并落库报告元数据
    download_reports   批量下载 PDF
    parse_report       解析单份报告（含大模型兜底）并写入报表/科目

设计要点：
    - 幂等：股票/报告按唯一键 upsert；解析时先删旧科目再写入
    - 混合抽取：规则不达标才调用大模型，按策略合并/替换
    - 状态落库：download_status / parse_status / needs_review / confidence
"""

import logging
from decimal import Decimal
from pathlib import Path

from app.config import settings
from app.enums import ParseStatus, ReportType, Scope, Source, StatementType
from app.models import Company, Report, Statement, StatementItem
from app.services.crawler.cninfo import CninfoClient, default_window
from app.services.extractor.llm_fallback import extract_with_llm
from app.services.extractor.locator import StatementLocator
from app.services.extractor.normalizer import AccountNormalizer, get_normalizer
from app.services.extractor.result import ParsedItem, ParseResult
from app.services.extractor.rule_extract import extract_rule
from app.services.extractor.validator import Validator

logger = logging.getLogger(__name__)

# 目标报表：三大报表 × 合并/母公司，共 6 张
STATEMENT_TARGETS = [
    (StatementType.bs, Scope.consolidated.value),
    (StatementType.bs, Scope.parent.value),
    (StatementType.is_, Scope.consolidated.value),
    (StatementType.is_, Scope.parent.value),
    (StatementType.cf, Scope.consolidated.value),
    (StatementType.cf, Scope.parent.value),
]


async def sync_stock_list() -> int:
    """拉取巨潮全量股票列表并 upsert 到 company 表，返回处理条数。"""
    client = CninfoClient()
    rows = await client.fetch_stock_list()
    count = 0
    for row in rows:
        await Company.update_or_create(defaults=row, stock_code=row["stock_code"])
        count += 1
    return count


async def sync_reports(
    company_codes: list[str] | None,
    report_types: list[ReportType],
    years: list[int],
) -> int:
    """查询并落库报告元数据（公告标题/日期/PDF 链接）。

    :param company_codes: 为空表示全部已入库公司
    :return: 命中的报告数量
    """
    client = CninfoClient()
    if company_codes:
        companies = await Company.filter(stock_code__in=company_codes)
    else:
        companies = await Company.all()
    created = 0
    for company in companies:
        if not company.org_id:  # 没有 orgId 无法查询公告
            continue
        for rt in report_types:
            for year in years:
                start, end = default_window(rt, year)
                try:
                    anns = await client.query_announcements(
                        company.stock_code, company.org_id, rt, start, end
                    )
                except Exception as exc:  # noqa: BLE001 - 单个查询失败不影响整体
                    logger.warning("查询公告失败 %s %s: %s", company.stock_code, rt, exc)
                    continue
                sel = client.select_announcement(anns, rt)  # 选正文
                if not sel:
                    continue
                await Report.update_or_create(
                    defaults={
                        "ann_title": sel["title"],
                        "ann_date": sel.get("ann_date"),
                        "pdf_url": sel.get("pdf_url"),
                        # 年报/半年报期末为 12-31，季报不设置
                        "period_end": f"{year}-12-31"
                        if rt in (ReportType.annual, ReportType.semi)
                        else None,
                    },
                    company_id=company.id,
                    report_type=rt,
                    report_year=year,
                )
                created += 1
    return created


async def download_reports(report_ids: list[int] | None = None) -> dict:
    """批量下载报告 PDF。

    :param report_ids: 为空表示全部报告
    :return: {"total", "success", "failed"}
    """
    client = CninfoClient()
    query = Report.all()
    if report_ids:
        query = query.filter(id__in=report_ids)
    reports = await query
    stats = {"total": len(reports), "success": 0, "failed": 0}
    for report in reports:
        # 本地已存在则跳过（幂等）
        if report.local_path and Path(report.local_path).exists():
            stats["success"] += 1
            continue
        if not report.pdf_url:
            stats["failed"] += 1
            continue
        company = await report.company
        try:
            info = await client.download_pdf(
                report.pdf_url,
                company.stock_code,
                report.report_year,
                report.report_type.value,
            )
            report.local_path = info["local_path"]
            report.file_size = info["file_size"]
            report.file_hash = info["file_hash"]
            report.download_status = "done"
            report.error_message = None
            stats["success"] += 1
        except Exception as exc:  # noqa: BLE001
            report.download_status = "failed"
            report.error_message = str(exc)[:1000]
            stats["failed"] += 1
        await report.save()
    return stats


def _merge_results(rule: ParseResult, llm: ParseResult) -> ParseResult:
    """规则 + LLM 合并：规则条目优先（已有科目不覆盖），LLM 只补缺失科目。"""
    merged = ParseResult(
        statement_type=rule.statement_type,
        scope=rule.scope,
        unit=rule.unit or llm.unit,
        source="llm",
        page_range=rule.page_range or llm.page_range,
    )
    by_code: dict[str, ParsedItem] = {}
    order: list[str] = []
    for item in rule.items:  # 先放规则的
        if item.item_code and item.item_code not in by_code:
            by_code[item.item_code] = item
            order.append(item.item_code)
    for item in llm.items:  # 再用 LLM 补缺失的科目，并回填规则的空值
        if not item.item_code:
            continue
        existing = by_code.get(item.item_code)
        if existing is None:
            by_code[item.item_code] = item
            order.append(item.item_code)
            continue
        # 规则阶段经常“匹配到科目但金额列为空”，此时用 LLM 的值回填，
        # 否则最终入库仍是 null，而校验却按 LLM 结果通过了。
        if existing.value_current is None and item.value_current is not None:
            existing.value_current = item.value_current
        if existing.value_previous is None and item.value_previous is not None:
            existing.value_previous = item.value_previous
        if not existing.item_name_std and item.item_name_std:
            existing.item_name_std = item.item_name_std
    for idx, code in enumerate(order):
        by_code[code].sort_order = idx
    merged.items = [by_code[c] for c in order]
    merged.raw_rows = len(merged.items)
    return merged


async def _persist_statement(
    report: Report, result: ParseResult, confidence: float, needs_review: bool
) -> None:
    """写入单张报表及其科目（先删旧科目，保证幂等）。"""
    stmt, _ = await Statement.update_or_create(
        defaults={
            "unit": result.unit,
            "source": result.source,
            "page_range": result.page_range,
        },
        report_id=report.id,
        statement_type=result.statement_type,
        scope=result.scope,
    )
    await StatementItem.filter(statement_id=stmt.id).delete()  # 清旧数据
    items = [
        StatementItem(
            statement_id=stmt.id,
            item_code=it.item_code or "unmapped",
            item_name_raw=it.item_name_raw,
            item_name_std=it.item_name_std,
            # 用字符串构造 Decimal，避免 float 精度问题
            value_current=Decimal(str(it.value_current)) if it.value_current is not None else None,
            value_previous=Decimal(str(it.value_previous)) if it.value_previous is not None else None,
            sort_order=it.sort_order,
            is_derived=it.is_derived,
        )
        for it in result.items
        if it.item_code  # 仅入库已归一化的科目
    ]
    if items:
        await StatementItem.bulk_create(items)


async def parse_report(
    report: Report,
    normalizer: AccountNormalizer | None = None,
    force_llm: bool = False,
) -> dict:
    """解析单份报告（含兜底），返回摘要信息。

    对每张目标报表：
        1. 规则抽取 + 校验
        2. 未通过且启用 LLM 时，调用视觉兜底
        3. 按 merge_strategy 决定合并或替换
        4. 写入数据库并累计置信度
    """
    normalizer = normalizer or get_normalizer()
    validator = Validator(normalizer)

    # PDF 不存在直接失败
    if not report.local_path or not Path(report.local_path).exists():
        report.parse_status = ParseStatus.failed.value
        report.error_message = "PDF 文件不存在"
        await report.save()
        return {"report_id": report.id, "status": "failed", "error": "pdf_missing"}

    await report.fetch_related("company")
    report.parse_status = ParseStatus.parsing.value
    await report.save()

    # 定位器只创建一次，整份 PDF 文本只解析一次，供 6 张表复用
    locator = StatementLocator(report.local_path)
    ranges = locator.locate()
    confidences: list[float] = []
    needs_review = False
    parsed = 0

    for stype, scope in STATEMENT_TARGETS:
        page_range = ranges.get((stype.value, scope))
        if not page_range:  # 未定位到该表（如无母公司报表）
            continue

        # 规则抽取 + 校验
        result = extract_rule(report.local_path, stype, scope, normalizer, page_range, locator)
        verdict = validator.evaluate(result)
        source = "rule"

        # 决定是否走大模型兜底：启用且（强制 或 规则未通过）
        should_llm = settings.llm_enabled and (force_llm or not verdict.passed)
        if should_llm:
            try:
                # 传入 file_hash 以启用响应缓存
                llm = await extract_with_llm(
                    report.local_path,
                    stype,
                    scope,
                    normalizer,
                    page_range,
                    result.unit,
                    file_hash=report.file_hash,
                )
                v_llm = validator.evaluate(llm)
                strategy = settings.merge_strategy
                if force_llm and not verdict.passed:
                    # 强制兜底：直接采用 LLM 结果
                    chosen = llm
                    confidence = v_llm.confidence
                elif strategy == "replace" or (strategy == "auto" and not result.code_map):
                    # 规则基本没结果 -> 整表替换为 LLM
                    chosen = llm
                    confidence = v_llm.confidence
                else:
                    # 合并：规则优先，LLM 补缺
                    chosen = _merge_results(result, llm)
                    confidence = max(verdict.confidence, v_llm.confidence)
                result = chosen
                source = "llm"
                verdict = v_llm
            except Exception as exc:  # noqa: BLE001 - 兜底失败降级为人工复核
                logger.warning("LLM 兜底失败 report=%s %s/%s: %s", report.id, stype, scope, exc)
                needs_review = True
                confidence = verdict.confidence
        else:
            confidence = verdict.confidence

        # 以“最终采用的 verdict”为准标记复核：若 LLM 已通过，则不再误标
        if not verdict.passed:
            needs_review = True

        result.source = source
        await _persist_statement(report, result, confidence, needs_review)
        confidences.append(confidence)
        parsed += 1

    # 汇总报告级状态
    report.parse_confidence = (
        round(sum(confidences) / len(confidences), 3) if confidences else 0.0
    )
    # 一张报表都没定位到时，不能标记为 done（否则 0 置信度会被误认为通过）
    if parsed == 0:
        needs_review = True
        report.error_message = "未定位到任何报表"
    report.needs_review = needs_review
    report.parse_status = (
        ParseStatus.needs_review.value if needs_review else ParseStatus.done.value
    )
    await report.save()
    return {
        "report_id": report.id,
        "status": report.parse_status,
        "statements": parsed,
        "confidence": report.parse_confidence,
    }
