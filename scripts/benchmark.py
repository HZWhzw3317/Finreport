"""可复现的评测脚本：抽样 -> 采集 -> 下载 -> 解析 -> 指标。

用法::

    # 冒烟：5 家，重建评测库
    python scripts/benchmark.py run --limit 5 --recreate

    # 正式：50 家，2023 年报
    python scripts/benchmark.py run --limit 50 --year 2023 --recreate

    # 只重算指标（不重新采集）
    python scripts/benchmark.py metrics --year 2023

安全约束：
    1. 默认把连接串中的库名替换为 ``finreport_bench``；
    2. 库名为 ``finreport`` 时直接退出，避免误写生产库；
    3. PDF 默认落 ``data/pdfs_bench``，不污染原 ``data/pdfs``；
    4. ``--recreate`` 才会删除并重建评测库，避免误删。

指标口径见 ``benchmarks/README.md``。
"""

import argparse
import asyncio
import json
import os
import random
import sys
import time
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

# 保证脚本可直接运行
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_DB_NAME = "finreport_bench"
DEFAULT_DB_URL = "mysql://finreport:finreport@127.0.0.1:3306/finreport"

# 板块抽样权重（总和为 1，50 家时正好 29/12/6/3）
MARKET_WEIGHTS = {"主板": 0.58, "创业板": 0.24, "科创板": 0.12, "北交所": 0.06}
MARKET_ORDER = ["主板", "创业板", "科创板", "北交所"]

# 金融企业识别关键词（company 表没有行业字段，只能用简称做启发式判断）
FINANCIAL_KEYWORDS = ("银行", "保险", "证券", "信托", "期货")


# ============================================================================
# 配置与连接串
# ============================================================================
def _read_env_value(key: str, default: str | None = None) -> str | None:
    """从项目根目录 .env 读取一个键（不依赖 pydantic，供设置环境变量前使用）。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return default
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() == key:
            return v.strip().strip('"').strip("'")
    return default


def _with_db_name(url: str, db_name: str) -> str:
    """把 MySQL 连接串中的库名替换为 db_name，保留账号/主机/查询参数。"""
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + db_name))


def _resolve_db_url(args) -> tuple[str, str]:
    """确定评测库连接串，并做生产库保护。"""
    if getattr(args, "db_url", None):
        url = args.db_url
    else:
        base = (
            os.environ.get("DATABASE_URL")
            or _read_env_value("DATABASE_URL")
            or DEFAULT_DB_URL
        )
        url = _with_db_name(base, args.db_name)

    db_name = urlsplit(url).path.lstrip("/")
    if not db_name:
        raise SystemExit(f"连接串缺少库名: {url}")
    if db_name == "finreport":
        raise SystemExit(
            "拒绝写入生产库 finreport。请使用 --db-name finreport_bench 或显式 --db-url。"
        )
    return url, db_name


def _configure_env(url: str, pdf_dir: Path, no_llm: bool) -> None:
    """在 import app.config 之前写入环境变量。

    注意：``app.config.settings`` 是进程级单例，必须在首次 import 前设置，
    否则会读回 .env 里的旧库/旧目录。
    """
    os.environ["DATABASE_URL"] = url
    os.environ["PDF_DIR"] = str(pdf_dir)
    os.environ["LLM_ENABLED"] = "false" if no_llm else "true"


# ============================================================================
# 抽样
# ============================================================================
def _eligible(row: dict) -> bool:
    """过滤掉不适合评测的股票：ST、B 股、非 A 股板块。"""
    code = (row.get("stock_code") or "").strip()
    name = (row.get("sec_name") or "").strip()
    if len(code) != 6:
        return False
    if code.startswith(("900", "200")):  # B 股
        return False
    if not code.startswith(("60", "68", "00", "30", "4", "8", "92")):
        return False
    if name.upper().lstrip("*").startswith("ST"):
        return False
    return row.get("market") in MARKET_WEIGHTS


def _is_financial(row: dict) -> bool:
    """用证券简称判断是否金融企业（启发式）。"""
    name = row.get("sec_name") or ""
    return any(k in name for k in FINANCIAL_KEYWORDS)


def _allocate(limit: int) -> dict[str, int]:
    """按板块权重分配名额（最大余数法），保证总和等于 limit。"""
    exact = {m: limit * MARKET_WEIGHTS[m] for m in MARKET_ORDER}
    quotas = {m: int(exact[m]) for m in MARKET_ORDER}
    rest = limit - sum(quotas.values())
    # 余数从大到小分配；同余数时按固定板块顺序，保证可复现
    order = sorted(MARKET_ORDER, key=lambda m: (-(exact[m] - quotas[m]), MARKET_ORDER.index(m)))
    for m in order[:rest]:
        quotas[m] += 1
    return quotas


async def _select_sample(limit: int, financial_target: int, seed: int):
    """分层抽样：先抽金融企业，再按板块配额补足。

    返回 (selected, backup)：
        selected  最终入选的 limit 家（若候选不足则更少）
        backup    同板块的备选池，用于 sync 后补位（无 2023 年报等）
    """
    from app.models import Company

    rows = await Company.all().values("stock_code", "sec_name", "market")
    pool = [r for r in rows if _eligible(r)]

    rng = random.Random(seed)
    # 先排序再打乱，保证不同机器上结果一致
    pool.sort(key=lambda r: r["stock_code"])
    rng.shuffle(pool)

    financial = [r for r in pool if _is_financial(r)]
    non_financial = [r for r in pool if not _is_financial(r)]
    financial_target = min(financial_target, len(financial), limit)

    picked_fin = financial[:financial_target]
    picked_codes = {r["stock_code"] for r in picked_fin}

    selected = list(picked_fin)
    if len(selected) >= limit:
        selected = selected[:limit]
        selected.sort(key=lambda r: r["stock_code"])
        backup = [r for r in pool if r["stock_code"] not in picked_codes]
        return selected, backup

    quotas = _allocate(limit)
    by_market = defaultdict(list)
    for r in non_financial:
        by_market[r["market"]].append(r)

    # 金融企业已占用对应板块名额
    used = defaultdict(int)
    for r in picked_fin:
        used[r["market"]] += 1

    for market in MARKET_ORDER:
        need = max(0, quotas[market] - used[market])
        take = by_market[market][:need]
        selected.extend(take)
        picked_codes.update(r["stock_code"] for r in take)

    # 候选不足时，从其它板块补足；超出则截断
    if len(selected) < limit:
        leftovers = [r for r in pool if r["stock_code"] not in picked_codes]
        selected.extend(leftovers[: limit - len(selected)])
    selected = selected[:limit]
    picked_codes = {r["stock_code"] for r in selected}

    selected.sort(key=lambda r: r["stock_code"])
    backup = [r for r in pool if r["stock_code"] not in picked_codes]
    return selected, backup


# ============================================================================
# 采集 / 解析
# ============================================================================
async def _seed_accounts() -> int:
    """把内置科目字典写入 account_dict，并刷新进程内归一化器。"""
    from app.enums import StatementType
    from app.models import AccountDict
    from app.services.extractor.accounts import iter_accounts
    from app.services.extractor.normalizer import get_normalizer

    count = 0
    for stype in (StatementType.bs, StatementType.is_, StatementType.cf):
        for code, name, synonyms, core, order in iter_accounts(stype):
            await AccountDict.update_or_create(
                defaults={
                    "item_name_std": name,
                    "statement_type": stype,
                    "synonyms": synonyms,
                    "is_core": core,
                    "sort_order": order,
                },
                item_code=code,
            )
            count += 1
    await get_normalizer().load_from_db()
    return count


async def _sync_and_pick(selected, backup, year: int, limit: int, max_backup: int = 30):
    """同步公告元数据，并按“有 PDF 链接”挑选最终样本。

    只同步 selected + 有限数量的 backup（默认 30 家），避免把全市场
    5000+ 公司都查一遍；随后删除未被选中的 report 行，保证库内只保留
    最终样本，指标口径干净。
    """
    from app.enums import ReportType
    from app.models import Report
    from app.services import pipeline

    candidates = list(selected) + list(backup[:max_backup])
    codes = [r["stock_code"] for r in candidates]
    await pipeline.sync_reports(codes, [ReportType.annual], [year])

    reports = await Report.filter(
        company__stock_code__in=codes,
        report_type=ReportType.annual,
        report_year=year,
    ).prefetch_related("company")
    by_code = {r.company.stock_code: r for r in reports}

    chosen = []
    for row in candidates:
        report = by_code.get(row["stock_code"])
        if report and report.pdf_url:
            chosen.append(report)
        if len(chosen) >= limit:
            break

    chosen_ids = [r.id for r in chosen]
    unchosen_ids = [r.id for r in reports if r.id not in set(chosen_ids)]
    if unchosen_ids:
        # 注意：Tortoise 的 QuerySet.delete() 不支持带 join 的过滤条件，
        # 这里先取 id 再按主键删除。
        await Report.filter(id__in=unchosen_ids).delete()

    return chosen


# ============================================================================
# 指标计算
# ============================================================================
def _pct(numerator, denominator) -> float:
    """百分比，保留 2 位小数；分母为 0 时返回 0。"""
    return round(numerator / denominator * 100, 2) if denominator else 0.0


def _percentile(values: list[float], p: float) -> float | None:
    """线性插值分位数（p 取 0~1）。"""
    if not values:
        return None
    data = sorted(values)
    pos = (len(data) - 1) * p
    low = int(pos)
    high = min(low + 1, len(data) - 1)
    return round(data[low] + (data[high] - data[low]) * (pos - low), 4)


def _close(a: Decimal, b: Decimal, rel: str = "0.01", abs_tol: str = "1") -> bool:
    """与 validator._close 保持一致的容差：1% 相对误差或 1 个绝对单位。"""
    return abs(a - b) <= max(Decimal(abs_tol), abs(b) * Decimal(rel))


def _eval_check(items: dict, codes: tuple[str, ...], stype: str):
    """计算一张报表的勾稽结果。

    :return: (passed, has_derived) 或 None（关键科目缺失，无法判断）
    """
    values = {}
    derived = False
    for code in codes:
        value, is_derived = items.get(code, (None, False))
        if value is None:
            return None
        values[code] = value
        derived = derived or bool(is_derived)

    if stype == "bs":
        passed = _close(values["total_assets"], values["total_liabilities"] + values["total_equity"])
    elif stype == "is":
        # 与 validator 一致：兼容“带符号”和“括号表示费用”两种口径
        np_ = values["net_profit"]
        tp = values["total_profit"]
        tax = values["income_tax_expense"]
        passed = _close(np_, tp - tax) or _close(np_, tp - abs(tax))
    else:  # cf：严格口径，要求经营/投资/筹资三项齐全，汇率影响可选
        fx_value, fx_derived = items.get("exchange_rate_effect", (None, False))
        fx = fx_value if fx_value is not None else Decimal("0")
        derived = derived or bool(fx_derived)
        passed = _close(
            values["net_increase_in_cash"],
            values["net_cash_flow_operating"]
            + values["net_cash_flow_investing"]
            + values["net_cash_flow_financing"]
            + fx,
        )
    return passed, derived


CHECK_SPECS = {
    "bs": ("total_assets", "total_liabilities", "total_equity"),
    "is": ("net_profit", "total_profit", "income_tax_expense"),
    "cf": (
        "net_increase_in_cash",
        "net_cash_flow_operating",
        "net_cash_flow_investing",
        "net_cash_flow_financing",
    ),
}


async def compute_metrics(
    db_name: str,
    year: int,
    limit: int,
    pdf_dir: Path,
    run_meta: dict | None = None,
) -> dict:
    """从数据库重算全部评测指标。"""
    from tortoise import connections

    conn = connections.get("default")

    async def query(sql: str, params: list | None = None) -> list[dict]:
        return await conn.execute_query_dict(sql, params or [])

    def as_int(value) -> int:
        return int(value or 0)

    def as_float(value) -> float:
        return float(value) if value is not None else 0.0

    # ---- 报告级 ----
    overall_rows = await query(
        """
        SELECT COUNT(*) AS reports,
               COUNT(DISTINCT company_id) AS companies,
               SUM(download_status = 'done') AS downloaded,
               SUM(parse_status IN ('done', 'needs_review')) AS parsed,
               SUM(parse_status = 'done') AS passed,
               SUM(needs_review = 1) AS needs_review,
               AVG(parse_confidence) AS avg_confidence
        FROM report
        WHERE report_year = %s
        """,
        [year],
    )
    overall = overall_rows[0] if overall_rows else {}
    reports = as_int(overall.get("reports"))
    companies = as_int(overall.get("companies"))
    downloaded = as_int(overall.get("downloaded"))
    parsed = as_int(overall.get("parsed"))
    passed_reports = as_int(overall.get("passed"))
    needs_review = as_int(overall.get("needs_review"))
    avg_confidence = as_float(overall.get("avg_confidence"))

    # ---- 报表级 ----
    statement_rows = await query(
        """
        SELECT COUNT(*) AS total,
               SUM(s.source = 'rule') AS rule_count,
               SUM(s.source = 'llm') AS llm_count
        FROM statement s
        JOIN report r ON r.id = s.report_id
        WHERE r.report_year = %s
        """,
        [year],
    )
    statement_stats = statement_rows[0] if statement_rows else {}
    statements = as_int(statement_stats.get("total"))
    rule_count = as_int(statement_stats.get("rule_count"))
    llm_count = as_int(statement_stats.get("llm_count"))

    by_type_rows = await query(
        """
        SELECT s.statement_type, s.scope, s.source, COUNT(*) AS n
        FROM statement s
        JOIN report r ON r.id = s.report_id
        WHERE r.report_year = %s
        GROUP BY s.statement_type, s.scope, s.source
        """,
        [year],
    )
    by_type = defaultdict(lambda: {"total": 0, "rule": 0, "llm": 0})
    for row in by_type_rows:
        key = f"{row['statement_type']}/{row['scope']}"
        by_type[key]["total"] += as_int(row["n"])
        if row["source"] == "rule":
            by_type[key]["rule"] += as_int(row["n"])
        elif row["source"] == "llm":
            by_type[key]["llm"] += as_int(row["n"])

    # ---- 科目级 ----
    item_rows = await query(
        """
        SELECT COUNT(*) AS total,
               COUNT(DISTINCT i.item_code) AS distinct_codes,
               SUM(i.is_derived = 1) AS derived
        FROM statement_item i
        JOIN statement s ON s.id = i.statement_id
        JOIN report r ON r.id = s.report_id
        WHERE r.report_year = %s
        """,
        [year],
    )
    item_stats = item_rows[0] if item_rows else {}
    items = as_int(item_stats.get("total"))
    distinct_codes = as_int(item_stats.get("distinct_codes"))
    derived_items = as_int(item_stats.get("derived"))

    # ---- 置信度分布 ----
    confidence_rows = await query(
        "SELECT parse_confidence FROM report WHERE report_year = %s AND parse_confidence IS NOT NULL",
        [year],
    )
    confidences = [float(r["parse_confidence"]) for r in confidence_rows]
    confidence = {
        "avg": round(avg_confidence, 4),
        "median": _percentile(confidences, 0.5),
        "p25": _percentile(confidences, 0.25),
        "p75": _percentile(confidences, 0.75),
        "min": round(min(confidences), 4) if confidences else None,
        "max": round(max(confidences), 4) if confidences else None,
        "gte_0_8": sum(1 for c in confidences if c >= 0.8),
        "count": len(confidences),
    }

    # ---- 核心科目覆盖 + 勾稽校验 ----
    core_rows = await query("SELECT statement_type, item_code FROM account_dict WHERE is_core = 1")
    core_by_type = defaultdict(set)
    for row in core_rows:
        core_by_type[row["statement_type"]].add(row["item_code"])

    detail_rows = await query(
        """
        SELECT s.id AS statement_id, s.statement_type,
               i.item_code, i.value_current, i.is_derived
        FROM statement s
        JOIN report r ON r.id = s.report_id
        JOIN statement_item i ON i.statement_id = s.id
        WHERE r.report_year = %s
        """,
        [year],
    )
    statement_map: dict[int, dict] = {}
    for row in detail_rows:
        sid = int(row["statement_id"])
        entry = statement_map.setdefault(
            sid, {"type": row["statement_type"], "items": {}}
        )
        entry["items"][row["item_code"]] = (row["value_current"], bool(row["is_derived"]))

    coverage_values = []
    for entry in statement_map.values():
        core = core_by_type.get(entry["type"], set())
        if core:
            coverage_values.append(len(core & set(entry["items"])) / len(core))

    checks = {
        key: {
            "checked": 0,
            "passed": 0,
            "failed": 0,
            "incomplete": 0,
            "raw_checked": 0,
            "raw_passed": 0,
        }
        for key in CHECK_SPECS
    }
    for entry in statement_map.values():
        stype = entry["type"]
        if stype not in CHECK_SPECS:
            continue
        result = _eval_check(entry["items"], CHECK_SPECS[stype], stype)
        stat = checks[stype]
        if result is None:
            stat["incomplete"] += 1
            continue
        passed, has_derived = result
        stat["checked"] += 1
        if passed:
            stat["passed"] += 1
        else:
            stat["failed"] += 1
        if not has_derived:
            stat["raw_checked"] += 1
            if passed:
                stat["raw_passed"] += 1

    for stat in checks.values():
        stat["pass_rate"] = _pct(stat["passed"], stat["checked"])
        stat["raw_pass_rate"] = _pct(stat["raw_passed"], stat["raw_checked"])

    # ---- 样本分布 ----
    market_rows = await query(
        """
        SELECT c.market, COUNT(DISTINCT r.company_id) AS n
        FROM report r
        JOIN company c ON c.id = r.company_id
        WHERE r.report_year = %s
        GROUP BY c.market
        """,
        [year],
    )
    by_market = {(row["market"] or "未知"): as_int(row["n"]) for row in market_rows}

    financial_like = " OR ".join(["c.sec_name LIKE %s"] * len(FINANCIAL_KEYWORDS))
    financial_rows = await query(
        f"""
        SELECT COUNT(DISTINCT r.company_id) AS n
        FROM report r
        JOIN company c ON c.id = r.company_id
        WHERE r.report_year = %s AND ({financial_like})
        """,
        [year, *[f"%{k}%" for k in FINANCIAL_KEYWORDS]],
    )
    financial_companies = as_int(financial_rows[0]["n"]) if financial_rows else 0

    # ---- 存储与缓存 ----
    size_rows = await query(
        """
        SELECT COALESCE(SUM(data_length + index_length), 0) AS bytes
        FROM information_schema.TABLES
        WHERE table_schema = %s
        """,
        [db_name],
    )
    db_bytes = as_int(size_rows[0]["bytes"]) if size_rows else 0
    pdf_rows = await query(
        "SELECT COALESCE(SUM(file_size), 0) AS bytes FROM report WHERE report_year = %s",
        [year],
    )
    pdf_db_bytes = as_int(pdf_rows[0]["bytes"]) if pdf_rows else 0
    pdf_disk_bytes = (
        sum(p.stat().st_size for p in Path(pdf_dir).rglob("*.pdf"))
        if Path(pdf_dir).exists()
        else 0
    )
    cache_rows = await query("SELECT COUNT(*) AS n FROM llm_cache")
    llm_cache_rows = as_int(cache_rows[0]["n"]) if cache_rows else 0

    timings = (run_meta or {}).get("timings", {})
    quality = {
        "rule_statements": rule_count,
        "llm_statements": llm_count,
        "rule_rate": _pct(rule_count, statements),
        "llm_rate": _pct(llm_count, statements),
        "final_pass_rate": _pct(passed_reports, parsed),
        "needs_review_rate": _pct(needs_review, parsed),
        "core_coverage_avg": round(
            sum(coverage_values) / len(coverage_values) * 100, 2
        )
        if coverage_values
        else 0.0,
        "core_coverage_min": round(min(coverage_values) * 100, 2)
        if coverage_values
        else 0.0,
        "core_coverage_gte_80": _pct(
            sum(1 for v in coverage_values if v >= 0.8), len(coverage_values)
        ),
        "confidence": confidence,
        "checks": checks,
    }

    return {
        "meta": {
            "db_name": db_name,
            "year": year,
            "limit": limit,
            "llm_enabled": not (run_meta or {}).get("no_llm", False),
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "pdf_dir": str(pdf_dir),
            "timings": timings,
            "timings_note": (run_meta or {}).get("timings_note"),
        },
        "sample": {
            "planned_limit": limit,
            "companies": companies,
            "by_market": by_market,
            "financial_companies": financial_companies,
        },
        "scale": {
            "companies": companies,
            "reports": reports,
            "downloaded": downloaded,
            "parsed": parsed,
            "statements": statements,
            "items": items,
            "distinct_item_codes": distinct_codes,
            "derived_items": derived_items,
            "avg_items_per_statement": round(items / statements, 2) if statements else 0.0,
            "pdf_bytes_db": pdf_db_bytes,
            "pdf_bytes_disk": pdf_disk_bytes,
            "db_bytes": db_bytes,
            "llm_cache_rows": llm_cache_rows,
        },
        "reports": {
            "total": reports,
            "downloaded": downloaded,
            "parsed": parsed,
            "passed": passed_reports,
            "needs_review": needs_review,
            "failed": max(0, downloaded - parsed),
            "download_rate": _pct(downloaded, reports),
            "parse_rate": _pct(parsed, downloaded),
        },
        "statements": {
            "total": statements,
            "by_type": dict(by_type),
        },
        "quality": quality,
    }


# ============================================================================
# 输出
# ============================================================================
def _fmt_bytes(num: int) -> str:
    """人类可读的字节数。"""
    value = float(num)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


def render_markdown(metrics: dict) -> str:
    """把指标渲染成便于粘贴到 README/简历的 Markdown。"""
    meta = metrics["meta"]
    sample = metrics["sample"]
    scale = metrics["scale"]
    reports = metrics["reports"]
    quality = metrics["quality"]
    checks = quality["checks"]
    confidence = quality["confidence"]
    timings = meta.get("timings", {})

    markets = " / ".join(
        f"{name} {count}" for name, count in sample["by_market"].items()
    )
    lines = [
        f"# finreport 评测结果（{meta['year']} 年报）",
        "",
        f"- 数据库：`{meta['db_name']}`",
        f"- 样本：{sample['companies']} 家（{markets}，含 {sample['financial_companies']} 家金融）",
        f"- 规模：{reports['total']} 份年报 / {scale['statements']} 张报表 / "
        f"{scale['items']:,} 条科目（去重 {scale['distinct_item_codes']} 个科目）",
        f"- PDF：{_fmt_bytes(scale['pdf_bytes_disk'])}（磁盘），数据库 {_fmt_bytes(scale['db_bytes'])}",
        "",
        "## 质量指标",
        "",
        "| 指标 | 数值 |",
        "| --- | --- |",
        f"| 下载成功率 | {reports['download_rate']:.2f}%（{reports['downloaded']}/{reports['total']}） |",
        f"| 解析完成率 | {reports['parse_rate']:.2f}%（{reports['parsed']}/{reports['downloaded']}） |",
        f"| 最终通过率 | {quality['final_pass_rate']:.2f}%（{reports['passed']}/{reports['parsed']}） |",
        f"| 待人工复核率 | {quality['needs_review_rate']:.2f}%（{reports['needs_review']} 份） |",
        f"| 规则独立完成率 | {quality['rule_rate']:.2f}%（{quality['rule_statements']}/{scale['statements']} 张报表） |",
        f"| LLM 兜底参与率 | {quality['llm_rate']:.2f}%（{quality['llm_statements']}/{scale['statements']} 张报表） |",
        f"| 平均置信度 / 中位数 | {confidence['avg']:.4f} / {confidence['median']} |",
        f"| 核心科目覆盖率（均值 / 最低） | {quality['core_coverage_avg']:.2f}% / {quality['core_coverage_min']:.2f}% |",
        f"| 核心科目覆盖率 ≥80% 的报表 | {quality['core_coverage_gte_80']:.2f}% |",
        f"| 推算科目占比 | {_pct(scale['derived_items'], scale['items']):.2f}% |",
        "",
        "## 勾稽校验",
        "",
        "| 报表 | 可校验 | 通过 | 通过率 | 原始口径通过率（排除推算值） |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for key, label in (("bs", "资产负债表"), ("is", "利润表"), ("cf", "现金流量表")):
        stat = checks[key]
        lines.append(
            f"| {label} | {stat['checked']} | {stat['passed']} | "
            f"{stat['pass_rate']:.2f}% | {stat['raw_pass_rate']:.2f}% |"
        )

    lines += [
        "",
        "## 效率",
        "",
        "| 阶段 | 耗时 |",
        "| --- | ---: |",
    ]
    labels = {
        "bootstrap_s": "股票列表",
        "sync_s": "公告同步",
        "download_s": "PDF 下载",
        "parse_s": "解析入库",
        "metrics_s": "指标计算",
    }
    for key, label in labels.items():
        if key in timings:
            lines.append(f"| {label} | {timings[key]:.1f}s |")
    if "total_s" in timings:
        lines.append(f"| **合计** | **{timings['total_s']:.1f}s** |")
    if "parse_per_report_s" in timings:
        lines.append(f"\n单份年报平均解析耗时：**{timings['parse_per_report_s']:.2f}s**")
    if meta.get("timings_note"):
        lines.append(f"\n{meta['timings_note']}")

    lines += [
        "",
        "## 口径说明",
        "",
        "- 最终通过率 = `parse_status='done'` / 已解析报告；`needs_review` 单独统计。",
        "- 规则独立完成率 = `statement.source='rule'`，即该报表未触发 LLM。",
        "- 勾稽通过率从 `statement_item` 重算，容差与 `validator._close` 一致（1% 相对或 1 绝对）。",
        "- 原始口径排除 `is_derived=1` 的推算科目；现金流量表要求经营/投资/筹资三项齐全。",
        "- 本报告只反映内部校验与覆盖情况。",
        "",
    ]
    return "\n".join(lines)


async def write_companies(year: int, out_dir: Path) -> Path:
    """把评测样本名单写入文本文件，便于复现。"""
    from app.models import Report

    reports = await Report.filter(report_year=year).prefetch_related("company")
    lines = ["stock_code\tsec_name\tmarket\tfinancial"]
    for report in sorted(reports, key=lambda r: r.company.stock_code):
        name = report.company.sec_name or ""
        financial = any(k in name for k in FINANCIAL_KEYWORDS)
        lines.append(
            f"{report.company.stock_code}\t{name}\t{report.company.market}\t"
            f"{'yes' if financial else 'no'}"
        )
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"companies_{year}.txt"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _write_outputs(metrics: dict, out_dir: Path, limit: int) -> tuple[Path, Path]:
    """写出 Markdown 与 JSON，返回两个路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    year = metrics["meta"]["year"]
    md_path = out_dir / f"report_{year}_{limit}.md"
    json_path = out_dir / f"report_{year}_{limit}.json"
    md_path.write_text(render_markdown(metrics), encoding="utf-8")
    json_path.write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return md_path, json_path


# ============================================================================
# 子命令
# ============================================================================
async def run_benchmark(args) -> None:
    """执行完整评测流程。"""
    url, db_name = _resolve_db_url(args)
    pdf_dir = (
        Path(args.pdf_dir).resolve()
        if args.pdf_dir
        else (ROOT / "data" / "pdfs_bench").resolve()
    )
    _configure_env(url, pdf_dir, args.no_llm)

    # 必须在设置环境变量之后导入 app 模块
    from app.config import settings
    from app.db import close_db, create_database, drop_database, init_db
    from app.models import Report
    from app.services import pipeline

    if not args.no_llm and not settings.llm_api_key:
        raise SystemExit(
            "LLM 已启用但未找到 API Key。请设置环境变量 deepseek，或使用 --no-llm。"
        )

    print(f"[bench] database = {url}")
    print(f"[bench] pdf_dir  = {pdf_dir}")
    print(f"[bench] llm      = {'off' if args.no_llm else settings.llm_model}")

    started = time.perf_counter()
    timings: dict[str, float] = {}
    metrics: dict | None = None
    chosen: list = []

    try:
        t0 = time.perf_counter()
        if args.recreate:
            await drop_database(url)
        await create_database(url)
        await init_db(generate_schemas=True)
        seeded = await _seed_accounts()
        timings["setup_s"] = round(time.perf_counter() - t0, 2)
        print(f"[setup] 建库/建表/字典完成，科目 {seeded} 条")

        t0 = time.perf_counter()
        print("[bootstrap] 拉取巨潮全量股票列表 ...")
        stock_count = await pipeline.sync_stock_list()
        timings["bootstrap_s"] = round(time.perf_counter() - t0, 2)
        print(f"[bootstrap] 入库公司 {stock_count} 家，耗时 {timings['bootstrap_s']}s")

        t0 = time.perf_counter()
        selected, backup = await _select_sample(args.limit, args.financial, args.seed)
        timings["sample_s"] = round(time.perf_counter() - t0, 2)
        dist = defaultdict(int)
        for row in selected:
            dist[row["market"]] += 1
        print(
            f"[sample] 入选 {len(selected)} 家（备选 {len(backup)} 家）："
            + " / ".join(f"{m} {dist[m]}" for m in MARKET_ORDER if dist[m])
        )

        t0 = time.perf_counter()
        print("[sync] 查询公告元数据 ...")
        chosen = await _sync_and_pick(
            selected, backup, args.year, args.limit, args.max_backup
        )
        timings["sync_s"] = round(time.perf_counter() - t0, 2)
        print(f"[sync] 有 PDF 链接的报告 {len(chosen)} 份，耗时 {timings['sync_s']}s")

        t0 = time.perf_counter()
        print("[download] 下载 PDF ...")
        stats = await pipeline.download_reports([r.id for r in chosen])
        # 失败的重试一次（下载逻辑本身已带 3 次网络重试）
        failed = await Report.filter(
            id__in=[r.id for r in chosen], download_status="failed"
        )
        if failed:
            print(f"[download] 首次失败 {len(failed)} 份，重试一次 ...")
            retry_stats = await pipeline.download_reports([r.id for r in failed])
            stats["success"] += retry_stats["success"]
            stats["failed"] = retry_stats["failed"]
        timings["download_s"] = round(time.perf_counter() - t0, 2)
        print(f"[download] {stats}，耗时 {timings['download_s']}s")

        # 只解析本地文件确实存在的报告
        chosen = await Report.filter(id__in=[r.id for r in chosen]).prefetch_related(
            "company"
        )
        downloaded = [
            r for r in chosen if r.local_path and Path(r.local_path).exists()
        ]
        print(f"[parse] 待解析 {len(downloaded)} 份 ...")

        t0 = time.perf_counter()
        parse_times = []
        for idx, report in enumerate(downloaded, 1):
            t_report = time.perf_counter()
            try:
                result = await pipeline.parse_report(report, force_llm=args.force_llm)
                status = result.get("status", "?")
                confidence = result.get("confidence", "-")
            except Exception as exc:  # noqa: BLE001 - 单份失败不影响整体
                status = f"error: {exc}"
                confidence = "-"
            elapsed = time.perf_counter() - t_report
            parse_times.append(elapsed)
            print(
                f"[parse {idx}/{len(downloaded)}] report={report.id} "
                f"status={status} confidence={confidence} {elapsed:.1f}s"
            )
        timings["parse_s"] = round(time.perf_counter() - t0, 2)
        if parse_times:
            timings["parse_per_report_s"] = round(
                sum(parse_times) / len(parse_times), 2
            )

        t0 = time.perf_counter()
        metrics = await compute_metrics(
            db_name=db_name,
            year=args.year,
            limit=args.limit,
            pdf_dir=pdf_dir,
            run_meta={"timings": timings, "no_llm": args.no_llm},
        )
        timings["metrics_s"] = round(time.perf_counter() - t0, 2)
        metrics["meta"]["timings"] = timings
        metrics["meta"]["seed"] = args.seed
        metrics["meta"]["financial_target"] = args.financial

        timings["total_s"] = round(time.perf_counter() - started, 2)
        metrics["meta"]["timings"] = dict(timings)

        out_dir = Path(args.out_dir)
        if not out_dir.is_absolute():
            out_dir = ROOT / out_dir
        md_path, json_path = _write_outputs(metrics, out_dir, args.limit)

        # 保存最终样本名单，便于复现
        sample_path = await write_companies(args.year, out_dir)

        print("")
        print(f"[done] 报告: {md_path}")
        print(f"[done] JSON: {json_path}")
        print(f"[done] 名单: {sample_path}")
        print(f"[done] 总耗时: {timings['total_s']}s")
        print("")
        print(render_markdown(metrics))
    finally:
        await close_db()


async def metrics_only(args) -> None:
    """只从已有评测库重算指标。"""
    url, db_name = _resolve_db_url(args)
    pdf_dir = (
        Path(args.pdf_dir).resolve()
        if args.pdf_dir
        else (ROOT / "data" / "pdfs_bench").resolve()
    )
    _configure_env(url, pdf_dir, no_llm=False)

    from app.db import close_db, init_db

    run_meta = {}
    if args.run_json:
        raw = json.loads(Path(args.run_json).read_text(encoding="utf-8"))
        # 既支持 run 生成的完整指标 JSON，也支持 {"timings": ...} 形式
        run_meta = raw.get("meta", raw)

    out_dir = Path(args.out_dir)
    if not out_dir.is_absolute():
        out_dir = ROOT / out_dir

    await init_db()
    try:
        metrics = await compute_metrics(
            db_name=db_name,
            year=args.year,
            limit=args.limit,
            pdf_dir=pdf_dir,
            run_meta=run_meta,
        )
        sample_path = await write_companies(args.year, out_dir)
    finally:
        await close_db()

    md_path, json_path = _write_outputs(metrics, out_dir, args.limit)
    print(f"[done] 报告: {md_path}")
    print(f"[done] JSON: {json_path}")
    print(f"[done] 名单: {sample_path}")
    print("")
    print(render_markdown(metrics))


def build_parser() -> argparse.ArgumentParser:
    """命令行参数。"""
    parser = argparse.ArgumentParser(description="finreport 可复现评测")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--db-name", default=DEFAULT_DB_NAME, help="评测库名")
        p.add_argument("--db-url", default=None, help="完整连接串（优先级最高）")
        p.add_argument("--pdf-dir", default=None, help="PDF 目录，默认 data/pdfs_bench")
        p.add_argument("--year", type=int, default=2023, help="报告年度")
        p.add_argument("--limit", type=int, default=50, help="样本公司数上限")
        p.add_argument("--out-dir", default="benchmarks", help="指标输出目录")

    run = sub.add_parser("run", help="执行完整评测流程")
    add_common(run)
    run.add_argument("--financial", type=int, default=5, help="金融企业数量")
    run.add_argument("--seed", type=int, default=42, help="随机种子")
    run.add_argument("--max-backup", type=int, default=30, help="备选池同步数量上限")
    run.add_argument("--recreate", action="store_true", help="删除并重建评测库")
    run.add_argument("--no-llm", action="store_true", help="禁用 LLM 兜底")
    run.add_argument("--force-llm", action="store_true", help="跳过规则直接调用 LLM")

    metrics = sub.add_parser("metrics", help="只重算指标")
    add_common(metrics)
    metrics.add_argument("--run-json", default=None, help="可选：读取 run 生成的 JSON 以复用耗时")

    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "run":
        asyncio.run(run_benchmark(args))
    elif args.command == "metrics":
        asyncio.run(metrics_only(args))


if __name__ == "__main__":
    main()
