"""初始化数据库表结构。

开发/验证阶段可直接按模型建表；生产环境建议使用 aerich 迁移。

用法::

    python scripts/init_db.py

说明：调用 ``init_db(generate_schemas=True)``，内部使用 safe 模式，
对已存在的表不会重复创建，可安全重复执行。
"""

import asyncio
import sys
from pathlib import Path

# 保证脚本可直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import close_db, init_db  # noqa: E402


async def main() -> None:
    await init_db(generate_schemas=True)  # 建表
    print("数据库表已创建")
    await close_db()


if __name__ == "__main__":
    asyncio.run(main())
