"""Title and synopsis translation through free public translation endpoints.

Two keyless endpoints are tried in order: the Google dictionary endpoint used
by Chrome's dictionary extension (batched, one request per chunk) and
MyMemory (one text per request). Translations are cached on disk so a page of
results is translated once, and texts are only sent when the user enabled
translation.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import unicodedata
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from ..net.http_client import FetchError, fetch_text
from ..config.paths import default_database_path

MAX_TEXTS = 60
MAX_TEXT_LENGTH = 1200
MAX_CACHE_ROWS = 50_000
TARGET_LANGUAGES = frozenset({"zh-CN", "zh-TW", "en"})
_GOOGLE_ORIGIN = "https://clients5.google.com"
_MYMEMORY_ORIGIN = "https://api.mymemory.translated.net"
_GOOGLE_BATCH_URL_BYTES = 6_000
_LOCK = threading.Lock()


class TranslationError(RuntimeError):
    pass


class TranslationService:
    def __init__(self, database_path: Path | None = None) -> None:
        self.path = Path(database_path or default_database_path("translation_cache.sqlite3"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS translations ("
                "key TEXT PRIMARY KEY, target TEXT NOT NULL, translated TEXT NOT NULL, "
                "provider TEXT NOT NULL, created_at REAL NOT NULL)"
            )
            connection.commit()

    def translate(self, texts: list[str], target: str = "zh-CN") -> list[str | None]:
        if target not in TARGET_LANGUAGES:
            raise TranslationError("translation target is invalid")
        if not isinstance(texts, list) or len(texts) > MAX_TEXTS:
            raise TranslationError("too many texts to translate")
        clean = [_clean_text(text) for text in texts]
        results: list[str | None] = [None] * len(clean)
        keys = [_cache_key(text, target) if text else "" for text in clean]
        cached = self._cached([key for key in keys if key])
        pending: list[int] = []
        for index, (text, key) in enumerate(zip(clean, keys)):
            if not text:
                continue
            if _already_target(text, target):
                results[index] = text
            elif key in cached:
                results[index] = cached[key]
            else:
                pending.append(index)
        if not pending:
            return results
        unique = list(dict.fromkeys(clean[index] for index in pending))
        translated = _translate_google(unique, target)
        missing = [text for text in unique if not translated.get(text)]
        for text in missing[:10]:
            try:
                value = _translate_mymemory(text, target)
            except (FetchError, TranslationError, ValueError):
                continue
            if value:
                translated[text] = value
        fresh: list[tuple[str, str, str]] = []
        for index in pending:
            value = translated.get(clean[index])
            if value:
                results[index] = value
                fresh.append((keys[index], target, value))
        self._store(fresh)
        return results

    def _cached(self, keys: list[str]) -> dict[str, str]:
        if not keys:
            return {}
        output: dict[str, str] = {}
        with closing(self._connect()) as connection:
            for start in range(0, len(keys), 200):
                chunk = keys[start : start + 200]
                rows = connection.execute(
                    f"SELECT key, translated FROM translations WHERE key IN ({','.join('?' for _ in chunk)})",
                    chunk,
                ).fetchall()
                output.update({str(row[0]): str(row[1]) for row in rows})
        return output

    def _store(self, rows: list[tuple[str, str, str]]) -> None:
        if not rows:
            return
        now = time.time()
        with _LOCK, closing(self._connect()) as connection:
            connection.executemany(
                "INSERT OR REPLACE INTO translations (key, target, translated, provider, created_at) "
                "VALUES (?, ?, ?, 'public', ?)",
                [(key, target, value, now) for key, target, value in rows],
            )
            count = int(connection.execute("SELECT COUNT(*) FROM translations").fetchone()[0])
            if count > MAX_CACHE_ROWS:
                connection.execute(
                    "DELETE FROM translations WHERE key IN (SELECT key FROM translations "
                    "ORDER BY created_at LIMIT ?)",
                    (count - MAX_CACHE_ROWS,),
                )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection


def _clean_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = " ".join(unicodedata.normalize("NFKC", value).split())
    return text[:MAX_TEXT_LENGTH]


def _cache_key(text: str, target: str) -> str:
    return hashlib.sha256(f"{target}\0{text}".encode("utf-8")).hexdigest()


def _already_target(text: str, target: str) -> bool:
    """Skip texts with no kana or Hangul when the target is Chinese."""

    if not target.startswith("zh"):
        return False
    has_letters = any(character.isalpha() for character in text)
    foreign = any(
        "぀" <= character <= "ヿ" or "가" <= character <= "힯"
        for character in text
    )
    latin = sum(character.isascii() and character.isalpha() for character in text)
    return has_letters and not foreign and latin < len(text) * 0.5


def _translate_google(texts: list[str], target: str) -> dict[str, str]:
    output: dict[str, str] = {}
    chunk: list[str] = []
    size = 0
    for text in texts:
        encoded = len(quote(text, safe="")) + 3
        if chunk and size + encoded > _GOOGLE_BATCH_URL_BYTES:
            output.update(_google_request(chunk, target))
            chunk, size = [], 0
        chunk.append(text)
        size += encoded
    if chunk:
        output.update(_google_request(chunk, target))
    return output


def _google_request(texts: list[str], target: str) -> dict[str, str]:
    query = "&".join(f"q={quote(text, safe='')}" for text in texts)
    url = f"{_GOOGLE_ORIGIN}/translate_a/t?client=dict-chrome-ex&sl=auto&tl={quote(target)}&{query}"
    try:
        body = fetch_text(url, timeout=15.0, max_bytes=512 * 1024, allowed_origin=_GOOGLE_ORIGIN)
        payload = json.loads(body)
    except (FetchError, ValueError):
        return {}
    if not isinstance(payload, list):
        return {}
    output: dict[str, str] = {}
    for text, item in zip(texts, payload):
        value = item[0] if isinstance(item, list) and item else item
        if isinstance(value, str) and value.strip():
            output[text] = " ".join(value.split())[:MAX_TEXT_LENGTH * 2]
    return output


def _translate_mymemory(text: str, target: str) -> str | None:
    pair = f"ja|{target}"
    url = f"{_MYMEMORY_ORIGIN}/get?q={quote(text[:500], safe='')}&langpair={quote(pair, safe='')}"
    payload = json.loads(
        fetch_text(url, timeout=15.0, max_bytes=256 * 1024, allowed_origin=_MYMEMORY_ORIGIN)
    )
    if not isinstance(payload, dict) or payload.get("responseStatus") not in {200, "200"}:
        return None
    data = payload.get("responseData")
    value = data.get("translatedText") if isinstance(data, dict) else None
    return " ".join(value.split()) if isinstance(value, str) and value.strip() else None
