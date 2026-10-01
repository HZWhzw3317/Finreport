"""把内置标准科目字典写入 account_dict 表。

作用：让字典可持久化、可人工扩展（补充同义词/调整核心科目），
后续可通过 ``AccountNormalizer.load_from_db()`` 在运行期覆盖内置字典。

用法::

    python scripts/seed_accounts.py
"""

import asyncio
import sys
from pathlib import Path

# 保证脚本可直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import close_db, init_db  # noqa: E402
from app.enums import StatementType  # noqa: E402
from app.models import AccountDict  # noqa: E402
from app.services.extractor.accounts import iter_accounts  # noqa: E402
from app.services.extractor.normalizer import get_normalizer  # noqa: E402


async def main() -> None:
    await init_db()
    try:
        count = 0
        # 遍历三大报表的内置科目，逐条 upsert 到 account_dict
        for stype in (StatementType.bs, StatementType.is_, StatementType.cf):
            for code, name, syn, core, order in iter_accounts(stype):
                await AccountDict.update_or_create(
                    defaults={
                        "item_name_std": name,
                        "statement_type": stype,
                        "synonyms": syn,
                        "is_core": core,
                        "sort_order": order,
                    },
                    item_code=code,  # 以 item_code 为唯一键
                )
                count += 1
        # 写入后立即刷新进程内字典，保证“seed 即生效”
        await get_normalizer().load_from_db()
        print(f"已写入 {count} 个标准科目")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
