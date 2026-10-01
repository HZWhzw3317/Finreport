"""数据库初始化模块（Tortoise-ORM）。

集中管理 Tortoise 的 ORM 配置与连接生命周期。业务代码在 FastAPI 的
lifespan 启动阶段调用 :func:`init_db`，关闭阶段调用 :func:`close_db`；
命令行脚本则在 ``asyncio.run`` 中成对调用。
"""

from tortoise import Tortoise

from app.config import settings

# Tortoise 的 ORM 配置字典：
#   connections.default 指定默认数据库连接串（来自 .env 的 DATABASE_URL）
#   apps.models.models   声明需要加载的模型模块；aerich.models 供迁移工具使用
#   default_connection   模型默认使用的连接名
TORTOISE_ORM = {
    "connections": {"default": settings.database_url},
    "apps": {
        "models": {
            "models": ["app.models", "aerich.models"],
            "default_connection": "default",
        }
    },
}


async def init_db(generate_schemas: bool = False) -> None:
    """初始化 Tortoise（加载模型、建立连接池配置）。

    :param generate_schemas: 为 True 时按模型直接建表（开发/脚本用）。
        生产环境建议使用 aerich 迁移，保持 ``False``。

    注意：``_enable_global_fallback=True`` 是 tortoise-orm 1.x 的关键参数。
    1.x 使用 ContextVar 保存运行时上下文，而 FastAPI 的 lifespan 与请求
    处理运行在不同的 asyncio 任务中，若不开全局回退，请求时会报
    ``No TortoiseContext is currently active``。
    """
    await Tortoise.init(config=TORTOISE_ORM, _enable_global_fallback=True)
    # safe=True 表示对已存在的表不重复创建，可安全重复执行
    if generate_schemas:
        await Tortoise.generate_schemas(safe=True)
    await _reload_account_dict()


async def _reload_account_dict() -> None:
    """用 account_dict 表覆盖内置科目字典（存在时）。

    ``account_dict`` 为空或尚未建表（如迁移前）时静默跳过，不影响启动。
    """
    try:
        from app.services.extractor.normalizer import get_normalizer

        await get_normalizer().load_from_db()
    except Exception:  # noqa: BLE001 - 字典加载失败不应阻断应用启动
        pass


async def close_db() -> None:
    """关闭数据库连接池并清理全局上下文（应用退出时调用）。"""
    await Tortoise.close_connections()
