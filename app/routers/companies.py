"""公司相关接口。

前缀：``/api/companies``
提供公司列表查询、全量股票列表引导入库、单个/批量导入。
"""

from fastapi import APIRouter, HTTPException

from app.models import Company
from app.schemas import CompanyIn, CompanyOut, ImportCompaniesIn
from app.services.pipeline import sync_stock_list

# tags 用于在接口文档中分组
router = APIRouter(prefix="/api/companies", tags=["companies"])


@router.get("", response_model=list[CompanyOut])
async def list_companies(q: str | None = None, limit: int = 100):
    """查询公司列表。

    :param q: 可选，按股票代码前缀模糊匹配
    :param limit: 返回条数上限，默认 100
    """
    query = Company.all()
    if q:
        query = query.filter(stock_code__startswith=q)
    return await query.limit(limit)


@router.post("/bootstrap")
async def bootstrap_stock_list():
    """拉取巨潮全量股票列表并写入 company 表（幂等，重复执行只更新）。

    建议在首次使用时调用一次。
    """
    count = await sync_stock_list()
    return {"synced": count}


@router.post("", response_model=CompanyOut)
async def add_company(payload: CompanyIn):
    """新增或更新单个公司（按 stock_code 去重）。"""
    company, _ = await Company.update_or_create(
        defaults={"sec_name": payload.sec_name}, stock_code=payload.stock_code
    )
    return company


@router.post("/import")
async def import_companies(payload: ImportCompaniesIn):
    """按代码批量导入公司。

    实现上先同步全量股票列表，再从库中筛选出目标代码；
    返回成功匹配的代码与未找到的代码。
    """
    await sync_stock_list()
    found = await Company.filter(stock_code__in=payload.codes)
    found_codes = {c.stock_code for c in found}
    missing = [c for c in payload.codes if c not in found_codes]
    return {"imported": sorted(found_codes), "missing": missing}


@router.get("/{code}", response_model=CompanyOut)
async def get_company(code: str):
    """按股票代码查询单个公司。"""
    company = await Company.get_or_none(stock_code=code)
    if not company:
        raise HTTPException(status_code=404, detail="company not found")
    return company
