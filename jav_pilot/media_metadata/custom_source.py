"""User-defined public JSON metadata, imported only through review."""

import json
import re
from urllib.parse import quote, urlsplit

from ..core.catalog_code import normalize_catalog_code
from ..net.http_client import fetch_bytes
from ..watching.store import connection
from .review.models import METADATA_FIELDS


def settings(value=None):
    with connection() as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS custom_metadata (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)"
        )
        if value is not None:
            url, fields = value.get("url"), value.get("fields")
            if not isinstance(url, str) or len(url) > 1000:
                raise ValueError("资料接口地址无效")
            parts = urlsplit(url)
            if url and (
                url.count("{code}") != 1
                or parts.scheme != "https"
                or not parts.hostname
                or parts.port not in (None, 443)
                or parts.username
                or parts.password
                or parts.fragment
                or "{" in parts.netloc
            ):
                raise ValueError("请输入含 {code} 的公开 HTTPS 地址")
            if (
                not isinstance(fields, dict)
                or (url and "code" not in fields)
                or set(fields) - set(METADATA_FIELDS) - {"code"}
            ):
                raise ValueError("资料字段映射无效")
            for path in fields.values():
                if (
                    not isinstance(path, str)
                    or not re.fullmatch(r"[\w-]+(?:\.[\w-]+){0,12}", path)
                    or len(path) > 160
                ):
                    raise ValueError("字段路径无效")
            db.execute(
                "INSERT OR REPLACE INTO custom_metadata VALUES(1,?)",
                (json.dumps({"url": url, "fields": fields}),),
            )
        row = db.execute("SELECT data FROM custom_metadata WHERE id=1").fetchone()
        return (
            json.loads(row[0])
            if row
            else {"url": "", "fields": {"code": "code", "title": "title"}}
        )


def fetch_fields(code):
    config = settings()
    if not config["url"]:
        raise ValueError("请先配置自定义资料源")
    identity = normalize_catalog_code(code)
    if not identity:
        raise ValueError("番号无效")
    url = config["url"].replace("{code}", quote(identity[0], safe=""))
    raw = fetch_bytes(url, timeout=12, max_bytes=1024 * 1024, allowed_origins=(url,))
    data = json.loads(raw)
    result = {}
    for name, path in config["fields"].items():
        value = data
        for key in path.split("."):
            if isinstance(value, list) and key.isdigit() and int(key) < len(value):
                value = value[int(key)]
            elif isinstance(value, dict):
                value = value.get(key)
            else:
                value = None
        if value is not None:
            result[name] = value
    found = normalize_catalog_code(result.pop("code", None))
    if not found or found[1] != identity[1]:
        raise ValueError("资料源返回的番号不匹配")
    return result
