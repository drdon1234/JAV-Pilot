"""Read SupJav's current gateway and declared HLS lines without executing scripts."""
from __future__ import annotations

import base64
import codecs
import json
import re
import time
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from ..net.network_guard import PublicHostResolver
from ..web_download.providers import (
    ProviderManifest, WebDownloadProviderError, _bounded_page,
    _extract_packed_manifest, _origin, _session,
)


def _public(url: str, *, embed: bool = False, resolver: PublicHostResolver | None = None) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in {None, 443} or not parsed.hostname
            or not (resolver or PublicHostResolver(max_hosts=1)).is_public(parsed.hostname)
            or embed and not re.fullmatch(r'/(?:e|embed)/[a-zA-Z0-9]{6,64}/?', parsed.path)):
        raise ValueError('SupJav source URL rejected')
    return url


def _read(session, url: str, deadline: float, headers: dict | None = None) -> tuple[str, str]:
    for _ in range(4):
        _public(url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('SupJav source timeout')
        response = session.get(url, headers=headers or {}, timeout=min(10, remaining), allow_redirects=False)
        if response.status_code in {301, 302, 303, 307, 308}:
            url = urljoin(url, str(response.headers.get('location', '')))
            continue
        if response.status_code != 200 or not response.content or len(response.content) > 4 * 1024 * 1024:
            raise ValueError('SupJav source response invalid')
        return response.text, url
    raise ValueError('SupJav source redirect limit')


def _voe(document: str) -> str:
    match = re.search(r'<script type="application/json">\s*(\["[^"<]{16,1048576}"\])\s*</script>', document)
    if not match:
        return ''
    value = codecs.decode(json.loads(match[1])[0], 'rot_13')
    for marker in ['@$', '^^', '~@', '%?', '*~', '!!', '#&']:
        value = value.replace(marker, '')
    value = ''.join(chr(char - 3) for char in base64.b64decode(value))[::-1]
    return json.loads(base64.b64decode(value))['source']


def _line(name: str, token: str, page_url: str, deadline: float) -> ProviderManifest:
    session = _session()
    gateway = 'https://lk1.supremejav.com/supjav.php?l=' + token
    document, _ = _read(session, gateway, deadline, {'Referer': _origin(page_url) + '/'})
    olid = re.search(r"var\s+OLID\s*=\s*'([a-zA-Z0-9]{32,512})'\s*;\s*OLID\s*=\s*OLID\.split\(''\)\.reverse\(\)\.join\(''\)", document)
    if not olid:
        raise ValueError('SupJav gateway data changed')
    jump = session.get('https://lk1.supremejav.com/supjav.php?c=' + olid[1][::-1],
                       headers={'Referer': gateway}, timeout=min(10, max(0.1, deadline - time.monotonic())), allow_redirects=False)
    if jump.status_code not in {301, 302, 303, 307, 308}:
        raise ValueError('SupJav gateway did not redirect')
    embed = _public(urljoin(gateway, str(jump.headers.get('location', ''))), embed=True)
    if name == 'VAS':
        api = _origin(embed) + '/api/stream'
        _public(api)
        response = session.post(api, json={'filecode': urlsplit(embed).path.split('/')[2], 'device': 'android', 'codecs': []},
            headers={'Referer': embed}, timeout=min(10, max(0.1, deadline - time.monotonic())), allow_redirects=False)
        if response.status_code != 200 or len(response.content) > 1024 * 1024:
            raise ValueError('SupJav stream response invalid')
        manifest = response.json()['streaming_url']
    else:
        document, final = _read(session, embed, deadline, {'Referer': 'https://lk1.supremejav.com/'})
        if name == 'VOE':
            manifest = _voe(document)
            if not manifest:
                redirect = re.search(r"window\.location\.href\s*=\s*'([^'\s]{1,512})'", document)
                if not redirect:
                    raise ValueError('SupJav VOE source unavailable')
                document, _ = _read(session, _public(urljoin(final, redirect[1]), embed=True), deadline)
                manifest = _voe(document)
        else:
            manifest = _extract_packed_manifest(document)
    _public(manifest)
    if not urlsplit(manifest).path.endswith('.m3u8'):
        raise ValueError('SupJav source is not HLS')
    headers = {'Referer': 'https://lk1.supremejav.com/'}
    document, final = _read(session, manifest, deadline, headers)
    for _ in range(3):
        if '#EXT-X-STREAM-INF' not in document:
            break
        path = next(line.strip() for line in document.splitlines() if line.strip() and not line.startswith('#'))
        document, final = _read(session, urljoin(final, path), deadline, headers)
    if not document.startswith('#EXTM3U') or '#EXTINF' not in document:
        raise ValueError('SupJav media playlist unavailable')
    # Return the media playlist we actually checked, not an unverified master
    # whose other renditions may use different (and unapproved) CDN hosts.
    hosts = {urlsplit(final).hostname}
    resolver = PublicHostResolver(max_hosts=32)
    for line in document.splitlines():
        resources = re.findall(r'URI="([^"\r\n]+)"', line) if line.startswith('#') else [line.strip()]
        for resource in filter(None, resources):
            hosts.add(urlsplit(_public(urljoin(final, resource), resolver=resolver)).hostname)
    segment = next(line.strip() for line in document.splitlines() if line.strip() and not line.startswith('#'))
    segment = _public(urljoin(final, segment))
    hosts.add(urlsplit(segment).hostname)
    response = session.get(segment, headers={**headers, 'Range': 'bytes=0-15'},
        timeout=min(10, max(0.1, deadline - time.monotonic())), allow_redirects=False, stream=True)
    try:
        if response.status_code not in {200, 206}:
            raise ValueError('SupJav first segment unavailable')
        next(response.iter_content(chunk_size=16))
    finally:
        response.close()
    return ProviderManifest('supjav', final, headers, page_url, allowed_media_hosts=tuple(sorted(hosts)))


def capture(page_url: str) -> ProviderManifest:
    document = _bounded_page(_session(), page_url, 40, allowed_hosts={urlsplit(page_url).hostname})
    tokens = {}
    for anchor in BeautifulSoup(document, 'html.parser').select('a.btn-server[data-link]'):
        name, token = anchor.get_text(strip=True).upper(), str(anchor.get('data-link', ''))
        if name in {'VAS', 'EVS', 'FST', 'LUC', 'VOE'} and re.fullmatch(r'[a-zA-Z0-9]{32,512}', token):
            tokens.setdefault(name, token)
    deadline = time.monotonic() + 35
    for name in ['VAS', 'EVS', 'FST', 'LUC', 'VOE']:
        if name not in tokens or time.monotonic() >= deadline:
            continue
        try:
            return _line(name, tokens[name], page_url, min(deadline, time.monotonic() + 12))
        except Exception:
            continue
    raise WebDownloadProviderError('SupJav has no playable HLS line', code='upstream_unavailable')
