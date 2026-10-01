"""报表与科目接口。

前缀：``/api``
    - ``GET /reports/{report_id}/statements``  查询某报告下的报表
    - ``GET /statements/{statement_id}/items`` 查询某报表的科目明细
"""

from fastapi import APIRouter, HTTPException

from app.models import Statement, StatementItem
from app.schemas import StatementItemOut, StatementOut

router = APIRouter(prefix="/api", tags=["statements"])


@router.get("/reports/{report_id}/statements", response_model=list[StatementOut])
async def list_statements(report_id: int, type: str | None = None, scope: str | None = None):
    """查询指定报告下的报表列表。

    :param type: 过滤报表类型 bs/is/cf
    :param scope: 过滤口径 consolidated/parent
    """
    query = Statement.filter(report_id=report_id)
    if type:
        query = query.filter(statement_type=type)
    if scope:
        query = query.filter(scope=scope)
    return await query


@router.get("/statements/{statement_id}/items", response_model=list[StatementItemOut])
async def list_statement_items(statement_id: int):
    """查询指定报表的科目明细（按报表内原始顺序）。"""
    statement = await Statement.get_or_none(id=statement_id)
    if not statement:
        raise HTTPException(status_code=404, detail="statement not found")
    return await StatementItem.filter(statement_id=statement_id).order_by("sort_order")
