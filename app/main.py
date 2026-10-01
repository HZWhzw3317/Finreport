"""FastAPI 应用入口。

职责：
    1. 通过 lifespan 管理数据库连接的启动/关闭
    2. 注册各业务路由
    3. 自定义文档页 /docs、/redoc（改用国内可访问的 CDN，避免白屏）
    4. 根路径 / 重定向到 /docs
    5. 健康检查

启动方式（务必使用本项目 conda 环境）::

    conda activate finreport
    uvicorn app.main:app --reload --port 8000
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import RedirectResponse

from app.config import settings
from app.db import close_db, init_db
from app.routers import companies, jobs, reports, statements

# 全局日志级别（生产可改为 WARNING）
logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时初始化数据库，退出时释放连接。"""
    await init_db()  # 启动：加载 Tortoise 模型与连接配置
    yield
    await close_db()  # 退出：关闭连接池


# docs_url/redoc_url 设为 None，关闭 FastAPI 默认文档路由，改用下方自定义路由
app = FastAPI(
    title=settings.app_name,
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
)

# 注册业务路由
app.include_router(companies.router)  # 公司相关
app.include_router(reports.router)  # 报告相关
app.include_router(statements.router)  # 报表 / 科目相关
app.include_router(jobs.router)  # 下载 / 解析任务


@app.get("/", include_in_schema=False)
async def index():
    """根路径重定向到接口文档，避免访问 / 出现 404。"""
    return RedirectResponse(url="/docs")


@app.get("/docs", include_in_schema=False)
async def custom_swagger_ui_html():
    """自定义 Swagger UI 页面。

    默认模板从 cdn.jsdelivr.net 加载 JS/CSS，在国内常无法访问导致白屏，
    这里改为配置项中的 CDN 地址（默认 bootcdn）。
    """
    return get_swagger_ui_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=f"{app.title} - Swagger UI",
        swagger_js_url=settings.swagger_js_url,
        swagger_css_url=settings.swagger_css_url,
    )


@app.get("/redoc", include_in_schema=False)
async def custom_redoc_html():
    """自定义 ReDoc 页面（默认 CDN 同样替换为国内可访问地址）。"""
    return get_redoc_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=f"{app.title} - ReDoc",
        redoc_js_url=settings.redoc_js_url,
    )


@app.get("/api/health")
async def health():
    """健康检查接口，用于探活。"""
    return {"status": "ok", "app": settings.app_name}
