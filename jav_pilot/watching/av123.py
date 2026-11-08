"""123AV's declared player JSON and recommendation API; no remote script evaluation."""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from urllib.parse import parse_qs, quote, urlsplit

from bs4 import BeautifulSoup

from ..net.network_guard import PublicHostResolver
from ..web_download.media import media_provider_scope, require_allowed_media_url
from ..web_download.providers import ProviderManifest, WebDownloadProviderError, _bounded_get, _session

_VISITOR = str(uuid.uuid4())


def episodes(document: str) -> list[dict]:
    node = BeautifulSoup(document, "html.parser").select_one('.watch__main[x-data]')
    match = re.match(r"player\(JSON\.parse\('((?:\\.|[^'\\])*)'\)", str(node.get('x-data', '')) if node else '')
    if not match:
        raise WebDownloadProviderError('123AV player data not found', code='response_invalid')
    def unescape(match):
        value = match.group(1)
        return chr(int(value[1:], 16)) if value[0] in {'u', 'x'} else {'n': '\n', 'r': '\r', 't': '\t'}.get(value, value)
    try:
        data = json.loads(re.sub(r'\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[\\/\'"nrt])', unescape, match.group(1)))
    except ValueError as exc:
        raise WebDownloadProviderError('123AV player data is invalid', code='response_invalid') from exc
    if not isinstance(data, list) or len(data) > 100:
        raise WebDownloadProviderError('123AV player data is invalid', code='response_invalid')
    return [item for item in data if isinstance(item, dict) and re.fullmatch(r'https://sadie-shop\.site/e/[a-z0-9_]+(?:\?[^\s#]*)?', str(item.get('url', '')), re.I)]


def poster(document: str) -> str:
    try:
        choices = episodes(document)
        return parse_qs(urlsplit(choices[0]['url']).query).get('poster', [''])[0] if choices else ''
    except WebDownloadProviderError:
        return ''


def capture(session, document: str, page_url: str) -> ProviderManifest:
    choices = episodes(document)
    if not choices:
        raise WebDownloadProviderError('123AV has no playback source', code='not_found')
    token = urlsplit(choices[0]['url']).path.rsplit('/', 1)[-1]
    referer = 'https://sadie-shop.site/e/' + token
    raw = _bounded_get(session, 'https://sadie-shop.site/stream?id=' + token, 20,
                       allowed_hosts={'sadie-shop.site'}, headers={'Referer': referer}, max_bytes=1024 * 1024)
    try:
        manifest = json.loads(raw)['media']['stream']
    except (ValueError, KeyError, TypeError) as exc:
        raise WebDownloadProviderError('123AV stream response is invalid', code='response_invalid') from exc
    if not isinstance(manifest, str) or not re.fullmatch(r'https://[^\s#]+\.m3u8(?:\?[^\s#]*)?', manifest):
        raise WebDownloadProviderError('123AV HLS source is unavailable', code='not_found')
    host = urlsplit(manifest).hostname or ''
    with media_provider_scope('av123', allowed_hosts=(host,)):
        require_allowed_media_url(manifest, resolver=PublicHostResolver(max_hosts=1))
    return ProviderManifest('av123', manifest, {'Referer': referer, 'Origin': 'https://sadie-shop.site'}, page_url, allowed_media_hosts=(host,))


def related(document: str, page_url: str) -> list[dict]:
    from .catalog import identity
    soup = BeautifulSoup(document, 'html.parser')
    node = soup.select_one('[x-data^="recommendation("]')
    config = str(node.get('x-data', '')) if node else ''
    key = re.search(r"\bkey:\s*'([a-f0-9-]{36})'", config)
    if not key or "base: 'https://rec2.123av.com'" not in config or "scenario: 'watch-similar'" not in config:
        return []
    if not PublicHostResolver(max_hosts=1).is_public('rec2.123av.com'):
        return []
    ua, timestamp = 'Mozilla/5.0', str(int(time.time()))
    signature = 0
    for char in key[1] + timestamp + ua:
        signature = ((signature << 5) - signature + ord(char)) & 0xffffffff
    code = urlsplit(page_url).path.rsplit('/', 1)[-1]
    try:
        response = _session().post('https://rec2.123av.com/items/' + quote(code, safe='') + '/items',
            params={'timestamp': timestamp, 'signature': format(signature, 'x')},
            headers={'User-Agent': ua}, json={'count': 12, 'target_user': _VISITOR,
                'included_properties': ['code', 'title_cn', 'duration'], 'scenario': 'watch-similar'},
            timeout=12, allow_redirects=False)
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            return []
        data = response.json()
    except Exception:
        return []
    if not isinstance(data, dict) or not isinstance(data.get('recomms', []), list):
        return []
    items = []
    for item in data.get('recomms', [])[:24]:
        if not isinstance(item, dict):
            continue
        slug = str(item.get('code', '')).lower()
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{1,120}', slug):
            continue
        work_code, editions = identity(slug)
        prefix = hashlib.md5(slug.encode(), usedforsecurity=False).hexdigest()[:2]
        items.append({'source': 'av123', 'url': 'https://123av.com/cn/v/' + slug,
            'title': (slug.upper() + ' ' + str(item.get('title_cn', '')))[:500],
            'code': work_code, 'editions': editions,
            'cover': f'https://icdn.123av.me/img2/s360/{prefix}/{slug}/cover.jpg'})
    return items
