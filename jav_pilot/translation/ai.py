"""On-demand AI translation through a user-configured model service.

AI translation never runs automatically: the web UI calls it only when the
user presses an "AI 翻译" button, and its results are shown next to (never
instead of) the free public translation. Any provider that speaks one of five
wire protocols can be used: OpenAI-compatible chat completions, Azure OpenAI,
the Anthropic Messages API, Google Gemini and Ollama. The API key is stored in
the data directory, never returned by the API, and redacted from errors.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import sqlite3
import ssl
import tempfile
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import dataclass, field
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import (
    HTTPHandler,
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from ..net.network_guard import resolve_public_addresses
from ..net.pinned_http import PinnedHTTPHandler, PinnedHTTPSHandler
from ..config.paths import default_database_path, runtime_data_dir

MAX_TEXTS = 60
MAX_TEXT_LENGTH = 1200
BATCH_SIZE = 20
MAX_PARALLEL_BATCHES = 3
MAX_OUTPUT_TOKENS = 8192
FALLBACK_OUTPUT_TOKENS = 4096
REQUEST_TIMEOUT_SECONDS = 75.0
TOTAL_TIMEOUT_SECONDS = 110.0
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_ERROR_BYTES = 64 * 1024
MAX_CACHE_ROWS = 50_000
MAX_INSTRUCTIONS_LENGTH = 500
TARGET_LANGUAGES = {
    "zh-CN": "Simplified Chinese (简体中文)",
    "zh-TW": "Traditional Chinese (繁體中文)",
    "en": "English",
}
PROTOCOLS = frozenset({"openai", "azure_openai", "anthropic", "gemini", "ollama"})
_TEST_TEXT = "今日はとても良い天気ですね。"
_WRITE_LOCK = threading.Lock()
_CONFIG_LOCK = threading.Lock()
# Bounds concurrent upstream calls across every request so a burst of clicks
# cannot fan out into dozens of paid requests at once.
_UPSTREAM_SLOTS = threading.BoundedSemaphore(4)


@dataclass(frozen=True, slots=True)
class AiProvider:
    key: str
    label: str
    protocol: str
    default_base_url: str = ""
    default_api_version: str = ""
    requires_api_key: bool = True
    token_limit_field: str = "max_tokens"

    def public_dict(self) -> dict[str, object]:
        return {
            "id": self.key,
            "label": self.label,
            "protocol": self.protocol,
            "default_base_url": self.default_base_url,
            "default_api_version": self.default_api_version,
            "requires_api_key": self.requires_api_key,
        }


_PROVIDER_LIST = (
    AiProvider("openai_compatible", "自定义 OpenAI 兼容", "openai", requires_api_key=False),
    AiProvider(
        "openai", "OpenAI", "openai", "https://api.openai.com/v1",
        token_limit_field="max_completion_tokens",
    ),
    AiProvider("azure_openai", "Azure OpenAI", "azure_openai", "", "2024-10-21"),
    AiProvider("anthropic", "Anthropic Claude", "anthropic", "https://api.anthropic.com", "2023-06-01"),
    AiProvider("gemini", "Google Gemini", "gemini", "https://generativelanguage.googleapis.com/v1beta"),
    AiProvider("xai", "xAI Grok", "openai", "https://api.x.ai/v1"),
    AiProvider("ollama", "Ollama", "ollama", "http://localhost:11434", requires_api_key=False),
    AiProvider("deepseek", "DeepSeek", "openai", "https://api.deepseek.com/v1"),
    AiProvider("moonshot", "Moonshot / Kimi", "openai", "https://api.moonshot.cn/v1"),
    AiProvider("qwen", "阿里云百炼 / 通义千问", "openai", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    AiProvider("glm", "智谱 AI / GLM", "openai", "https://open.bigmodel.cn/api/paas/v4"),
    AiProvider("volcengine", "火山引擎方舟 / 豆包", "openai", "https://ark.cn-beijing.volces.com/api/v3"),
    AiProvider("hunyuan", "腾讯混元", "openai", "https://api.hunyuan.cloud.tencent.com/v1"),
    AiProvider("qianfan", "百度千帆 / 文心", "openai", "https://qianfan.baidubce.com/v2"),
    AiProvider("mistral", "Mistral AI", "openai", "https://api.mistral.ai/v1"),
    AiProvider("groq", "Groq", "openai", "https://api.groq.com/openai/v1"),
    AiProvider("openrouter", "OpenRouter", "openai", "https://openrouter.ai/api/v1"),
    AiProvider("siliconflow", "SiliconFlow", "openai", "https://api.siliconflow.cn/v1"),
    AiProvider("together", "Together AI", "openai", "https://api.together.xyz/v1"),
    AiProvider("fireworks", "Fireworks AI", "openai", "https://api.fireworks.ai/inference/v1"),
    AiProvider("deepinfra", "DeepInfra", "openai", "https://api.deepinfra.com/v1/openai"),
)
PROVIDERS: dict[str, AiProvider] = {provider.key: provider for provider in _PROVIDER_LIST}


class AiTranslationError(RuntimeError):
    """A user-facing (Chinese) failure with a stable machine-readable code."""

    def __init__(self, message: str, code: str = "ai_translation_failed") -> None:
        super().__init__(message)
        self.code = code


class AiRefusal(AiTranslationError):
    def __init__(self, message: str = "模型拒绝翻译这些内容（服务商的内容安全策略）") -> None:
        super().__init__(message, "ai_translation_refused")


@dataclass(frozen=True, slots=True)
class AiTranslationConfig:
    provider: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key: str = field(default="", repr=False)
    api_version: str = ""
    allow_private_network: bool = False
    instructions: str = ""
    daily_limit: int = 0

    @property
    def definition(self) -> AiProvider:
        return PROVIDERS.get(self.provider, PROVIDERS["openai_compatible"])

    @property
    def effective_base_url(self) -> str:
        return (self.base_url or self.definition.default_base_url).rstrip("/")

    @property
    def effective_api_version(self) -> str:
        return self.api_version or self.definition.default_api_version

    def missing_fields(self) -> list[str]:
        missing: list[str] = []
        if not self.effective_base_url:
            missing.append("Base URL")
        if not self.model:
            missing.append("模型")
        if self.definition.requires_api_key and not self.api_key:
            missing.append("API Key")
        return missing

    def public_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "base_url": self.base_url,
            "model": self.model,
            "api_version": self.api_version,
            "allow_private_network": self.allow_private_network,
            "instructions": self.instructions,
            "daily_limit": self.daily_limit,
            "api_key_configured": bool(self.api_key),
        }

    def cache_scope(self) -> str:
        return json.dumps(
            [self.provider, self.effective_base_url, self.model, self.instructions],
            ensure_ascii=False,
        )


_CONFIG_KEYS = frozenset(
    {
        "provider",
        "base_url",
        "model",
        "api_key",
        "api_version",
        "allow_private_network",
        "instructions",
        "daily_limit",
    }
)


def normalize_ai_translation_config(
    payload: object, current: AiTranslationConfig | None = None
) -> AiTranslationConfig:
    """Validate a submitted config; an omitted ``api_key`` keeps the saved one."""

    if not isinstance(payload, dict):
        raise AiTranslationError("AI 翻译配置格式无效", "ai_translation_invalid_config")
    unknown = set(payload) - _CONFIG_KEYS - {"api_key_configured"}
    if unknown:
        raise AiTranslationError("AI 翻译配置包含未知字段", "ai_translation_invalid_config")
    provider = _clean_line(payload.get("provider"), 40) or "openai_compatible"
    if provider not in PROVIDERS:
        raise AiTranslationError("不支持的 AI 服务商", "ai_translation_invalid_config")
    base_url = _clean_line(payload.get("base_url"), 2048)
    allow_private = payload.get("allow_private_network", False)
    if not isinstance(allow_private, bool):
        raise AiTranslationError("“允许本机或局域网地址”必须是开关值", "ai_translation_invalid_config")
    if base_url:
        _validate_base_url(base_url, allow_private)
    model = _clean_line(payload.get("model"), 200)
    api_version = _clean_line(payload.get("api_version"), 40)
    if api_version and not re.fullmatch(r"[A-Za-z0-9._-]+", api_version):
        raise AiTranslationError("API 版本格式无效", "ai_translation_invalid_config")
    instructions = payload.get("instructions") or ""
    if not isinstance(instructions, str):
        raise AiTranslationError("附加要求必须是文本", "ai_translation_invalid_config")
    instructions = _strip_controls(instructions, keep_newlines=True).strip()
    if len(instructions) > MAX_INSTRUCTIONS_LENGTH:
        raise AiTranslationError(
            f"附加要求不能超过 {MAX_INSTRUCTIONS_LENGTH} 个字符", "ai_translation_invalid_config"
        )
    daily_limit = payload.get("daily_limit", 0)
    if isinstance(daily_limit, bool) or not isinstance(daily_limit, int) or not 0 <= daily_limit <= 100_000:
        raise AiTranslationError("每日请求上限必须是 0–100000 的整数", "ai_translation_invalid_config")
    if "api_key" in payload:
        raw_key = payload.get("api_key")
        if raw_key is not None and not isinstance(raw_key, str):
            raise AiTranslationError("API Key 格式无效", "ai_translation_invalid_config")
        api_key = (raw_key or "").strip()
        if len(api_key) > 4096 or any(ord(character) < 32 or ord(character) == 127 for character in api_key):
            raise AiTranslationError("API Key 格式无效", "ai_translation_invalid_config")
    else:
        api_key = current.api_key if current else ""
    config = AiTranslationConfig(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key,
        api_version=api_version,
        allow_private_network=allow_private,
        instructions=instructions,
        daily_limit=daily_limit,
    )
    if not base_url and config.effective_base_url:
        _validate_base_url(config.effective_base_url, allow_private)
    return config


def ai_translation_config_path() -> Path:
    return runtime_data_dir() / "ai_translation.json"


def load_ai_translation_config(path: Path | None = None) -> AiTranslationConfig:
    target = path or ai_translation_config_path()
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return AiTranslationConfig()
    try:
        return normalize_ai_translation_config(payload)
    except AiTranslationError:
        return AiTranslationConfig()


def save_ai_translation_config(config: AiTranslationConfig, path: Path | None = None) -> AiTranslationConfig:
    target = path or ai_translation_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(
        {
            "provider": config.provider,
            "base_url": config.base_url,
            "model": config.model,
            "api_key": config.api_key,
            "api_version": config.api_version,
            "allow_private_network": config.allow_private_network,
            "instructions": config.instructions,
            "daily_limit": config.daily_limit,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    with _CONFIG_LOCK:
        handle, temporary = tempfile.mkstemp(prefix=".ai_translation.", dir=str(target.parent))
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(body)
            try:
                os.chmod(temporary, 0o600)
            except OSError:
                pass
            os.replace(temporary, target)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
    return config


def provider_catalog() -> list[dict[str, object]]:
    return [provider.public_dict() for provider in _PROVIDER_LIST]


@dataclass(slots=True)
class AiTranslationResult:
    translations: list[str | None]
    cached: int = 0
    refused: int = 0
    failed: int = 0
    error: str | None = None
    error_code: str | None = None

    def public_dict(self) -> dict[str, object]:
        return {
            "translations": self.translations,
            "cached": self.cached,
            "refused": self.refused,
            "failed": self.failed,
            "error": self.error,
            "code": self.error_code,
        }


class AiTranslationService:
    def __init__(self, database_path: Path | None = None) -> None:
        self.path = Path(database_path or default_database_path("translation_cache.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ai_translations ("
                "key TEXT PRIMARY KEY, translated TEXT NOT NULL, created_at REAL NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ai_translation_usage ("
                "day TEXT PRIMARY KEY, requests INTEGER NOT NULL)"
            )
            connection.commit()

    def usage_today(self) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT requests FROM ai_translation_usage WHERE day = ?", (_today(),)
            ).fetchone()
        return int(row[0]) if row else 0

    def translate(
        self, texts: list[str], config: AiTranslationConfig, target: str = "zh-CN"
    ) -> AiTranslationResult:
        if target not in TARGET_LANGUAGES:
            raise AiTranslationError("翻译目标语言无效", "ai_translation_invalid_request")
        if not isinstance(texts, list) or len(texts) > MAX_TEXTS:
            raise AiTranslationError(f"一次最多翻译 {MAX_TEXTS} 条", "ai_translation_invalid_request")
        _require_configured(config)
        clean = [_clean_text(text) for text in texts]
        keys = [_cache_key(config, target, text) if text else "" for text in clean]
        cached = self._cached([key for key in keys if key])
        result = AiTranslationResult(translations=[None] * len(clean))
        pending: list[str] = []
        for index, (text, key) in enumerate(zip(clean, keys)):
            if not text:
                continue
            if key in cached:
                result.translations[index] = cached[key]
                result.cached += 1
            else:
                pending.append(text)
        unique = list(dict.fromkeys(pending))
        if not unique:
            return result
        batches = [unique[start : start + BATCH_SIZE] for start in range(0, len(unique), BATCH_SIZE)]
        self._reserve_requests(config, len(batches))
        deadline = time.monotonic() + TOTAL_TIMEOUT_SECONDS
        translated: dict[str, str] = {}
        errors: list[AiTranslationError] = []
        with ThreadPoolExecutor(max_workers=min(MAX_PARALLEL_BATCHES, len(batches))) as pool:
            futures = [
                pool.submit(self._translate_batch, batch, config, target, deadline)
                for batch in batches
            ]
            for future in futures:
                values, batch_errors = future.result()
                translated.update(values)
                errors.extend(batch_errors)
        fresh: list[tuple[str, str]] = []
        for index, text in enumerate(clean):
            if result.translations[index] is not None or not text:
                continue
            value = translated.get(text)
            if value:
                result.translations[index] = value
                fresh.append((keys[index], value))
        self._store(fresh)
        missing = [text for text in unique if text not in translated]
        if missing:
            refusals = [error for error in errors if isinstance(error, AiRefusal)]
            result.refused = len(missing) if refusals and len(refusals) == len(errors) else 0
            result.failed = len(missing) - result.refused
            primary = next((error for error in errors if not isinstance(error, AiRefusal)), None)
            primary = primary or (errors[0] if errors else None)
            if primary is not None:
                result.error, result.error_code = str(primary), primary.code
            else:
                result.error, result.error_code = "AI 没有返回部分条目的译文", "ai_translation_incomplete"
        return result

    def test(self, config: AiTranslationConfig, target: str = "zh-CN") -> str:
        _require_configured(config)
        self._reserve_requests(config, 1)
        deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
        values = _request_translations([_TEST_TEXT], config, target, deadline)
        value = values.get(_TEST_TEXT)
        if not value:
            raise AiTranslationError("AI 返回的内容中没有译文", "ai_translation_invalid_response")
        return value

    def _translate_batch(
        self, batch: list[str], config: AiTranslationConfig, target: str, deadline: float
    ) -> tuple[dict[str, str], list[AiTranslationError]]:
        try:
            values = _request_translations(batch, config, target, deadline)
        except AiRefusal as refusal:
            if len(batch) < 2 or time.monotonic() >= deadline:
                return {}, [refusal]
            # One refused title can sink a whole batch. Split it once so the
            # rest of the page still gets translated.
            middle = len(batch) // 2
            output: dict[str, str] = {}
            errors: list[AiTranslationError] = []
            for half in (batch[:middle], batch[middle:]):
                try:
                    self._count_extra_request(config)
                    output.update(_request_translations(half, config, target, deadline))
                except AiTranslationError as exc:
                    errors.append(exc)
            return output, errors
        except AiTranslationError as exc:
            return {}, [exc]
        missing = [text for text in batch if text not in values]
        errors = [AiTranslationError("AI 没有返回部分条目的译文", "ai_translation_incomplete")] if missing else []
        return values, errors

    def _reserve_requests(self, config: AiTranslationConfig, count: int) -> None:
        day = _today()
        with _WRITE_LOCK, closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT requests FROM ai_translation_usage WHERE day = ?", (day,)
            ).fetchone()
            used = int(row[0]) if row else 0
            if config.daily_limit and used + count > config.daily_limit:
                raise AiTranslationError(
                    f"今日 AI 翻译请求已达上限（{config.daily_limit} 次），可在默认参数中调整",
                    "ai_translation_daily_limit",
                )
            connection.execute(
                "INSERT INTO ai_translation_usage (day, requests) VALUES (?, ?) "
                "ON CONFLICT(day) DO UPDATE SET requests = requests + excluded.requests",
                (day, count),
            )
            connection.execute("DELETE FROM ai_translation_usage WHERE day < ?", (_day_offset(-30),))
            connection.commit()

    def _count_extra_request(self, config: AiTranslationConfig) -> None:
        self._reserve_requests(config, 1)

    def _cached(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        output: dict[str, str] = {}
        with closing(self._connect()) as connection:
            for start in range(0, len(keys), 200):
                chunk = keys[start : start + 200]
                rows = connection.execute(
                    f"SELECT key, translated FROM ai_translations WHERE key IN ({','.join('?' for _ in chunk)})",
                    chunk,
                ).fetchall()
                output.update({str(row[0]): str(row[1]) for row in rows})
        return output

    def _store(self, rows: list[tuple[str, str]]) -> None:
        if not rows:
            return
        now = time.time()
        with _WRITE_LOCK, closing(self._connect()) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO ai_translations (key, translated, created_at) VALUES (?, ?, ?)",
                [(key, value, now) for key, value in rows],
            )
            count = int(connection.execute("SELECT COUNT(*) FROM ai_translations").fetchone()[0])
            if count > MAX_CACHE_ROWS:
                connection.execute(
                    "DELETE FROM ai_translations WHERE key IN (SELECT key FROM ai_translations "
                    "ORDER BY created_at LIMIT ?)",
                    (count - MAX_CACHE_ROWS,),
                )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


# ---------------------------------------------------------------------------
# Prompting and response parsing


def _system_prompt(config: AiTranslationConfig, target: str) -> str:
    language = TARGET_LANGUAGES[target]
    prompt = (
        "You translate titles of Japanese adult video products, including amateur "
        "uploads, for a personal media library. "
        f"Translate the text of every input item into {language}. "
        "Keep product codes (letters and digits), numbers and personal names as they "
        "appear in the original. Translate faithfully and naturally in the tone of the "
        "original, without explanations, notes or surrounding quotation marks. "
        "The input is a JSON array of objects with an id and a text. Reply with only a "
        "JSON array containing one object per input item, in the same order: "
        '[{"id": <id>, "translation": "<translated text>"}]'
    )
    if config.instructions:
        prompt += "\n\nAdditional requirements from the user:\n" + config.instructions
    return prompt


def _user_prompt(texts: list[str]) -> str:
    return json.dumps(
        [{"id": index + 1, "text": text} for index, text in enumerate(texts)],
        ensure_ascii=False,
    )


_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE = re.compile(r"^```[a-zA-Z0-9_-]*\s*|\s*```$")
_SALVAGE_ITEM = re.compile(r'"id"\s*:\s*"?(\d{1,4})"?\s*,\s*"translation"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _parse_translations(content: str, texts: list[str]) -> dict[str, str]:
    text = _THINK_BLOCK.sub("", content or "").strip()
    text = _FENCE.sub("", text).strip()
    values: dict[int, str] = {}
    parsed: Any = None
    for opener, closer in (("[", "]"), ("{", "}")):
        start, end = text.find(opener), text.rfind(closer)
        if start >= 0 and end > start:
            try:
                parsed = json.loads(text[start : end + 1])
                break
            except ValueError:
                continue
    if isinstance(parsed, dict):
        for key in ("translations", "items", "results", "data"):
            if isinstance(parsed.get(key), list):
                parsed = parsed[key]
                break
    if isinstance(parsed, list):
        if all(isinstance(item, str) for item in parsed) and len(parsed) == len(texts):
            values = {index + 1: item for index, item in enumerate(parsed)}
        else:
            for position, item in enumerate(parsed):
                if not isinstance(item, dict):
                    continue
                raw_id = item.get("id", position + 1)
                value = item.get("translation", item.get("text"))
                try:
                    identifier = int(raw_id)
                except (TypeError, ValueError):
                    continue
                if isinstance(value, str):
                    values[identifier] = value
    elif isinstance(parsed, dict):
        for raw_id, value in parsed.items():
            if isinstance(value, str) and str(raw_id).isdigit():
                values[int(raw_id)] = value
    if not values:
        # A truncated or slightly malformed reply still carries complete items.
        for match in _SALVAGE_ITEM.finditer(text):
            try:
                values[int(match.group(1))] = json.loads(f'"{match.group(2)}"')
            except ValueError:
                continue
    if not values and len(texts) == 1 and text and not text.startswith(("[", "{")):
        values[1] = text
    output: dict[str, str] = {}
    for identifier, value in values.items():
        if 1 <= identifier <= len(texts):
            cleaned = " ".join(_strip_controls(value).split())[: MAX_TEXT_LENGTH * 2]
            if cleaned:
                output[texts[identifier - 1]] = cleaned
    return output


# ---------------------------------------------------------------------------
# Wire protocols


@dataclass(frozen=True, slots=True)
class _HttpRequest:
    url: str
    headers: dict[str, str]
    body: dict[str, Any]


def _request_translations(
    texts: list[str], config: AiTranslationConfig, target: str, deadline: float
) -> dict[str, str]:
    system = _system_prompt(config, target)
    user = _user_prompt(texts)
    protocol = config.definition.protocol
    token_field = config.definition.token_limit_field
    max_tokens = MAX_OUTPUT_TOKENS
    swapped_field = False
    for _attempt in range(3):
        request = _build_request(config, system, user, max_tokens=max_tokens, token_field=token_field)
        try:
            payload = _post_json(request, config, deadline)
        except _UpstreamRejected as rejected:
            if rejected.status == 400 and _mentions_token_limit(rejected.detail):
                if protocol in {"openai", "azure_openai"} and not swapped_field:
                    token_field = "max_tokens" if token_field == "max_completion_tokens" else "max_completion_tokens"
                    swapped_field = True
                    continue
                if max_tokens > FALLBACK_OUTPUT_TOKENS:
                    max_tokens = FALLBACK_OUTPUT_TOKENS
                    continue
            raise rejected.as_error(config) from None
        content = _extract_content(protocol, payload)
        values = _parse_translations(content, texts)
        if not values:
            raise AiTranslationError("AI 返回的内容无法解析为译文，请换用更强的模型", "ai_translation_invalid_response")
        return values
    raise AiTranslationError("AI 服务拒绝了输出长度参数", "ai_translation_failed")


def _build_request(
    config: AiTranslationConfig, system: str, user: str, *, max_tokens: int, token_field: str
) -> _HttpRequest:
    protocol = config.definition.protocol
    base = config.effective_base_url
    key = config.api_key
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if protocol == "openai":
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return _HttpRequest(
            _join_suffix(base, "/chat/completions"),
            headers,
            {"model": config.model, "messages": messages, token_field: max_tokens},
        )
    if protocol == "azure_openai":
        headers = {"Content-Type": "application/json", "api-key": key}
        body: dict[str, Any] = {"messages": messages, token_field: max_tokens}
        if base.endswith("/chat/completions"):
            url = base
        elif re.search(r"/openai/v1$", base):
            # The Azure "v1" surface takes the deployment as the model name.
            return _HttpRequest(base + "/chat/completions", headers, {"model": config.model, **body})
        else:
            url = f"{base}/openai/deployments/{quote(config.model, safe='-_.~')}/chat/completions"
        if config.effective_api_version:
            url = _set_query(url, "api-version", config.effective_api_version)
        return _HttpRequest(url, headers, body)
    if protocol == "anthropic":
        url = base if base.endswith("/v1/messages") else (
            base + "/messages" if base.endswith("/v1") else base + "/v1/messages"
        )
        return _HttpRequest(
            url,
            {
                "Content-Type": "application/json",
                "x-api-key": key,
                "anthropic-version": config.effective_api_version or "2023-06-01",
            },
            {
                "model": config.model,
                "max_tokens": max_tokens,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
    if protocol == "gemini":
        if base.endswith(":generateContent"):
            url = base
        else:
            root = base if re.search(r"/v\d+(?:alpha|beta)?\d*$", base) else base + "/v1beta"
            url = f"{root}/models/{quote(config.model, safe='-_.~')}:generateContent"
        headers = {"Content-Type": "application/json"}
        if key:
            # The header keeps the key out of URLs, proxies and access logs.
            headers["x-goog-api-key"] = key
        return _HttpRequest(
            url,
            headers,
            {
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"maxOutputTokens": max_tokens},
            },
        )
    if protocol == "ollama":
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return _HttpRequest(
            _join_suffix(base, "/api/chat"),
            headers,
            {
                "model": config.model,
                "messages": messages,
                "stream": False,
                "options": {"num_predict": max_tokens},
            },
        )
    raise AiTranslationError("不支持的 AI 协议", "ai_translation_invalid_config")


_REFUSAL_FINISH_REASONS = frozenset(
    {"content_filter", "safety", "prohibited_content", "blocklist", "spii", "image_safety"}
)
_TRUNCATED_FINISH_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})


def _extract_content(protocol: str, payload: object) -> str:
    if not isinstance(payload, dict):
        raise AiTranslationError("AI 服务返回了无法识别的响应", "ai_translation_invalid_response")
    if protocol in {"openai", "azure_openai"}:
        choices = payload.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        if str(choice.get("finish_reason") or "").lower() in _REFUSAL_FINISH_REASONS or message.get("refusal"):
            raise AiRefusal()
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text") or "") if isinstance(part, dict) else str(part) for part in content
            )
        text = str(content or "")
        finish = str(choice.get("finish_reason") or "")
    elif protocol == "anthropic":
        if payload.get("stop_reason") == "refusal":
            raise AiRefusal()
        blocks = payload.get("content") if isinstance(payload.get("content"), list) else []
        text = "".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )
        finish = str(payload.get("stop_reason") or "")
    elif protocol == "gemini":
        feedback = payload.get("promptFeedback")
        if isinstance(feedback, dict) and feedback.get("blockReason"):
            raise AiRefusal()
        candidates = payload.get("candidates")
        candidate = candidates[0] if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict) else {}
        content = candidate.get("content") if isinstance(candidate.get("content"), dict) else {}
        parts = content.get("parts") if isinstance(content.get("parts"), list) else []
        text = "".join(
            str(part.get("text") or "")
            for part in parts
            if isinstance(part, dict) and not part.get("thought")
        )
        finish = str(candidate.get("finishReason") or "")
        if not text.strip() and finish.lower() in _REFUSAL_FINISH_REASONS:
            raise AiRefusal()
    elif protocol == "ollama":
        message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
        text = str(message.get("content") or "")
        finish = str(payload.get("done_reason") or "")
    else:
        raise AiTranslationError("不支持的 AI 协议", "ai_translation_invalid_config")
    if not text.strip() and finish.lower() in _TRUNCATED_FINISH_REASONS:
        raise AiTranslationError(
            "模型用尽了输出长度仍未给出译文（推理模型较常见），请换用非推理模型",
            "ai_translation_truncated",
        )
    if not text.strip():
        raise AiTranslationError("AI 返回了空内容", "ai_translation_invalid_response")
    return text


# ---------------------------------------------------------------------------
# Transport


class _UpstreamRejected(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status
        self.detail = detail

    def as_error(self, config: AiTranslationConfig) -> AiTranslationError:
        detail = _redact(self.detail, config)
        if _looks_like_refusal(self.detail):
            return AiRefusal()
        reason = {
            400: "请求被拒绝，请检查模型名称与参数",
            401: "API Key 无效或已过期",
            403: "API Key 没有访问该模型的权限",
            404: "接口地址或模型不存在，请检查 Base URL 与模型名称",
            408: "AI 服务响应超时",
            413: "请求内容过长",
            429: "请求过于频繁或账户额度不足",
        }.get(self.status, "AI 服务暂时不可用" if self.status >= 500 else "AI 服务拒绝了请求")
        message = f"{reason}（HTTP {self.status}{'：' + detail if detail else ''}）"
        return AiTranslationError(message[:230], f"ai_http_{self.status}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        raise AiTranslationError("AI 接口返回了重定向，请把 Base URL 改成最终地址", "ai_translation_redirect")


def _post_json(request: _HttpRequest, config: AiTranslationConfig, deadline: float) -> object:
    parsed = urlsplit(request.url)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    public = bool(resolve_public_addresses(host.rstrip(".").lower(), port))
    if public:
        if parsed.scheme != "https":
            raise AiTranslationError("公网 AI 服务必须使用 HTTPS 地址", "ai_translation_invalid_config")
        opener = build_opener(ProxyHandler(), PinnedHTTPHandler(), PinnedHTTPSHandler(), _NoRedirect())
    elif config.allow_private_network:
        # Local and LAN services (Ollama, self-hosted gateways) are reached
        # directly; a system proxy normally cannot route to them.
        opener = build_opener(
            ProxyHandler({}),
            HTTPHandler(),
            HTTPSHandler(context=ssl.create_default_context()),
            _NoRedirect(),
        )
    else:
        raise AiTranslationError(
            "Base URL 指向本机或局域网地址，或域名无法解析；使用 Ollama 等本地服务时请开启“允许本机或局域网地址”",
            "ai_translation_private_host",
        )
    body = json.dumps(request.body, ensure_ascii=False).encode("utf-8")
    headers = {
        **request.headers,
        "Accept": "application/json",
        "User-Agent": "JAV-Pilot/ai-translation",
    }
    remaining = deadline - time.monotonic()
    if remaining <= 1:
        raise AiTranslationError("AI 翻译超时", "ai_translation_timeout")
    timeout = min(REQUEST_TIMEOUT_SECONDS, remaining)
    acquired = _UPSTREAM_SLOTS.acquire(timeout=timeout)
    if not acquired:
        raise AiTranslationError("AI 翻译请求排队超时，请稍后重试", "ai_translation_timeout")
    try:
        remaining = deadline - time.monotonic()
        if remaining <= 1:
            raise AiTranslationError("AI 翻译超时", "ai_translation_timeout")
        try:
            with opener.open(
                Request(request.url, data=body, headers=headers, method="POST"),
                timeout=min(REQUEST_TIMEOUT_SECONDS, remaining),
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            try:
                detail = exc.read(MAX_ERROR_BYTES).decode("utf-8", errors="replace")
            except (OSError, HTTPException):
                detail = ""
            finally:
                exc.close()
            raise _UpstreamRejected(exc.code, _error_detail(detail)) from None
        except (TimeoutError, ssl.SSLError, URLError, HTTPException, OSError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise AiTranslationError("AI 服务响应超时，请稍后重试或换用更快的模型", "ai_translation_timeout") from None
            reason = getattr(exc, "reason", exc)
            raise AiTranslationError(
                f"无法连接 AI 服务（{_redact(_strip_controls(str(reason)), config)[:120]}）",
                "ai_translation_network",
            ) from None
    finally:
        _UPSTREAM_SLOTS.release()
    if len(raw) > MAX_RESPONSE_BYTES:
        raise AiTranslationError("AI 服务的响应过大", "ai_translation_invalid_response")
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        raise AiTranslationError("AI 服务返回的不是 JSON，请检查 Base URL", "ai_translation_invalid_response") from None


def _error_detail(body: str) -> str:
    try:
        payload = json.loads(body)
    except ValueError:
        payload = None
    message: object = None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        payload = payload[0]
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or error.get("type")
            code = error.get("code")
            if isinstance(code, str) and isinstance(message, str) and code not in message:
                message = f"{code}: {message}"
        elif isinstance(error, str):
            message = error
        message = message or payload.get("message") or payload.get("msg") or payload.get("detail")
    text = message if isinstance(message, str) else ("" if payload is not None else body)
    return " ".join(_strip_controls(str(text)).split())[:160]


def _looks_like_refusal(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in (
            "content_filter",
            "contentfilter",
            "content filter",
            "content management policy",
            "data_inspection_failed",
            "inappropriate content",
            "sensitive",
            "moderation",
            "敏感",
            "不安全",
        )
    )


def _mentions_token_limit(detail: str) -> bool:
    lowered = detail.lower()
    return any(
        marker in lowered
        for marker in ("max_tokens", "max_completion_tokens", "maxoutputtokens", "num_predict", "unsupported_parameter")
    )


def _require_configured(config: AiTranslationConfig) -> None:
    missing = config.missing_fields()
    if missing:
        raise AiTranslationError(
            f"AI 翻译尚未配置完整（缺少 {'、'.join(missing)}），请在 系统管理 → 默认参数 中设置",
            "ai_translation_not_configured",
        )


def _validate_base_url(value: str, allow_private: bool) -> None:
    if len(value) > 2048 or any(character.isspace() for character in value) or "\\" in value:
        raise AiTranslationError("Base URL 格式无效", "ai_translation_invalid_config")
    try:
        parsed = urlsplit(value)
        parsed.port  # noqa: B018 - raises for an invalid port
    except ValueError:
        raise AiTranslationError("Base URL 格式无效", "ai_translation_invalid_config") from None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AiTranslationError("Base URL 必须以 http:// 或 https:// 开头", "ai_translation_invalid_config")
    if parsed.username is not None or parsed.password is not None or parsed.fragment:
        raise AiTranslationError("Base URL 不能包含账号密码或 # 片段", "ai_translation_invalid_config")
    host = parsed.hostname.lower()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    local = (
        (literal is not None and not literal.is_global)
        or host == "localhost"
        or host.endswith((".localhost", ".local", ".lan", ".home", ".internal"))
        or "." not in host
    )
    if local and not allow_private:
        raise AiTranslationError(
            "Base URL 是本机或局域网地址，请同时开启“允许本机或局域网地址”", "ai_translation_invalid_config"
        )
    if parsed.scheme == "http" and not local:
        raise AiTranslationError("公网 AI 服务必须使用 HTTPS 地址", "ai_translation_invalid_config")


def _join_suffix(base: str, suffix: str) -> str:
    return base if base.endswith(suffix) else base + suffix


def _set_query(url: str, key: str, value: str) -> str:
    parts = urlsplit(url)
    query = [(name, item) for name, item in parse_qsl(parts.query, keep_blank_values=True) if name != key]
    query.append((key, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _redact(text: str, config: AiTranslationConfig) -> str:
    output = text
    if config.api_key and len(config.api_key) >= 4:
        output = output.replace(config.api_key, "***")
    return re.sub(r"(?i)((?:api[-_]?key|key|token)=)[^&\s]+", r"\1***", output)


def _strip_controls(value: str, *, keep_newlines: bool = False) -> str:
    allowed = {"\n"} if keep_newlines else set()
    return "".join(
        character
        for character in str(value)
        if character in allowed or not (ord(character) < 32 or 127 <= ord(character) < 160)
    )


def _clean_line(value: object, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AiTranslationError("AI 翻译配置格式无效", "ai_translation_invalid_config")
    text = _strip_controls(value).strip()
    if len(text) > limit:
        raise AiTranslationError("AI 翻译配置的内容过长", "ai_translation_invalid_config")
    return text


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(unicodedata.normalize("NFKC", value).split())
    return text[:MAX_TEXT_LENGTH]


def _cache_key(config: AiTranslationConfig, target: str, text: str) -> str:
    return hashlib.sha256(f"{config.cache_scope()}\0{target}\0{text}".encode("utf-8")).hexdigest()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _day_offset(days: int) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(time.time() + days * 86400))


def config_for_test(payload: object, saved: AiTranslationConfig) -> AiTranslationConfig:
    """A draft config from the settings form, keeping the saved key if omitted."""

    if payload is None:
        return saved
    return normalize_ai_translation_config(payload, saved)


__all__ = [
    "AiRefusal",
    "AiTranslationConfig",
    "AiTranslationError",
    "AiTranslationService",
    "PROVIDERS",
    "TARGET_LANGUAGES",
    "ai_translation_config_path",
    "config_for_test",
    "load_ai_translation_config",
    "normalize_ai_translation_config",
    "provider_catalog",
    "save_ai_translation_config",
]
