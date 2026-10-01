"""接口出入参模型（Pydantic v2）。

分为三类：
    - ``*In``   请求体（客户端 -> 服务端）
    - ``*Out``  响应体（服务端 -> 客户端）
    - ``*JobIn`` 后台任务参数

所有 ``*Out`` 均设置 ``from_attributes=True``，以便直接将 Tortoise 模型
对象序列化为响应（FastAPI 会读取对象属性）。
"""

from datetime import date
from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class CompanyIn(BaseModel):
    """新增单个公司的请求体。"""

    stock_code: str
    sec_name: Optional[str] = None


class CompanyOut(BaseModel):
    """公司信息响应体。"""

    model_config = ConfigDict(from_attributes=True)  # 允许从 ORM 对象构造
    id: int
    stock_code: str
    sec_name: Optional[str] = None
    org_id: Optional[str] = None
    exchange: Optional[str] = None
    market: Optional[str] = None


class ImportCompaniesIn(BaseModel):
    """批量导入公司的请求体。"""

    codes: List[str]  # 股票代码列表


class ReportOut(BaseModel):
    """报告信息响应体（含下载/解析状态）。"""

    model_config = ConfigDict(from_attributes=True)
    id: int
    company_id: int  # 外键列名
    report_type: str
    report_year: int
    ann_title: Optional[str] = None
    ann_date: Optional[date] = None
    pdf_url: Optional[str] = None
    local_path: Optional[str] = None
    download_status: str
    parse_status: str
    parse_confidence: Optional[float] = None
    needs_review: bool = False
    error_message: Optional[str] = None


class StatementItemOut(BaseModel):
    """报表科目行响应体。"""

    model_config = ConfigDict(from_attributes=True)
    id: int
    item_code: str
    item_name_raw: Optional[str] = None
    item_name_std: Optional[str] = None
    value_current: Optional[float] = None
    value_previous: Optional[float] = None
    sort_order: int = 0


class StatementOut(BaseModel):
    """单张报表响应体。"""

    model_config = ConfigDict(from_attributes=True)
    id: int
    report_id: int
    statement_type: str
    scope: str
    currency: str
    unit: Optional[str] = None
    source: str
    page_range: Optional[str] = None


class DownloadJobIn(BaseModel):
    """创建下载任务的请求体。"""

    company_codes: Optional[List[str]] = None  # 为空表示全部公司
    report_types: List[str] = ["annual"]  # 报告类型
    years: Optional[List[int]] = None  # 为空表示全部年度


class ParseJobIn(BaseModel):
    """创建解析任务的请求体。"""

    report_ids: Optional[List[int]] = None  # 直接指定报告ID（优先级最高）
    company_codes: Optional[List[str]] = None
    report_types: List[str] = ["annual"]
    years: Optional[List[int]] = None
    force_llm: bool = False  # 跳过规则、直接调用大模型
    only_missing: bool = True  # 只解析尚未完成的报告


class JobOut(BaseModel):
    """任务状态响应体。"""

    model_config = ConfigDict(from_attributes=True)
    id: int
    task_type: str
    target: Optional[str] = None
    status: str
    total: int
    success: int
    failed: int
    message: Optional[str] = None
