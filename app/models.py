"""数据库 ORM 模型（Tortoise-ORM）。

实体关系概览::

    Company 1 ── n Report 1 ── n Statement 1 ── n StatementItem
    AccountDict   标准科目字典（全局，供归一化使用）
    LlmCache      大模型响应缓存，按 file_hash+页码+模型 去重
    JobLog        采集/解析后台任务日志

字段命名约定：
    - 主键统一为 ``id``（IntField）
    - 外键字段如 ``company`` 在数据库中的列名为 ``company_id``
    - 金额一律使用 ``DecimalField`` 以避免浮点误差
    - 所有表都带 ``created_at``/``updated_at`` 便于审计
"""

from tortoise import fields, models

from app.enums import (
    DownloadStatus,
    JobStatus,
    ParseStatus,
    ReportType,
    Scope,
    Source,
    StatementType,
)


class Company(models.Model):
    """上市公司基础信息（来自巨潮股票列表接口）。"""

    id = fields.IntField(primary_key=True)
    stock_code = fields.CharField(max_length=16, unique=True)  # 股票代码，唯一
    sec_name = fields.CharField(max_length=64, null=True)  # 证券简称
    org_id = fields.CharField(max_length=32, null=True)  # 巨潮机构ID（查询公告必需）
    exchange = fields.CharField(max_length=8, null=True)  # 交易所 szse / sse / bse
    market = fields.CharField(max_length=16, null=True)  # 板块 主板/创业板/科创板/北交所
    created_at = fields.DatetimeField(auto_now_add=True)  # 创建时间（自动）
    updated_at = fields.DatetimeField(auto_now=True)  # 更新时间（自动）

    class Meta:
        table = "company"
        ordering = ["stock_code"]  # 默认按股票代码排序


class Report(models.Model):
    """某一公司某类型某年度的财报报告（含公告元数据与下载/解析状态）。"""

    id = fields.IntField(primary_key=True)
    # 所属公司；删除公司时级联删除其报告
    company = fields.ForeignKeyField(
        "models.Company", related_name="reports", on_delete=fields.CASCADE
    )
    report_type = fields.CharEnumField(ReportType, max_length=16)  # 报告类型
    report_year = fields.IntField()  # 报告年度（如 2023）
    period_end = fields.DateField(null=True)  # 报告期末日期
    ann_title = fields.CharField(max_length=255, null=True)  # 公告标题
    ann_date = fields.DateField(null=True)  # 公告日期（用于选“更正后”最新版本）
    pdf_url = fields.CharField(max_length=512, null=True)  # 巨潮静态资源直链
    local_path = fields.CharField(max_length=512, null=True)  # 本地归档路径
    file_size = fields.BigIntField(null=True)  # 文件字节数
    file_hash = fields.CharField(max_length=64, null=True)  # SHA256，用于去重/缓存键
    download_status = fields.CharEnumField(
        DownloadStatus, default=DownloadStatus.pending, max_length=16
    )
    parse_status = fields.CharEnumField(
        ParseStatus, default=ParseStatus.pending, max_length=16
    )
    parse_confidence = fields.FloatField(null=True)  # 各报表置信度均值
    needs_review = fields.BooleanField(default=False)  # 是否需要人工复核
    error_message = fields.TextField(null=True)  # 最近一次错误信息
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "report"
        # 同一公司+类型+年度唯一，保证重复采集幂等
        unique_together = (("company", "report_type", "report_year"),)
        # 常见查询：按年度/类型筛选
        indexes = (("report_year", "report_type"),)
        ordering = ["-report_year"]  # 默认按年度倒序


class Statement(models.Model):
    """单张报表（报表类型 × 口径），是科目明细的容器。"""

    id = fields.IntField(primary_key=True)
    report = fields.ForeignKeyField(
        "models.Report", related_name="statements", on_delete=fields.CASCADE
    )
    statement_type = fields.CharEnumField(StatementType, max_length=4)  # bs/is/cf
    scope = fields.CharEnumField(Scope, max_length=16)  # 合并/母公司
    currency = fields.CharField(max_length=8, default="CNY")  # 币种
    unit = fields.CharField(max_length=16, null=True)  # 金额单位 元 / 千元 / 万元
    source = fields.CharEnumField(Source, default=Source.rule, max_length=8)  # 数据来源
    page_range = fields.CharField(max_length=32, null=True)  # 在原 PDF 中的页码区间
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "statement"
        # 同一报告下，同一类型+口径唯一
        unique_together = (("report", "statement_type", "scope"),)


class StatementItem(models.Model):
    """报表科目行（归一化后的单条数据）。"""

    id = fields.IntField(primary_key=True)
    statement = fields.ForeignKeyField(
        "models.Statement", related_name="items", on_delete=fields.CASCADE
    )
    item_code = fields.CharField(max_length=64, db_index=True)  # 标准科目编码
    item_name_raw = fields.CharField(max_length=128, null=True)  # PDF 中的原始科目名
    item_name_std = fields.CharField(max_length=128, null=True)  # 标准科目名
    value_current = fields.DecimalField(max_digits=24, decimal_places=4, null=True)  # 本期/期末
    value_previous = fields.DecimalField(
        max_digits=24, decimal_places=4, null=True
    )  # 上期/期初
    sort_order = fields.IntField(default=0)  # 报表内排序
    is_derived = fields.BooleanField(default=False)  # 是否由勾稽关系推算得到

    class Meta:
        table = "statement_item"
        ordering = ["sort_order"]  # 按报表原始顺序返回


class AccountDict(models.Model):
    """标准科目字典：标准名 + 同义词，用于科目名归一化。

    内置字典定义在 ``app/services/extractor/accounts.py``，
    可通过 ``scripts/seed_accounts.py`` 写入本表，再在运行期覆盖/扩展。
    """

    id = fields.IntField(primary_key=True)
    item_code = fields.CharField(max_length=64, unique=True)  # 标准科目编码
    item_name_std = fields.CharField(max_length=128)  # 标准科目名
    statement_type = fields.CharEnumField(StatementType, max_length=4)  # 所属报表
    synonyms = fields.JSONField(null=True)  # 同义词列表（JSON 数组）
    is_core = fields.BooleanField(default=False)  # 是否核心科目（用于校验）
    sort_order = fields.IntField(default=0)

    class Meta:
        table = "account_dict"
        ordering = ["statement_type", "sort_order"]


class LlmCache(models.Model):
    """大模型响应缓存，避免同一页重复调用产生费用。"""

    id = fields.IntField(primary_key=True)
    cache_key = fields.CharField(max_length=128, unique=True)  # file_hash:page:model
    response = fields.TextField(null=True)  # 模型返回的原始 JSON 文本
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "llm_cache"


class JobLog(models.Model):
    """后台任务日志（下载/解析），用于接口查询进度。"""

    id = fields.IntField(primary_key=True)
    task_type = fields.CharField(max_length=32)  # 任务类型 download / parse
    target = fields.TextField(null=True)  # 任务参数快照
    status = fields.CharEnumField(JobStatus, default=JobStatus.pending, max_length=16)
    total = fields.IntField(default=0)  # 目标数量
    success = fields.IntField(default=0)  # 成功数量
    failed = fields.IntField(default=0)  # 失败数量
    message = fields.TextField(null=True)  # 错误/备注信息
    started_at = fields.DatetimeField(null=True)  # 开始时间
    finished_at = fields.DatetimeField(null=True)  # 结束时间
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "job_log"
        ordering = ["-id"]  # 最新的任务在前
