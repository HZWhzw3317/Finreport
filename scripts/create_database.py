"""根据 DATABASE_URL 自动创建数据库（库不存在时），字符集 utf8mb4。

背景：Tortoise/aerich 只会建表，不会建库；本脚本负责“建库”这一步。

用法::

    python scripts/create_database.py

会从配置读取 DATABASE_URL，解析出主机/端口/账号/密码/库名后连接 MySQL
（不指定库）执行 CREATE DATABASE IF NOT EXISTS。
"""

import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import asyncmy

# 把项目根目录加入 sys.path，保证脚本可直接以 `python scripts/xxx.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402  (需在 sys.path 设置后导入)


def _parse_url(url: str) -> dict:
    """解析 SQLAlchemy/Tortoise 风格的 MySQL 连接串。

    兼容用户名/密码中的 URL 编码（如 %40 表示 @）。
    """
    parsed = urlparse(url)
    if not parsed.path or parsed.path == "/":
        raise SystemExit("DATABASE_URL 缺少数据库名，形如 mysql://user:pass@host:3306/dbname")
    dbname = parsed.path.lstrip("/")
    query = parse_qs(parsed.query)  # 解析 ?charset=utf8mb4
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": unquote(parsed.username or ""),
        "password": unquote(parsed.password or ""),
        "dbname": unquote(dbname),
        "charset": query.get("charset", ["utf8mb4"])[0],
    }


async def main() -> None:
    """连接 MySQL（不指定库）并创建目标数据库。"""
    cfg = _parse_url(settings.database_url)
    # 注意：这里不传 database 参数，因为目标是“创建”这个库
    conn = await asyncmy.connect(
        host=cfg["host"],
        port=cfg["port"],
        user=cfg["user"],
        password=cfg["password"],
    )
    try:
        async with conn.cursor() as cur:
            await cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{cfg['dbname']}` "
                f"CHARACTER SET {cfg['charset']} COLLATE {cfg['charset']}_general_ci"
            )
        await conn.commit()
        print(f"数据库已就绪: {cfg['dbname']} (charset={cfg['charset']})")
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
