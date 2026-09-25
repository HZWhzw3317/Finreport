"""项目统一枚举定义。

全部继承 ``str, Enum``，成员值即为写入数据库/接口返回的字符串，
便于在 SQL、JSON、日志中直接阅读。模型层通过 ``CharEnumField`` 使用。
"""

from enum import Enum


class ReportType(str, Enum):
    """财报类型（决定巨潮公告的检索分类与标题关键词）。"""

    annual = "annual"  # 年度报告
    semi = "semi"  # 半年度报告
    q1 = "q1"  # 第一季度报告
    q3 = "q3"  # 第三季度报告


class DownloadStatus(str, Enum):
    """PDF 下载状态。"""

    pending = "pending"  # 待下载
    downloading = "downloading"  # 下载中（预留）
    done = "done"  # 已下载
    failed = "failed"  # 下载失败


class ParseStatus(str, Enum):
    """PDF 解析状态。"""

    pending = "pending"  # 待解析
    parsing = "parsing"  # 解析中（预留）
    done = "done"  # 解析完成且校验通过
    needs_review = "needs_review"  # 解析完成但需人工复核（置信度不足）
    failed = "failed"  # 解析失败


class StatementType(str, Enum):
    """三大报表类型。"""

    bs = "bs"  # 资产负债表 Balance Sheet
    is_ = "is"  # 利润表 Income Statement（is 为关键字，加下划线）
    cf = "cf"  # 现金流量表 Cash Flow Statement


class Scope(str, Enum):
    """报表口径。"""

    consolidated = "consolidated"  # 合并报表
    parent = "parent"  # 母公司（单体）报表


class Source(str, Enum):
    """数据来源。"""

    rule = "rule"  # 规则解析
    llm = "llm"  # 大模型兜底
    manual = "manual"  # 人工录入/修正


class JobStatus(str, Enum):
    """后台任务状态。"""

    pending = "pending"  # 排队中
    running = "running"  # 执行中
    done = "done"  # 完成
    failed = "failed"  # 失败
