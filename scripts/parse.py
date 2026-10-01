"""仅解析已下载的 PDF（不涉及云采集/下载）。

示例::

    python scripts/parse.py --report-id 12
    python scripts/parse.py --codes 000001 --years 2023 --no-llm

适用于“PDF 已下载好，只想重跑解析”的场景。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 保证脚本可直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db import close_db, init_db  # noqa: E402
from app.models import Report  # noqa: E402
from app.services import pipeline  # noqa: E402


async def main(args: argparse.Namespace) -> None:
    # 命令行临时禁用 LLM
    if args.no_llm:
        settings.llm_enabled = False
    await init_db()
    try:
        # 优先按报告ID精确选择，否则按代码/年份过滤
        if args.report_id:
            reports = await Report.filter(id__in=[int(x) for x in args.report_id.split(",")])
        else:
            query = Report.all()
            if args.codes:
                query = query.filter(company__stock_code__in=args.codes.split(","))
            if args.years:
                query = query.filter(report_year__in=[int(y) for y in args.years.split(",")])
            reports = await query
        for report in reports:
            result = await pipeline.parse_report(report, force_llm=args.force_llm)
            print(result)
    finally:
        await close_db()


def build_parser() -> argparse.ArgumentParser:
    """定义命令行参数。"""
    parser = argparse.ArgumentParser(description="解析已下载 PDF")
    parser.add_argument("--report-id", help="报告 ID，逗号分隔")
    parser.add_argument("--codes", help="股票代码，逗号分隔")
    parser.add_argument("--years", help="年份，逗号分隔")
    parser.add_argument("--force-llm", action="store_true", help="跳过规则直接调用大模型")
    parser.add_argument("--no-llm", action="store_true", help="禁用大模型兜底")
    return parser


if __name__ == "__main__":
    asyncio.run(main(build_parser().parse_args()))
