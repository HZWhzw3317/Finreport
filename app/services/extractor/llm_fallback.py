"""大模型视觉兜底抽取。

当规则抽取未通过校验时，调用视觉大模型（默认 deepseek-flash）直接“看”
报表页面图片，输出结构化 JSON。

流程：
    1. 用 PyMuPDF 把报表页渲染为 PNG（base64 内联）
    2. 构造提示词：注入标准科目对照表 + 严格 JSON schema
    3. 调用 OpenAI 兼容接口（支持图片输入）
    4. 解析返回 JSON，按 item_code/科目名归一化
    5. 交给 Validator 重新校验（由 pipeline 完成）

接口地址、密钥、模型名均来自配置（.env 或环境变量）。
"""

import base64
import hashlib
import json
import logging

from openai import AsyncOpenAI
from tenacity import retry, stop_after_attempt, wait_exponential

from app.config import settings
from app.enums import StatementType
from app.services.extractor.accounts import iter_accounts
from app.services.extractor.normalizer import AccountNormalizer
from app.services.extractor.numbers import parse_amount
from app.services.extractor.result import ParsedItem, ParseResult

logger = logging.getLogger(__name__)

# 系统提示：限定角色与输出格式，强调“不得编造”
_SYSTEM_PROMPT = (
    "你是一名严谨的中国 A 股上市公司财务报表结构化抽取专家。"
    "你会收到财报中包含某张财务报表的页面图片，请只依据图片内容抽取数据，"
    "不要编造，无法确定的字段填 null。只输出 JSON。"
)


def _render_pages(pdf_path: str, start: int, end: int, dpi: int) -> list[str]:
    """把 [start, end] 页渲染为 PNG 并返回 base64 字符串列表。

    :param dpi: 渲染分辨率，越高越清晰但 token/成本越高。
    """
    import fitz  # PyMuPDF

    images: list[str] = []
    with fitz.open(pdf_path) as doc:
        last = min(end, len(doc))  # 防止越界
        for idx in range(start - 1, last):
            pix = doc[idx].get_pixmap(dpi=dpi)
            images.append(base64.b64encode(pix.tobytes("png")).decode("ascii"))
    return images


def _build_user_prompt(stype: StatementType, scope: str, unit_hint: str) -> str:
    """构造用户提示：包含口径、单位提示、标准科目对照表与输出 JSON 示例。"""
    scope_cn = "合并" if scope == "consolidated" else "母公司"
    code_lines = []
    for code, name, _syn, _core, _order in iter_accounts(stype):
        code_lines.append(f"- {code}: {name}")
    codes = "\n".join(code_lines)
    return (
        f"请抽取【{scope_cn}】报表（statement_type={stype.value}）的数据。\n"
        f"页面中若包含多张报表，只取该类型报表；金额单位参考页面标注（当前识别为 {unit_hint}）。\n"
        "数值规则：括号表示负数；千分位去掉；空值用 null。value_current 为本期/期末，"
        "value_previous 为上期/期初。\n\n"
        "标准科目对照表（优先使用下列 item_code，若不在表中则 item_code 填 null，"
        "并把原始科目名写入 item_name）：\n"
        f"{codes}\n\n"
        "只输出如下 JSON（不要额外文字）：\n"
        "{\n"
        '  "unit": "元|千元|万元",\n'
        '  "items": [\n'
        '    {"item_code": "cash", "item_name": "货币资金", '
        '"value_current": 123.45, "value_previous": 111.11}\n'
        "  ]\n"
        "}"
    )


async def _call_vision(images: list[str], user_prompt: str) -> str:
    """调用视觉接口，返回模型输出的原始文本。"""
    client = AsyncOpenAI(
        api_key=settings.llm_api_key or "sk-placeholder",
        base_url=settings.llm_base_url,
    )
    # 多模态消息：文本 + 多张图片
    content: list[dict] = [{"type": "text", "text": user_prompt}]
    for img in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{img}"},
            }
        )
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]

    # 网络/限流重试；指数退避
    @retry(
        stop=stop_after_attempt(settings.max_retry),
        wait=wait_exponential(multiplier=1, min=1, max=10),
        reraise=True,
    )
    async def _do():
        try:
            # 优先要求返回 JSON 对象（部分兼容端点不支持，则降级）
            resp = await client.chat.completions.create(
                model=settings.llm_model,
                messages=messages,
                temperature=settings.llm_temperature,
                response_format={"type": "json_object"},
            )
        except Exception:  # noqa: BLE001 - 兼容不支持 response_format 的端点
            resp = await client.chat.completions.create(
                model=settings.llm_model,
                messages=messages,
                temperature=settings.llm_temperature,
            )
        return resp.choices[0].message.content or "{}"

    return await _do()


def _coerce_amount(value) -> float | None:
    """把模型返回值转成 float（数字直接转，字符串用 parse_amount）。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return parse_amount(value)


def _parse_llm_json(text: str) -> dict:
    """解析模型返回的 JSON，容忍 ```json 代码块或前后多余文字。"""
    text = text.strip()
    if text.startswith("```"):  # 去掉 markdown 代码块围栏
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 退一步：截取第一个 { 到最后一个 } 之间
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _cache_key(
    file_hash: str, stype: StatementType, scope: str, start: int, end: int
) -> str:
    """构造缓存键。

    键 = ``sha256(file_hash|报表类型|口径|页区间|模型)``，固定 64 字符，
    既保证唯一又不会超过 ``LlmCache.cache_key`` 的 128 长度上限。
    """
    raw = f"{file_hash}|{stype.value}|{scope}|{start}-{end}|{settings.llm_model}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _load_cache(cache_key: str) -> str | None:
    """读取大模型响应缓存；无命中或数据库不可用时返回 None。"""
    from app.models import LlmCache  # 延迟导入，避免无 Tortoise 上下文时报错

    try:
        row = await LlmCache.get_or_none(cache_key=cache_key)
    except Exception as exc:  # noqa: BLE001 - 缓存不可用不应影响抽取
        logger.debug("读取 LLM 缓存失败: %s", exc)
        return None
    return row.response if row else None


async def _save_cache(cache_key: str, response: str) -> None:
    """写入大模型响应缓存；失败仅记录日志，不影响主流程。"""
    from app.models import LlmCache

    try:
        await LlmCache.update_or_create(
            defaults={"response": response}, cache_key=cache_key
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("写入 LLM 缓存失败: %s", exc)


async def extract_with_llm(
    pdf_path: str,
    stype: StatementType,
    scope: str,
    normalizer: AccountNormalizer,
    page_range: tuple[int, int],
    unit_hint: str = "元",
    file_hash: str | None = None,
) -> ParseResult:
    """视觉兜底抽取入口。

    :param unit_hint: 规则阶段识别的单位，作为提示与兜底单位。
    :param file_hash: PDF 的 SHA256；提供时启用响应缓存，避免重复调用。

    缓存流程：命中缓存则跳过“渲染图片 + 调用模型”两步直接复用；未命中
    则调用后写回。缓存不可用（如未接数据库）时自动降级为直接调用。
    """
    start, end = page_range
    start = max(1, start)
    # 限制送图页数，控制成本
    max_end = start + settings.llm_max_pages_per_statement - 1
    end = min(end, max_end)

    cache_key = _cache_key(file_hash, stype, scope, start, end) if file_hash else None
    raw = await _load_cache(cache_key) if cache_key else None
    if raw is None:
        images = _render_pages(pdf_path, start, end, settings.llm_render_dpi)
        prompt = _build_user_prompt(stype, scope, unit_hint)
        raw = await _call_vision(images, prompt)
        if cache_key:
            await _save_cache(cache_key, raw)
    data = _parse_llm_json(raw)

    result = ParseResult(
        statement_type=stype,
        scope=scope,
        unit=data.get("unit") or unit_hint,
        source="llm",
        page_range=f"{start}-{end}",
    )
    for idx, entry in enumerate(data.get("items", [])):
        name = entry.get("item_name") or ""
        code = entry.get("item_code") or None
        # 模型给的 code 不可信时，用科目名回退到字典匹配
        if not code or not normalizer.is_known(code):
            mapped = normalizer.lookup(stype, name)
            code = mapped[0] if mapped else None
        std = normalizer.std_name(code) if code else None
        result.items.append(
            ParsedItem(
                item_code=code,
                item_name_raw=name,
                item_name_std=std,
                value_current=_coerce_amount(entry.get("value_current")),
                value_previous=_coerce_amount(entry.get("value_previous")),
                sort_order=idx,
            )
        )
    result.raw_rows = len(result.items)
    return result
