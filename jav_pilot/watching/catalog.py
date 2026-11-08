"""Read site catalogs while retaining each edition's actual destination."""
from __future__ import annotations

import re
import hashlib
import hmac
import time
import uuid
from urllib.parse import quote, urljoin, urlsplit

from bs4 import BeautifulSoup

from ..config.settings import load_settings, site_has_capability
from ..core.catalog_code import canonical_catalog_code, normalize_catalog_code
from ..net.challenges import is_challenge
from ..web_download.providers import (
    ProviderManifest, WebDownloadProviderError, _bounded_get, _bounded_page,
    _extract_missav_manifest, _origin, _safe_origin, _session,
)

PROVIDERS = ("jable", "supjav", "missav", "av123")
_RECOMMENDATION_VISITOR = str(uuid.uuid4())
_CODE = re.compile(r"(?<![a-z0-9])(?:fc2[-_ ]*(?:ppv[-_ ]*)?\d{2,9}|[a-z]{2,12}[-_]\d{2,8})(?![a-z0-9])", re.I)
_EDITION_SUFFIX = re.compile(r"(?:-(?:chinese-subtitle|english-subtitle|uncensored(?:-leak(?:ed)?)?|leak))+$", re.I)


def sites() -> list[dict]:
    return [
        {"id": site["id"], "name": site["name"], "enabled": bool(site["enabled"]),
         "base_url": site["base_url"]}
        for site in load_settings()["sites"]
        if site["id"] in PROVIDERS and (site_has_capability(site, "resource_search") or site_has_capability(site, "online_watch"))
    ]


def enabled_site(source: object) -> dict:
    site = next((item for item in sites() if item["id"] == source and item["enabled"]), None)
    if site is None:
        raise ValueError("此在线观看站点未启用，请在站点设置中启用后重试")
    site["base_url"] = _safe_origin(site["base_url"], expected_profile=site["id"])
    return site


def detail_url(source: str, origin: str, value: object) -> str:
    raw = str(value or "")
    if len(raw) > 2048 or any(c in raw for c in "\r\n\0\\"):
        raise ValueError("站点作品地址无效")
    parsed = urlsplit(raw)
    patterns = {
        "jable": r"/videos/[a-z0-9][a-z0-9._-]{1,150}/?",
        "supjav": r"/(?:zh/)?\d+\.html",
        "av123": r"/cn/v/[a-z0-9][a-z0-9_-]{1,120}",
        "missav": r"/(?:dm\d*/)?(?:cn/)?[a-z0-9][a-z0-9._-]{1,150}",
    }
    if (parsed.scheme != "https" or parsed.netloc != urlsplit(origin).netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not re.fullmatch(patterns[source], parsed.path, re.I)):
        raise ValueError("作品地址不属于当前站点或地址格式不受支持")
    if source == "missav" and not identity(parsed.path.rsplit("/", 1)[-1])[0]:
        raise ValueError("作品地址没有可识别的番号")
    return raw


def identity(value: str) -> tuple[str, list[str]]:
    text = value.lower()
    editions = []
    if re.search(r"chinese[-_ ]subtitle|中文字幕|中字", text):
        editions.append("中文字幕")
    if re.search(r"uncensored[-_ ]leak|无码流出|無碼流出|流出", text):
        editions.append("无码流出")
    elif re.search(r"uncensored|无码|無碼", text):
        editions.append("无码")
    if re.search(r"english[-_ ]subtitle|英文字幕", text):
        editions.append("英文字幕")
    matches = _CODE.findall(_EDITION_SUFFIX.sub("", value))
    normalized = normalize_catalog_code(matches[0]) if matches else None
    return (normalized[0] if normalized else "", editions or ["原片"])


def parse_catalog(source: str, origin: str, document: str, query: str = "", page: int = 1) -> dict:
    if is_challenge(document):
        raise WebDownloadProviderError("站点验证尚未完成", code="challenge_active")
    soup = BeautifulSoup(document, "html.parser")
    selectors = {"jable": ".video-img-box", "supjav": ".post", "missav": ".thumbnail", "av123": ".card"}
    cards = soup.select(selectors[source])
    entries, seen = [], set()
    valid_cards = 0
    exact = canonical_catalog_code(query)
    for card in cards:
        anchors = card.select(".detail a[href], h3 a[href], a.text-secondary[href], a[href]")
        for anchor in anchors:
            target = urljoin(origin + "/", str(anchor.get("href") or ""))
            try:
                target = detail_url(source, origin, target)
            except ValueError:
                continue
            if target in seen:
                continue
            image = card.select_one("img")
            title = str(anchor.get("title") or anchor.get_text(" ", strip=True)
                        or (image.get("alt") if image else "") or "")[:500]
            title_anchor = card.select_one(".detail a, h3 a, a.text-secondary, .card__title a")
            if title_anchor is not None:
                title = title_anchor.get_text(" ", strip=True)[:500] or title
            slug = urlsplit(target).path.rstrip("/").rsplit("/", 1)[-1]
            code, editions = identity(f"{slug} {title} {card.get_text(' ', strip=True)}")
            valid_cards += 1
            if exact and canonical_catalog_code(code) != exact:
                continue
            cover = str((image.get("data-original") or image.get("data-src") or image.get("data-lazy-src") or image.get("src") or "") if image else "")
            cover = urljoin(origin, cover) if cover else ""
            if urlsplit(cover).scheme != "https":
                cover = ""
            seen.add(target)
            entries.append({"source": source, "url": target, "code": code,
                            "title": title or code, "editions": editions, "cover": cover})
            break
    empty = soup.select_one(".no-results, .no-result, .search-no-results, h5.inactive-color")
    if not cards and empty is None and not re.search(
        r"no results|nothing found|暂无|暫無|没有找到|沒有找到|找不到", soup.get_text(" "), re.I
    ):
        raise WebDownloadProviderError("站点页面结构已变化，请稍后重试", code="response_invalid")
    if cards and not valid_cards:
        raise WebDownloadProviderError("站点作品暂时无法解析", code="response_invalid")
    has_more = bool(soup.select_one(
        '.pagination a.next, .pagination .next a, a[rel="next"], a[aria-label="Next"]'
    )) or any(a.get_text(strip=True).isdigit() and int(a.get_text(strip=True)) > page
              for a in soup.select(".pagination a[href]"))
    return {"items": entries[:48], "has_more": has_more, "source": source}


def browse(source: str, query: str, page: int) -> dict:
    site = enabled_site(source)
    origin = site["base_url"]
    encoded = quote(query, safe="")
    if source == "jable":
        path = (f"/search/{encoded}/" if query else "/latest-updates/") + (f"{page}/" if page > 1 else "")
    elif source == "supjav":
        path = ("/" if page == 1 else f"/page/{page}/") + (f"?s={encoded}" if query else "")
    elif source == "av123":
        path = (f"/cn/search?keyword={encoded}&sort=relevant&" if query else "/cn/recent?") + f"page={page}"
    else:
        path = (f"/cn/search/{encoded}" if query else "/cn/new") + f"?page={page}"
    document = _bounded_page(_session(), origin + path, 40, allowed_hosts={urlsplit(origin).hostname})
    return {**parse_catalog(source, origin, document, query, page), "page": page}


def capture(source: str, url: str) -> ProviderManifest:
    site = enabled_site(source)
    origin = site["base_url"]
    url = detail_url(source, origin, url)
    if source == "supjav":
        from .supjav import capture as capture_supjav
        return capture_supjav(url)
    session = _session()
    document = _bounded_page(session, url, 40, allowed_hosts={urlsplit(origin).hostname})
    if source == "av123":
        from .av123 import capture as capture_av123
        return capture_av123(session, document, url)
    if source == "missav":
        manifest = _extract_missav_manifest(document)
    else:
        match = re.search(r"\bhlsUrl\s*=\s*['\"](https://[^'\"\s]+)['\"]", document)
        if match is None:
            raise WebDownloadProviderError("此作品暂无可用播放线路", code="not_found")
        manifest = match.group(1)
    # The common media transport enforces provider CDN allowlists and public DNS.
    from ..net.network_guard import PublicHostResolver
    from ..web_download.media import media_provider_scope, require_allowed_media_url
    with media_provider_scope(source):
        require_allowed_media_url(manifest, resolver=PublicHostResolver(max_hosts=1))
    headers = {"Referer": url, "Origin": _origin(url)}
    master = _bounded_get(session, manifest, 20, allowed_hosts={urlsplit(manifest).hostname}, headers=headers)
    if not master.lstrip().startswith("#EXTM3U"):
        raise WebDownloadProviderError("播放清单无效", code="manifest_invalid")
    return ProviderManifest(source, manifest, headers, url)


def describe(source: str, url: str) -> dict:
    site = enabled_site(source)
    origin = site["base_url"]
    url = detail_url(source, origin, url)
    document = _bounded_page(_session(), url, 40, allowed_hosts={urlsplit(origin).hostname})
    soup = BeautifulSoup(document, "html.parser")
    meta = soup.select_one('meta[property="og:title"]')
    heading = soup.select_one(".video-info h4, .archive-title h1, h1")
    title = str((meta.get("content") if meta else heading.get_text(" ", strip=True) if heading else "") or "")[:500]
    if not title:
        raise WebDownloadProviderError("站点作品暂时无法解析", code="response_invalid")
    image = soup.select_one('meta[property="og:image"]')
    cover = urljoin(origin, str(image.get("content") or "")) if image else ""
    if source == "av123":
        from .av123 import poster
        cover = poster(document)
    if not cover:
        image = soup.select_one("img.img, .video-info img, .post-content img")
        if image:
            cover = urljoin(origin, str(image.get("data-original") or image.get("data-src") or image.get("src") or ""))
    labels = " ".join(item.get_text(" ", strip=True) for item in soup.select(".post-meta .cats, .video-info .tags"))
    code, editions = identity(urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] + " " + title + " " + labels)
    description = soup.select_one(".video-info .desc, .watch__desc-text, .video-description")
    try:
        recommendations = [entry for entry in parse_catalog(source, origin, document)["items"] if entry["url"].rstrip('/') != url.rstrip('/')]
    except WebDownloadProviderError:
        recommendations = []
    if source == "av123":
        from .av123 import related
        recommendations = related(document, url) or recommendations
    return {"source": source, "url": url, "title": title, "code": code,
            "recommendations": recommendations,
            "cover": cover if urlsplit(cover).scheme == "https" else "", "editions": editions,
            "description": description.get_text(" ", strip=True)[:3000] if description else ""}


def recommendations(source: str, url: str) -> list[dict]:
    site = enabled_site(source)
    origin = site['base_url']
    url = detail_url(source, origin, url)
    if source != 'missav':
        return describe(source, url)['recommendations']
    session = _session()
    document = _bounded_page(session, url, 15, allowed_hosts={urlsplit(origin).hostname})
    soup = BeautifulSoup(document, 'html.parser')
    script = next((urljoin(origin, str(tag['src'])) for tag in soup.select('script[src]')
                   if urlsplit(urljoin(origin, str(tag['src']))).netloc == urlsplit(origin).netloc
                   and re.fullmatch(r'/build/assets/app[.\w-]+\.js', urlsplit(str(tag['src'])).path)), None)
    if not script:
        raise WebDownloadProviderError('Recommendation client not found', code='response_invalid')
    client = _bounded_get(session, script, 10, allowed_hosts={urlsplit(origin).hostname})
    match = re.search(r'new [\w.]+\.ApiClient\("([\w-]+)","([\w]+)",\{baseUri:"(client-rapi-missav\.recombee\.com)"', client)
    if not match:
        raise WebDownloadProviderError('Recommendation client changed', code='response_invalid')
    database, public_token, host = match.groups()
    from ..net.network_guard import PublicHostResolver
    if not PublicHostResolver(max_hosts=1).is_public(host):
        raise ValueError('推荐服务地址无效')
    slug = urlsplit(url).path.rstrip('/').rsplit('/', 1)[-1]
    path = f'/{database}/recomms/items/{quote(slug, safe="")}/items/?frontend_timestamp={int(time.time())}'
    signature = hmac.new(public_token.encode(), path.encode(), hashlib.sha1).hexdigest()
    response = session.post('https://' + host + path + '&frontend_sign=' + signature,
        json={'targetUserId': _RECOMMENDATION_VISITOR, 'cascadeCreate': True, 'count': 16,
              'scenario': 'desktop-watch-next-bottom', 'returnProperties': True,
              'includedProperties': ['title_zh', 'has_chinese_subtitle', 'has_english_subtitle', 'is_uncensored_leak']},
        headers={'Origin': origin, 'Referer': url}, timeout=12, allow_redirects=False)
    if response.status_code != 200 or len(response.content) > 1024 * 1024:
        raise WebDownloadProviderError('Recommendation service unavailable', code='upstream_unavailable')
    image = soup.select_one('meta[property="og:image"]')
    image_origin = _origin(str(image.get('content') or '')) if image else ''
    entries = []
    for item in response.json().get('recomms', [])[:16]:
        target_slug = str(item.get('id') or '')
        try:
            target = detail_url(source, origin, origin + '/cn/' + target_slug)
        except ValueError:
            continue
        values = item.get('values') or {}
        code, editions = identity(target_slug)
        if values.get('has_chinese_subtitle') and '中文字幕' not in editions:
            editions = ['中文字幕'] + [value for value in editions if value != '原片']
        entries.append({'source': source, 'url': target, 'code': code,
                        'title': str(values.get('title_zh') or code)[:500], 'editions': editions,
                        'cover': image_origin + '/' + target_slug + '/cover-t.jpg' if image_origin.startswith('https://') else ''})
    return entries
