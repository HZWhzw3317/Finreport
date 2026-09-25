"""科目名归一化。

把 PDF 中五花八门的科目写法（含编号前缀、同义词、括号变体）映射到
标准科目编码 ``item_code``。

归一化流程（normalize_name）：
    1. 去掉行首编号/修饰前缀：``一、`` ``1.`` ``其中：`` ``减：`` ``加：``
    2. 去掉所有空白与标点（含全角），并转小写

这样 ``一、营业收入``、``其中：营业收入``、``营业收入`` 都会归一为 ``营业收入``。
"""

import re

from app.enums import StatementType
from app.services.extractor.accounts import iter_accounts

# 需要剔除的标点/空白字符集（半角+全角）
_PUNCT_RE = re.compile(r"[\s\u3000（）()\[\]【】、，,：:；;\"'“”‘’·.。\-—_]+")
# 行首中文编号前缀，如 “一、”“（二）” 的 “二、”
_PREFIX_CN_RE = re.compile(r"^[一二三四五六七八九十]+\s*[、.．]\s*")
# 行首阿拉伯数字编号前缀，如 “1.”“3、”
_PREFIX_NUM_RE = re.compile(r"^\d+\s*[、.．]\s*")
# 行首修饰词前缀，如 “其中：”“减：”“加：”
_PREFIX_ADD_RE = re.compile(r"^(?:其中|减|加|其中)\s*[:：]\s*")


def normalize_name(name: str) -> str:
    """把原始科目名归一化为可比较的键。"""
    if not name:
        return ""
    s = str(name).strip()
    # 前缀可能叠加（如“减：其中：...”），最多循环 3 次即可覆盖
    for _ in range(3):
        new = _PREFIX_ADD_RE.sub("", s)
        new = _PREFIX_CN_RE.sub("", new)
        new = _PREFIX_NUM_RE.sub("", new)
        if new == s:  # 没有变化则提前结束
            break
        s = new
    return _PUNCT_RE.sub("", s).lower()


class AccountNormalizer:
    """科目名归一化器：维护“归一化名称 -> (编码, 标准名, 顺序)”映射。"""

    def __init__(self):
        self._map: dict[str, tuple[str, str, int]] = {}  # 归一化名 -> (code, std, order)
        self._core: dict[str, set[str]] = {}  # 报表类型 -> 核心科目编码集合
        self._std: dict[str, str] = {}  # code -> 标准名
        self._load_defaults()

    def _load_defaults(self) -> None:
        """加载内置字典（app/services/extractor/accounts.py）。"""
        for stype in (StatementType.bs, StatementType.is_, StatementType.cf):
            core: set[str] = set()
            for code, name, syn, is_core, order in iter_accounts(stype):
                self._std[code] = name
                # 标准名与所有同义词都指向同一编码
                for candidate in [name, *syn]:
                    self._map[normalize_name(candidate)] = (code, name, order)
                if is_core:
                    core.add(code)
            self._core[stype.value] = core

    def register(self, stype: StatementType, code: str, name: str, synonyms=None, order: int = 0) -> None:
        """注册/覆盖一个科目（供从数据库扩展字典时使用）。"""
        self._std[code] = name
        for candidate in [name, *(synonyms or [])]:
            self._map[normalize_name(candidate)] = (code, name, order)

    def lookup(self, stype: StatementType, raw_name: str):
        """按原始科目名查询，返回 (item_code, item_name_std, sort_order) 或 None。"""
        return self._map.get(normalize_name(raw_name))

    def core_codes(self, stype: StatementType) -> set[str]:
        """返回某报表的核心科目编码集合。"""
        return self._core.get(stype.value, set())

    def std_name(self, code: str) -> str:
        """返回编码对应的标准名；未知编码原样返回。"""
        return self._std.get(code, code)

    def is_known(self, code: str) -> bool:
        """判断编码是否在字典中（用于校验大模型返回的 item_code）。"""
        return code in self._std

    async def load_from_db(self) -> None:
        """从 account_dict 表加载/覆盖字典（可选，需先建表并初始化数据库）。"""
        from app.models import AccountDict

        rows = await AccountDict.all()
        for row in rows:
            self.register(
                row.statement_type,
                row.item_code,
                row.item_name_std,
                row.synonyms or [],
                row.sort_order,
            )
            if row.is_core:
                self._core.setdefault(row.statement_type.value, set()).add(row.item_code)


# 进程内单例，避免每次解析都重建映射
_normalizer: AccountNormalizer | None = None


def get_normalizer() -> AccountNormalizer:
    """获取全局归一化器单例。"""
    global _normalizer
    if _normalizer is None:
        _normalizer = AccountNormalizer()
    return _normalizer
