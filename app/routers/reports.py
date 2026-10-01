"""报告相关接口。

前缀：``/api/reports``
提供报告（公告元数据 + 下载/解析状态）的列表与详情查询。
"""

from fastapi import APIRouter, HTTPException

from app.models import Report
from app.schemas import ReportOut

router = APIRouter(prefix="/api/reports", tags=["reports"])


@router.get("", response_model=list[ReportOut])
async def list_reports(
    company_code: str | None = None,
    report_type: str | None = None,
    year: int | None = None,
    needs_review: bool | None = None,
    limit: int = 200,
):
    """按条件查询报告列表。

    :param company_code: 股票代码
    :param report_type: 报告类型 annual/semi/q1/q3
    :param year: 报告年度
    :param needs_review: 仅返回待人工复核的报告
    :param limit: 条数上限，默认 200
    """
    query = Report.all()
    # 支持通过关联表字段过滤（company__stock_code 会 join company 表）
    if company_code:
        query = query.filter(company__stock_code=company_code)
    if report_type:
        query = query.filter(report_type=report_type)
    if year:
        query = query.filter(report_year=year)
    if needs_review is not None:
        query = query.filter(needs_review=needs_review)
    return await query.limit(limit)


@router.get("/{report_id}", response_model=ReportOut)
async def get_report(report_id: int):
    """按主键查询单个报告详情。"""
    report = await Report.get_or_none(id=report_id)
    if not report:
        raise HTTPException(status_code=404, detail="report not found")
    return report
