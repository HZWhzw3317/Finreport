"""端到端 CLI：同步 -> 下载 -> 解析。

示例::

    # 1) 首次：拉取巨潮全量股票列表
    python scripts/crawl.py --bootstrap

    # 2) 同步公告元数据并下载指定年报
    python scripts/crawl.py --codes 000001,002594,600519 --years 2023 --sync --download

    # 3) 解析入库（规则失败自动走大模型兜底）
    python scripts/crawl.py --codes 000001 --years 2023 --parse

    # 4) 强制大模型 / 禁用大模型
    python scripts/crawl.py --codes 000001 --years 2023 --parse --force-llm
    python scripts/crawl.py --codes 000001 --years 2023 --parse --no-llm

说明：这是脚本版流程，便于本地批量跑；服务版通过 /api/jobs 触发。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 保证脚本可直接运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.db import close_db, init_db  # noqa: E402
from app.enums import ReportType  # noqa: E402
from app.models import Report  # noqa: E402
from app.services import pipeline  # noqa: E402


def _resolve(company_codes, report_types, years):
    """构造报告查询（按代码/类型/年份筛选）。"""
    query = Report.all()
    if company_codes:
        query = query.filter(company__stock_code__in=company_codes)
    if report_types:
        query = query.filter(report_type__in=report_types)
    if years:
        query = query.filter(report_year__in=years)
    return query


async def main(args: argparse.Namespace) -> None:
    # 根据命令行开关临时覆盖 LLM 配置（settings 为进程级单例，可直接改字段）
    if args.no_llm:
        settings.llm_enabled = False
    if args.force_llm:
        settings.llm_enabled = True

    await init_db()
    try:
        # 解析命令行参数
        codes = args.codes.split(",") if args.codes else None
        report_types = [ReportType(t) for t in args.types.split(",")]
        years = [int(y) for y in args.years.split(",")] if args.years else [2023]

        # 1) 股票列表
        if args.bootstrap:
            n = await pipeline.sync_stock_list()
            print(f"[bootstrap] 股票列表入库: {n}")

        # 2) 公告元数据
        if args.sync:
            n = await pipeline.sync_reports(codes, report_types, years)
            print(f"[sync] 报告元数据同步: {n}")

        # 3) 下载 PDF
        if args.download:
            reports = await _resolve(codes, report_types, years)
            stats = await pipeline.download_reports([r.id for r in reports])
            print(f"[download] {stats}")

        # 4) 解析入库
        if args.parse:
            reports = await _resolve(codes, report_types, years)
            total = len(reports)
            for idx, report in enumerate(reports, 1):
                # 默认跳过已完成的报告（--only-missing 默认 True）
                if args.only_missing and report.parse_status == "done":
                    continue
                result = await pipeline.parse_report(report, force_llm=args.force_llm)
                print(f"[parse {idx}/{total}] {result}")
    finally:
        await close_db()


def build_parser() -> argparse.ArgumentParser:
    """定义命令行参数。"""
    parser = argparse.ArgumentParser(description="财报采集与解析 CLI")
    parser.add_argument("--codes", help="股票代码，逗号分隔，如 000001,002594")
    parser.add_argument("--types", default="annual", help="annual,semi,q1,q3")
    parser.add_argument("--years", default="2023", help="年份，逗号分隔")
    parser.add_argument("--bootstrap", action="store_true", help="拉取巨潮全量股票列表")
    parser.add_argument("--sync", action="store_true", help="查询并同步公告元数据")
    parser.add_argument("--download", action="store_true", help="下载 PDF")
    parser.add_argument("--parse", action="store_true", help="解析 PDF 并入库")
    parser.add_argument("--only-missing", action="store_true", default=True)
    parser.add_argument("--force-llm", action="store_true", help="跳过规则直接调用大模型")
    parser.add_argument("--no-llm", action="store_true", help="禁用大模型兜底")
    return parser


if __name__ == "__main__":
    asyncio.run(main(build_parser().parse_args()))
