"""巨潮资讯网采集客户端。

负责三件事：
    1. 拉取全量股票列表（code -> orgId 映射，查公告必需）
    2. 按代码/类型/时间区间查询财报公告
    3. 下载 PDF 到本地并按 SHA256 计算哈希

接口说明（已实测可用）：
    - 股票列表：GET  http://www.cninfo.com.cn/new/data/szse_stock.json
    - 公告查询：POST http://www.cninfo.com.cn/new/hisAnnouncement/query
    - PDF 直链：http://static.cninfo.com.cn/{adjunctUrl}

注意事项：
    - 请求必须带 Referer / User-Agent 等头部，否则可能被拒
    - orgId 不能靠代码规则拼接（如 002594 的 orgId 为 gshk0001211），必须读列表
"""

import asyncio
import hashlib
import logging
from datetime import datetime
from pathlib import Path

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.enums import ReportType

logger = logging.getLogger(__name__)

CNINFO_BASE = "http://www.cninfo.com.cn"  # 查询接口域名
CNINFO_STATIC = "http://static.cninfo.com.cn"  # 静态资源（PDF）域名

# 统一请求头，模拟浏览器 AJAX 请求
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "http://www.cninfo.com.cn/new/commonUrl?url=disclosure/list/notice",
}

# 报告类型 -> 巨潮公告分类编码
CATEGORY = {
    ReportType.annual: "category_ndbg_szsh",  # 年度报告
    ReportType.semi: "category_bndbg_szsh",  # 半年度报告
    ReportType.q1: "category_yjdbg_szsh",  # 第一季度报告
    ReportType.q3: "category_sjdbg_szsh",  # 第三季度报告
}

# 报告类型 -> 标题关键词（用于在返回结果中筛选正文）
TITLE_KEYWORD = {
    ReportType.annual: "年度报告",
    ReportType.semi: "半年度报告",
    ReportType.q1: "第一季度报告",
    ReportType.q3: "第三季度报告",
}

# 需要排除的公告标题关键词（摘要、英文版、审计意见等非正文）
EXCLUDE_TITLE = ("摘要", "英文", "English", "意见", "取消", "补充", "问询", "回复")


def infer_exchange(code: str) -> str:
    """根据股票代码前缀推断交易所。

    - 60/68/90 -> 上交所(sse)
    - 00/30/20 -> 深交所(szse)
    - 4/8/92   -> 北交所(bse)
    """
    if code.startswith(("60", "68", "90")):
        return "sse"
    if code.startswith(("00", "30", "20")):
        return "szse"
    if code.startswith(("4", "8", "92")):
        return "bse"
    return "other"


def infer_market(code: str) -> str:
    """根据股票代码前缀推断所属板块。"""
    if code.startswith("688"):
        return "科创板"
    if code.startswith("300"):
        return "创业板"
    if code.startswith(("60", "000", "001", "002", "003")):
        return "主板"
    if code.startswith(("4", "8", "92")):
        return "北交所"
    return "其他"


def default_window(report_type: ReportType, report_year: int) -> tuple[str, str]:
    """给出各类型报告的默认公告检索时间窗（披露通常滞后于报告期）。

    - 年报：次年全年
    - 半年报：当年下半年
    - 一季报：4~6 月
    - 三季报：10~12 月
    """
    y = report_year
    if report_type == ReportType.annual:
        return f"{y + 1}-01-01", f"{y + 1}-12-31"
    if report_type == ReportType.semi:
        return f"{y}-07-01", f"{y}-12-31"
    if report_type == ReportType.q1:
        return f"{y}-04-01", f"{y}-06-30"
    return f"{y}-10-01", f"{y}-12-31"


class CninfoClient:
    """巨潮采集客户端（无状态，可反复实例化）。"""

    def __init__(self, pdf_dir: str | None = None):
        # PDF 归档根目录，默认取配置项
        self.pdf_dir = Path(pdf_dir or settings.pdf_dir)
        self.pdf_dir.mkdir(parents=True, exist_ok=True)

    async def fetch_stock_list(self) -> list[dict]:
        """拉取全量股票列表，返回 [{stock_code, sec_name, org_id, exchange, market}]。"""
        url = f"{CNINFO_BASE}/new/data/szse_stock.json"
        async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
        result = []
        for row in data.get("stockList", []):
            code = row.get("code", "")
            result.append(
                {
                    "stock_code": code,
                    "sec_name": row.get("zwjc"),  # 中文简称
                    "org_id": row.get("orgId"),  # 查询公告必需
                    "exchange": infer_exchange(code),
                    "market": infer_market(code),
                }
            )
        return result

    async def query_announcements(
        self,
        code: str,
        org_id: str,
        report_type: ReportType,
        start_date: str,
        end_date: str,
        page_size: int = 30,
    ) -> list[dict]:
        """查询某公司指定类型、指定时间区间的公告。

        返回统一后的结构：公告ID、标题、公告日期、adjunct 相对路径与 PDF 完整直链。
        """
        url = f"{CNINFO_BASE}/new/hisAnnouncement/query"
        # 巨潮要求的表单参数；stock 必须为 "代码,orgId"
        payload = {
            "pageNum": 1,
            "pageSize": page_size,
            "column": "szse",
            "tabName": "fulltext",
            "plate": "",
            "stock": f"{code},{org_id}",
            "searchkey": "",
            "secid": "",
            "category": CATEGORY[report_type],
            "trade": "",
            "seDate": f"{start_date}~{end_date}",  # 时间区间，必填，否则可能返回空
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
        async with httpx.AsyncClient(headers=HEADERS, timeout=30) as client:
            resp = await client.post(url, data=payload)
            resp.raise_for_status()
            data = resp.json()
        rows = data.get("announcements") or []
        results = []
        for row in rows:
            ts = row.get("announcementTime")  # 毫秒时间戳
            ann_date = (
                datetime.fromtimestamp(ts / 1000).date().isoformat() if ts else None
            )
            results.append(
                {
                    "announcement_id": row.get("announcementId"),
                    "title": row.get("announcementTitle"),
                    "ann_date": ann_date,
                    "adjunct_url": row.get("adjunctUrl"),
                    "pdf_url": f"{CNINFO_STATIC}/{row.get('adjunctUrl')}"
                    if row.get("adjunctUrl")
                    else None,
                }
            )
        return results

    def select_announcement(
        self, announcements: list[dict], report_type: ReportType
    ) -> dict | None:
        """从公告列表中挑选“报告正文”。

        规则：标题含对应关键词、不含排除词；
        若存在多个（如更正后），取公告日期最新的一条。
        """
        keyword = TITLE_KEYWORD[report_type]
        candidates = []
        for ann in announcements:
            title = ann.get("title") or ""
            if keyword not in title:
                continue
            if any(bad in title for bad in EXCLUDE_TITLE):
                continue
            if "公告编号" in title:
                continue
            candidates.append(ann)
        if not candidates:
            return None
        candidates.sort(key=lambda a: a.get("ann_date") or "", reverse=True)
        return candidates[0]

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        reraise=True,
    )
    async def _do_download(self, url: str, dest: Path) -> None:
        """流式下载到临时文件再改名，避免中断留下损坏文件。"""
        tmp = dest.with_suffix(dest.suffix + ".part")
        async with httpx.AsyncClient(headers=HEADERS, timeout=120, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with tmp.open("wb") as fh:
                    async for chunk in resp.aiter_bytes(8192):  # 分块写入，节省内存
                        fh.write(chunk)
        tmp.replace(dest)  # 原子替换

    async def download_pdf(self, url: str, code: str, year: int, tag: str) -> dict:
        """下载 PDF 并返回本地路径、文件大小与 SHA256 哈希。

        文件按 ``{pdf_dir}/{code}/{code}_{year}_{tag}.pdf`` 归档。
        下载后会休眠 ``DOWNLOAD_DELAY`` 秒做限速。
        """
        dest_dir = self.pdf_dir / code
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{code}_{year}_{tag}.pdf"
        await self._do_download(url, dest)
        data = dest.read_bytes()
        await asyncio.sleep(settings.download_delay)
        return {
            "local_path": str(dest),
            "file_size": len(data),
            "file_hash": hashlib.sha256(data).hexdigest(),
        }
