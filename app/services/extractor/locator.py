"""报表页面定位器。

任务：在一份几百页的年报 PDF 中，找出“合并/母公司 × 资产/利润/现金流”
六张报表各自所在的页码区间。

难点与对策：
    - 目录页会列出所有报表名 -> 含“目录”的页直接跳过
    - 审计报告/附注正文也会提到报表名 -> 要求标题后紧跟年份或“（续）”
    - 报表跨页（“资产负债表（续）”）-> 向后纳入连续页
    - 母公司口径命名多样（母公司/公司/银行/本行）-> 用“报表名但不以合并开头”匹配

页码约定：对外返回 1-based 闭区间；内部列表索引 0-based。
"""

import re

import pdfplumber

from app.enums import StatementType
from app.services.extractor.accounts import STATEMENT_WORDS, STOP_KEYWORDS

_WS_RE = re.compile(r"\s+")
# 金额特征：至少 4 位（含千分位），用于判断该页是否“装满了表格数字”
_AMOUNT_RE = re.compile(r"\d[\d,]{3,}")

_SCOPES = ("consolidated", "parent")


def _norm(text: str) -> str:
    """去掉所有空白，便于标题/关键词的连续匹配。"""
    return _WS_RE.sub("", text or "")


class StatementLocator:
    """报表定位器。构造时只记录路径，首次访问时惰性加载全文文本。"""

    def __init__(self, pdf_path: str):
        self.pdf_path = pdf_path
        self._pages_text: list[str] | None = None  # 去空白后的页文本
        self._pages_raw: list[str] | None = None  # 原始页文本（含换行）

    def _ensure_loaded(self) -> None:
        """惰性打开 PDF 并抽取每页文本（整份只做一次）。"""
        if self._pages_text is not None:
            return
        texts: list[str] = []
        raws: list[str] = []
        with pdfplumber.open(self.pdf_path) as pdf:
            for page in pdf.pages:
                raw = page.extract_text() or ""
                raws.append(raw)
                texts.append(_norm(raw))
        self._pages_raw = raws
        self._pages_text = texts

    @property
    def pages_text(self) -> list[str]:
        """去空白后的各页文本。"""
        self._ensure_loaded()
        assert self._pages_text is not None
        return self._pages_text

    @property
    def pages_raw(self) -> list[str]:
        """原始各页文本（保留换行，供按行解析）。"""
        self._ensure_loaded()
        assert self._pages_raw is not None
        return self._pages_raw

    @staticmethod
    def _has_amounts(text: str, min_count: int = 3) -> bool:
        """页面是否包含至少 min_count 个金额特征数字。"""
        return len(_AMOUNT_RE.findall(text)) >= min_count

    @staticmethod
    def _matches(text: str, stype: StatementType, scope: str) -> bool:
        """判断文本是否“属于”某张报表（用于跨页续页判断）。"""
        word = STATEMENT_WORDS[stype]
        merged = f"合并{word}"
        if scope == "consolidated":
            return merged in text
        # 母公司口径：含报表名，但不含“合并+报表名”
        return word in text and merged not in text

    @staticmethod
    def _title_pattern(stype: StatementType, scope: str) -> re.Pattern:
        """构建标题匹配正则。

        合并表：``合并资产负债表``；母公司表：``(?<!合并)资产负债表``。
        标题后需在极短距离内出现“20xx年 / （续）/ (续)”，以排除正文引用：
        - 真实标题形如 ``合并资产负债表2023年12月31日``
        - 正文引用形如 ``资产负债表项目分析2023年``（中间隔了 4 个字，不匹配）
        """
        word = re.escape(STATEMENT_WORDS[stype])
        prefix = "合并" if scope == "consolidated" else "(?<!合并)"
        return re.compile(prefix + word + r".{0,3}?(?:20\d{2}年|（续）|\(续\))")

    def _find_starts(self) -> dict[tuple[str, str], int]:
        """为每个 (报表类型, 口径) 找到起始页索引（0-based）。

        取“最早满足金额阈值”的页：金额 >=8 直接命中；
        否则退而记录金额 >=5 的页作为兜底。跳过目录页。
        """
        pages = self.pages_text
        starts: dict[tuple[str, str], int] = {}
        for stype in StatementType:
            for scope in _SCOPES:
                pattern = self._title_pattern(stype, scope)
                found: int | None = None
                fallback: int | None = None
                for idx, text in enumerate(pages):
                    if "目录" in text:  # 排除目录页
                        continue
                    if not pattern.search(text):
                        continue
                    score = len(_AMOUNT_RE.findall(text))
                    if score >= 8:
                        found = idx
                        break
                    if score >= 5 and fallback is None:
                        fallback = idx
                chosen = found if found is not None else fallback
                if chosen is not None:
                    starts[(stype.value, scope)] = chosen
        return starts

    def _page_keys(self, text: str) -> set[tuple[str, str]]:
        """判断某页像哪几张报表的标题页（用于识别“下一张表开始了”）。"""
        keys: set[tuple[str, str]] = set()
        for stype in StatementType:
            for scope in _SCOPES:
                if self._title_pattern(stype, scope).search(text):
                    keys.add((stype.value, scope))
        return keys

    def locate(self) -> dict[tuple[str, str], tuple[int, int]]:
        """返回 {(statement_type, scope): (start_page, end_page)}，页号 1-based。"""
        pages = self.pages_text
        n = len(pages)
        starts = self._find_starts()
        ranges: dict[tuple[str, str], tuple[int, int]] = {}

        # 按起始页排序处理，便于判断页区间是否连续
        for key, start in sorted(starts.items(), key=lambda kv: kv[1]):
            stype = StatementType(key[0])
            scope = key[1]
            end = start
            limit = min(n - 1, start + 3)  # 一张表最多向后取 3 页
            i = start + 1
            while i <= limit:
                text = pages[i]
                # 命中附注关键词 -> 报表正文结束
                if any(stop in text for stop in STOP_KEYWORDS):
                    break
                # 出现了“别的报表”的标题 -> 当前表结束
                if self._page_keys(text) - {key}:
                    break
                # 页面有金额或仍属于当前表 -> 纳入区间，继续向后
                if _AMOUNT_RE.findall(text) or self._matches(text, stype, scope):
                    end = i
                    i += 1
                    continue
                break
            ranges[key] = (start + 1, end + 1)  # 转 1-based

        return ranges

    def page_text(self, start: int, end: int) -> str:
        """按 1-based 闭区间拼接原始页文本（用于按行解析/单位识别）。"""
        if end < start:
            end = start
        return "\n".join(self.pages_raw[start - 1 : end])
