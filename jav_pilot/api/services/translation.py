"""Machine and AI translation service accessors."""

from __future__ import annotations

from http import HTTPStatus

from ...translation.ai import AiTranslationError, AiTranslationService
from ...translation.machine import TranslationService
from .. import state


def translation_service() -> TranslationService:
    with state.LAZY_SERVICES_LOCK:
        if state.TRANSLATION_SERVICE is None:
            state.TRANSLATION_SERVICE = TranslationService()
        return state.TRANSLATION_SERVICE


def ai_translation_service() -> AiTranslationService:
    with state.LAZY_SERVICES_LOCK:
        if state.AI_TRANSLATION_SERVICE is None:
            state.AI_TRANSLATION_SERVICE = AiTranslationService()
        return state.AI_TRANSLATION_SERVICE


def ai_error_status(error: AiTranslationError) -> HTTPStatus:
    if error.code in {"ai_translation_invalid_config", "ai_translation_invalid_request", "ai_translation_not_configured", "ai_translation_private_host"}:
        return HTTPStatus.BAD_REQUEST
    if error.code == "ai_translation_daily_limit":
        return HTTPStatus.TOO_MANY_REQUESTS
    return HTTPStatus.BAD_GATEWAY
