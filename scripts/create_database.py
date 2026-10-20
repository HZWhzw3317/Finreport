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

# 把项目根目录加入 sys.path，保证脚本可直接以 `python scripts/xxx.py` 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402  (需在 sys.path 设置后导入)
from app.db import create_database, parse_mysql_url  # noqa: E402


async def main() -> None:
    """连接 MySQL（不指定库）并创建目标数据库。"""
    cfg = parse_mysql_url(settings.database_url)
    await create_database(settings.database_url)
    print(f"数据库已就绪: {cfg['dbname']} (charset={cfg['charset']})")


if __name__ == "__main__":
    asyncio.run(main())
