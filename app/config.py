"""全局配置模块。

统一从环境变量与项目根目录下的 ``.env`` 文件读取配置，供整个项目复用。

配置优先级（pydantic-settings 规则）：
    显式传入的参数 > 环境变量 > ``.env`` 文件 > 代码中的默认值

用法::

    from app.config import settings
    print(settings.database_url)

说明：``settings`` 是一个进程内单例（见文件末尾 ``get_settings`` 的 lru_cache），
在应用运行期间只解析一次配置；测试或脚本中如需临时覆盖，可直接修改
``settings`` 上对应字段（例如 ``settings.llm_enabled = False``）。
"""

import os
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """项目配置项定义。

    字段名会自动映射为同名（不区分大小写）的环境变量，例如字段
    ``database_url`` 对应环境变量 ``DATABASE_URL``。
    """

    # 读取项目根目录的 .env；extra="ignore" 表示 .env 中多余键不报错
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ===== 应用信息 =====
    app_name: str = "finreport"  # 应用名称（FastAPI title、健康检查返回）
    debug: bool = True  # 调试开关（预留）

    # ===== 数据库与存储 =====
    # Tortoise 连接串，使用 asyncmy 驱动；末尾可带 ?charset=utf8mb4
    database_url: str = "mysql://finreport:finreport@127.0.0.1:3306/finreport"
    pdf_dir: str = "./data/pdfs"  # 下载的 PDF 归档目录

    # ===== 大模型（OpenAI 兼容接口）=====
    llm_enabled: bool = True  # 是否启用大模型兜底
    llm_base_url: str = "https://api.deepseek.com/v1"  # 接口地址
    llm_api_key: str = ""  # 密钥；为空时会尝试从环境变量 deepseek 读取
    llm_model: str = "deepseek-flash"  # 模型名（须与服务端支持列表一致）
    llm_max_pages_per_statement: int = 6  # 单张报表最多渲染多少页送给模型（控制成本）
    llm_render_dpi: int = 200  # 页面渲染为图片时的分辨率
    llm_temperature: float = 0.0  # 采样温度，抽取任务用 0 保证稳定
    llm_confidence_threshold: float = 0.8  # 规则结果置信度达到该值才认为通过

    # ===== 规则抽取阈值 =====
    rule_min_items: int = 8  # 规则结果归一条目少于此值判定为不达标
    core_missing_ratio: float = 0.3  # 核心科目缺失比例超过该值触发兜底
    merge_strategy: str = "auto"  # auto | merge | replace，规则与 LLM 结果的合并策略

    # ===== 采集参数 =====
    download_delay: float = 1.5  # 每次下载后的休眠秒数（限速、规避反爬）
    max_retry: int = 3  # 网络请求最大重试次数

    # ===== 接口文档静态资源（默认国内可访问的 CDN）=====
    # 若 /docs 白屏，通常是无法访问默认的 cdn.jsdelivr.net，可在此替换
    swagger_js_url: str = (
        "https://cdn.bootcdn.net/ajax/libs/swagger-ui/5.17.14/swagger-ui-bundle.min.js"
    )
    swagger_css_url: str = (
        "https://cdn.bootcdn.net/ajax/libs/swagger-ui/5.17.14/swagger-ui.min.css"
    )
    redoc_js_url: str = "https://unpkg.com/redoc@2/bundles/redoc.standalone.js"

    @model_validator(mode="after")
    def _resolve_api_key(self) -> "Settings":
        """密钥回填：未在 .env 配置时，从环境变量读取。

        依次尝试 ``deepseek``、``DEEPSEEK_API_KEY``、``DEEPSEEK_KEY``，
        命中第一个非空值即写入 ``llm_api_key``。
        """
        # 空值或仍是占位符（.env.example 中的 sk-xxxx）时才回填
        if not self.llm_api_key or self.llm_api_key.startswith("sk-xxxx"):
            for name in ("deepseek", "DEEPSEEK_API_KEY", "DEEPSEEK_KEY"):
                value = os.environ.get(name)
                if value:
                    self.llm_api_key = value.strip()
                    break
        return self


@lru_cache
def get_settings() -> Settings:
    """返回进程内唯一的 Settings 实例（避免重复解析 .env）。"""
    return Settings()


# 模块级单例，业务代码统一从 `app.config.settings` 导入
settings = get_settings()
