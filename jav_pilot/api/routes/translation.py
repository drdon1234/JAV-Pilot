"""Translation and AI translation endpoints."""

from __future__ import annotations

import sqlite3
import time
from http import HTTPStatus

from ...translation.ai import (
    AiTranslationError,
    load_ai_translation_config,
    normalize_ai_translation_config,
    save_ai_translation_config,
)
from ...translation.ai import (
    config_for_test as ai_translation_config_for_test,
)
from ...translation.ai import (
    provider_catalog as ai_translation_providers,
)
from ...translation.machine import TranslationError
from ..base import BaseHandler
from ..services.translation import (
    ai_error_status,
    ai_translation_service,
    translation_service,
)


class TranslationRoutes(BaseHandler):
    def _handle_translate(self) -> None:
        try:
            payload = self._read_json_body(128 * 1024)
            if not set(payload).issubset({"texts", "target"}):
                raise ValueError("translation request is invalid")
            texts = payload.get("texts")
            if not isinstance(texts, list) or any(not isinstance(item, str) for item in texts):
                raise ValueError("translation texts are invalid")
            translations = translation_service().translate(
                texts, str(payload.get("target") or "zh-CN")
            )
        except (ValueError, TranslationError) as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "translation cache is unavailable"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json({"ok": True, "translations": translations})

    def _ai_translation_snapshot(self) -> dict[str, object]:
        config = load_ai_translation_config()
        try:
            used = ai_translation_service().usage_today()
        except (OSError, sqlite3.Error):
            used = 0
        return {
            "ok": True,
            "config": config.public_dict(),
            "configured": not config.missing_fields(),
            "missing": config.missing_fields(),
            "providers": ai_translation_providers(),
            "usage_today": used,
        }

    def _handle_ai_translation_config(self) -> None:
        self._send_json(self._ai_translation_snapshot())

    def _handle_ai_translation_config_save(self) -> None:
        try:
            payload = self._read_json_body(32 * 1024)
            config = normalize_ai_translation_config(payload, load_ai_translation_config())
            save_ai_translation_config(config)
        except AiTranslationError as exc:
            self._send_json({"ok": False, "error": str(exc), "code": exc.code}, HTTPStatus.BAD_REQUEST)
            return
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except OSError:
            self._send_json(
                {"ok": False, "error": "AI 翻译配置无法保存到数据目录"},
                HTTPStatus.SERVICE_UNAVAILABLE,
            )
            return
        self._send_json(self._ai_translation_snapshot())

    def _handle_ai_translation_test(self) -> None:
        started = time.monotonic()
        try:
            payload = self._read_json_body(32 * 1024)
            if not set(payload).issubset({"config"}):
                raise ValueError("AI 翻译测试请求无效")
            config = ai_translation_config_for_test(payload.get("config"), load_ai_translation_config())
            translation = ai_translation_service().test(config)
        except AiTranslationError as exc:
            self._send_json({"ok": False, "error": str(exc), "code": exc.code}, ai_error_status(exc))
            return
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "AI 翻译缓存不可用"}, HTTPStatus.SERVICE_UNAVAILABLE
            )
            return
        self._send_json(
            {
                "ok": True,
                "translation": translation,
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
        )

    def _handle_ai_translate(self) -> None:
        try:
            payload = self._read_json_body(128 * 1024)
            if not set(payload).issubset({"texts", "target"}):
                raise ValueError("AI 翻译请求无效")
            texts = payload.get("texts")
            if not isinstance(texts, list) or any(not isinstance(item, str) for item in texts):
                raise ValueError("AI 翻译文本无效")
            result = ai_translation_service().translate(
                texts, load_ai_translation_config(), str(payload.get("target") or "zh-CN")
            )
        except AiTranslationError as exc:
            self._send_json({"ok": False, "error": str(exc), "code": exc.code}, ai_error_status(exc))
            return
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)
            return
        except (OSError, sqlite3.Error):
            self._send_json(
                {"ok": False, "error": "AI 翻译缓存不可用"}, HTTPStatus.SERVICE_UNAVAILABLE
            )
            return
        body = result.public_dict()
        if result.error and not any(result.translations):
            # Nothing could be translated: report it as a failure so the
            # page shows the cause instead of an empty result.
            self._send_json(
                {"ok": False, "error": result.error, "code": result.error_code},
                HTTPStatus.BAD_GATEWAY,
            )
            return
        self._send_json({"ok": True, **body})
