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
# 标题前的编号：1、 / 1. / （一） / (1) 等
_NUMBER_PREFIX_RE = re.compile(r"^[（(]?[0-9一二三四五六七八九十]+[）)、.．]\s*")


def _norm(text: str) -> str:
    """去掉所有空白，便于标题/关键词的连续匹配。"""
    return _WS_RE.sub("", text or "")


def _strip_numbering(line: str) -> str:
    """去掉标题前的编号，如 ``1、`` / ``（一）``。"""
    return _NUMBER_PREFIX_RE.sub("", line, count=1)


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
    def _line_keys(line: str) -> tuple[set[tuple[str, str]], bool]:
        """识别单行是否为“报表标题”，返回 (keys, combined)。"""
        line = _strip_numbering(_norm(line))
        keys: set[tuple[str, str]] = set()
        combined = False
        if not line:
            return keys, combined
        for stype in StatementType:
            word = STATEMENT_WORDS[stype]
            # 金融企业常见：合并表与母公司表并排在同一页
            if line.startswith(f"合并{word}") and line.count(word) >= 2:
                keys.add((stype.value, "consolidated"))
                keys.add((stype.value, "parent"))
                combined = True
                continue
            # 银行另一种并排写法：合并及母公司资产负债表 / 合并及母公司利润表
            if re.fullmatch(
                rf"合并及(?:母公司|公司|本行){word}(?:（续）|\(续\))?(?:20\d{{2}}年.*)?",
                line,
            ):
                keys.add((stype.value, "consolidated"))
                keys.add((stype.value, "parent"))
                combined = True
                continue
            # 合并表：合并xx表[（续）][2023年...]
            if re.fullmatch(rf"合并{word}(?:（续）|\(续\))?(?:20\d{{2}}年.*)?", line):
                keys.add((stype.value, "consolidated"))
                continue
            # 母公司表：母公司/公司/本行/银行 + xx表，或直接 xx表
            if re.fullmatch(
                rf"(?:母公司|公司|本行|银行)?{word}(?:（续）|\(续\))?(?:20\d{{2}}年.*)?",
                line,
            ):
                keys.add((stype.value, "parent"))
        return keys, combined

    @classmethod
    def _page_title_keys(cls, raw: str) -> tuple[set[tuple[str, str]], bool]:
        """识别一页中的“报表标题行”，返回 (keys, combined)。

        只在短标题行上匹配，避免正文/附注里的引用造成误判。标题允许：
            - 前置编号：``1、合并资产负债表`` / ``（一）合并利润表``
            - 续页：``合并资产负债表（续）``
            - 同行日期：``合并资产负债表2023年12月31日``
            - 银行并排格式：``合并资产负债表和资产负债表``（同时标记两个口径）

        :return: ({(报表类型, 口径), ...}, 是否为合并/母公司并排页)
        """
        keys: set[tuple[str, str]] = set()
        combined = False
        for line in (raw or "").splitlines():
            line_keys, line_combined = cls._line_keys(line)
            keys |= line_keys
            combined = combined or line_combined
        return keys, combined

    def _find_starts(self) -> dict[tuple[str, str], int]:
        """为每个 (报表类型, 口径) 找到起始页索引（0-based）。

        标题页可能没有金额（如“财务报表”封面页），因此按以下优先级选择：
        金额 >=8 的页 > 金额 >=3 的页 > 第一个标题页。
        目录页（含“目录”或大量点线）直接跳过。
        """
        candidates: dict[tuple[str, str], list[tuple[int, int]]] = {}
        for idx, raw in enumerate(self.pages_raw):
            text = self.pages_text[idx]
            if "目录" in text or "contents" in text.lower():
                continue
            if text.count("....") >= 2:  # 目录页的点线
                continue
            keys, _ = self._page_title_keys(raw)
            if not keys:
                continue
            score = len(_AMOUNT_RE.findall(text))
            for key in keys:
                candidates.setdefault(key, []).append((idx, score))

        starts: dict[tuple[str, str], int] = {}
        for key, cands in candidates.items():
            high = [c for c in cands if c[1] >= 8]
            mid = [c for c in cands if c[1] >= 3]
            chosen = (high or mid or cands)[0][0]
            starts[key] = chosen
        return starts

    def _page_keys(self, raw: str) -> tuple[set[tuple[str, str]], bool]:
        """判断某页像哪几张报表的标题页（用于识别“下一张表开始了”）。"""
        return self._page_title_keys(raw)

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
            limit = min(n - 1, start + 6)  # 一张表最多向后取 6 页（含跨页续表）
            i = start + 1
            while i <= limit:
                text = pages[i]
                keys, combined = self._page_keys(self.pages_raw[i])
                # 命中附注/权益变动表关键词 -> 报表正文结束。
                # 但报表页眉常带“财务报表附注”字样，若本页仍是当前报表
                # 的标题/续页（keys 非空），则视为页眉，不打断。
                stop_hit = any(stop in text for stop in STOP_KEYWORDS)
                if stop_hit and not keys:
                    # 权益变动表开始前，本页上半部分可能仍是当前现金流量表
                    # 的尾部（筹资净额、净增加额、期初期末余额），先纳入本页，
                    # 解析时再按“所有者权益变动表”标题行切分。
                    equity_hit = any(
                        k in text for k in ("所有者权益变动表", "股东权益变动表")
                    )
                    if equity_hit and _AMOUNT_RE.findall(text):
                        end = i
                    break
                # 出现了“别的报表”的标题 -> 当前表结束；
                # 但银行“合并xx表和xx表”同页时，同一报表类型的两个口径不算打断
                other = keys - {key}
                if other:
                    same_type = all(k[0] == key[0] for k in other)
                    if combined and same_type:
                        # 银行并排页：两个口径共用同一区间，继续向后
                        pass
                    else:
                        # 边界页可能上半部分是当前表尾部、下半部分是下一张表标题。
                        # 先纳入本页，解析时再按标题行切分。
                        end = i
                        break
                # 页面有金额、仍属于当前表，或本身就是当前表的标题/续页 -> 纳入区间
                if (
                    _AMOUNT_RE.findall(text)
                    or self._matches(text, stype, scope)
                    or key in keys
                ):
                    end = i
                    i += 1
                    continue
                break
            ranges[key] = (start + 1, end + 1)  # 转 1-based

        return ranges

    def _slice_page(self, raw: str, key: tuple[str, str]) -> str:
        """按标题行切分一页，只保留属于当前报表的部分。

        典型场景：一页上半部分是“合并现金流量表”的尾部，下半部分是
        “母公司现金流量表”的标题和数据。解析合并表时保留上半部分，
        解析母公司表时保留下半部分；银行“合并xx表和xx表”并排页不切分。
        """
        lines = raw.splitlines()
        current: int | None = None
        other: int | None = None
        combined = False
        for idx, line in enumerate(lines):
            keys, line_combined = self._line_keys(line)
            if not keys:
                # 权益变动表是现金流量表之后的下一张表，作为切分边界
                if any(k in line for k in ("所有者权益变动表", "股东权益变动表")):
                    if other is None or idx < other:
                        other = idx
                continue
            others = keys - {key}
            if key in keys and current is None:
                current = idx
            if others:
                if line_combined and all(k[0] == key[0] for k in others):
                    combined = True
                elif other is None:
                    other = idx
        if combined:
            return raw  # 并排格式无法按行切分，保留整页
        start = current if current is not None else 0
        end = (
            other
            if other is not None and (current is None or other > current)
            else len(lines)
        )
        if start >= end:
            return ""
        return "\n".join(lines[start:end])

    def page_text(
        self, start: int, end: int, key: tuple[str, str] | None = None
    ) -> str:
        """按 1-based 闭区间拼接原始页文本（用于按行解析/单位识别）。

        :param key: 可选 (报表类型, 口径)。提供时会按标题行裁掉同页中
            其它报表的内容，避免合并/母公司表共用页面时相互污染。
        """
        if end < start:
            end = start
        last = min(end, len(self.pages_raw))
        chunks: list[str] = []
        for idx in range(start - 1, last):
            raw = self.pages_raw[idx]
            if key is not None:
                raw = self._slice_page(raw, key)
            chunks.append(raw)
        return "\n".join(chunks)
