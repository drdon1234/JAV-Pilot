from __future__ import annotations

import hashlib
import json
from http.cookies import SimpleCookie
from urllib.parse import parse_qs, urlsplit

from ..security.auth import AuthConfig, SESSION_COOKIE, make_session_cookie
from ..config.schema import SETTINGS_SCHEMA_VERSION


_PAGES = (
    ("/downloads", "下载任务"),
    ("/library", "媒体库"),
    ("/metadata", "元数据补全"),
    ("/history", "历史治理"),
)
_FORBIDDEN_POST_PATHS = frozenset(
    {
        "/api/download",
        "/api/downloads",
        "/api/downloads/import",
        "/api/detail-prefetch/batches",
        "/api/detail-prefetch/batches/action",
        "/api/web-downloads",
        "/api/web-downloads/batches/action",
    }
)


def _detail_fixture(*, with_magnets: bool) -> dict[str, object]:
    magnets: list[dict[str, object]] = []
    parse_status = "failed"
    error = "fixture_parse_failed"
    if with_magnets:
        info_hash = "a" * 40
        magnets = [
            {
                "info_hash": info_hash,
                "display_name": "ACPT-001 acceptance fixture",
                "size_bytes": 1_073_741_824,
                "size_is_exact": True,
                "source_refs": [
                    {
                        "source_id": "javbus",
                        "uri": f"magnet:?xt=urn:btih:{info_hash}",
                        "display_name": "ACPT-001 acceptance fixture",
                        "reported_size_text": "1 GiB",
                        "reported_size_bytes": 1_073_741_824,
                        "badges": [],
                        "trackers": [],
                    }
                ],
            }
        ]
        parse_status = "resolved"
        error = None
    return {
        "work": {
            "work_id": "code:ACPT001",
            "canonical_code": "ACPT001",
            "code": "ACPT-001",
            "title": "ACPT-001 acceptance fixture",
            "release_date": "2026-01-01",
            "release_date_conflict": False,
            "actors": [],
            "tags": [],
            "magnet_hint": "available" if with_magnets else "unavailable",
            "cover": None,
            "sources": [
                {
                    "source_id": "javbus",
                    "title": "ACPT-001 acceptance fixture",
                    "detail_url": None,
                    "raw_code": "ACPT-001",
                    "release_date": "2026-01-01",
                    "images": [],
                    "magnet_hint": ("available" if with_magnets else "unavailable"),
                    "parse_status": parse_status,
                    "error": error,
                }
            ],
            "magnets": magnets,
        },
        "cached": False,
    }


def _empty_web_downloads() -> dict[str, object]:
    return {
        "ok": True,
        "configured": True,
        "enabled": True,
        "available": True,
        "reason": None,
        "tasks": [],
        "max_concurrency": 8,
        "count": 0,
        "offset": 0,
        "limit": 50,
        "has_more": False,
        "summary": {
            "total": 0,
            "running": 0,
            "queued": 0,
            "completed": 0,
            "missing": 0,
            "failed": 0,
            "speed": 0,
        },
    }


def _resource_search_settings(origin: str) -> dict[str, object]:
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "detail_default_site_id": "javdb",
        "sites": [
            {
                "id": "javbus",
                "name": "Acceptance Metadata",
                "capabilities": ["metadata_search"],
                "enabled": True,
                "base_url": origin,
                "parser_profile": "javbus",
                "search": {"url_template": f"{origin}/fixture?q={{query}}"},
                "filters": [],
            },
            {
                "id": "missav",
                "name": "Acceptance Resources",
                "capabilities": [
                    "resource_search",
                    "web_download",
                    "description",
                ],
                "enabled": True,
                "base_url": origin,
                "parser_profile": "missav",
            },
        ],
        "organizer": {"enabled": True, "mode": "qbittorrent", "rules": []},
    }


def _resource_search_fixture(
    *,
    status: str,
    item_count: int,
    result_limit: int,
    revision: int,
    limit: int = 20,
    offset: int = 0,
) -> dict[str, object]:
    all_items = [
        {
            "item_id": hashlib.sha256(f"acceptance:{number}".encode()).hexdigest()[:32],
            "code": f"DEMO-{number:03d}",
            "title": (
                None if number % 5 == 0 else f"Acceptance resource title {number:03d}"
            ),
            "available_variants": ["original"],
            "source_ids": ["missav"],
        }
        for number in range(1, item_count + 1)
    ]
    terminal = status in {"limit_reached", "completed", "failed", "cancelled"}
    next_page = None if status == "completed" else max(2, item_count // 8 + 1)
    return {
        "session_id": "b" * 32,
        "source_id": "all",
        "source_ids": ["missav"],
        "sources": [
            {
                "source_id": "missav",
                "status": status,
                "item_count": item_count,
                "error_code": None,
                "retryable": False,
            }
        ],
        "query": "DEMO",
        "result_limit": result_limit,
        "suffix_width": None,
        "start": None,
        "end": None,
        "status": status,
        "revision": revision,
        "item_count": item_count,
        "error_code": None,
        "retryable": False,
        "created_at": 1.0,
        "updated_at": 2.0,
        "started_at": 1.0,
        "heartbeat_at": 2.0,
        "finished_at": 2.0 if terminal else None,
        "items": all_items[offset : offset + limit],
        "pagination": {
            "limit": limit,
            "offset": offset,
            "total": item_count,
            "has_more": offset + limit < item_count,
            "keyword": None,
            "variant": None,
        },
        "progress": {
            "percent": 100 if terminal else 50,
            "items_found": item_count,
            "result_limit": result_limit,
            "scanned_pages": max(1, item_count // 8),
            "total_pages": None,
            "next_page": next_page,
            "pending_total": 0,
            "pending_cursor": 0,
            "pending_remaining": 0,
            "determinate": False,
        },
        "can_continue": status in {"limit_reached", "cancelled"}
        and result_limit < 999
        and next_page is not None,
        "can_retry": False,
        "can_cancel": status in {"queued", "running"},
        "can_remove": terminal,
    }


class BrowserAcceptanceError(RuntimeError):
    pass


def _resource_search_next_page_button(page):  # noqa: ANN001, ANN202
    return page.get_by_role(
        "navigation",
        name="资源结果顶部分页",
        exact=True,
    ).get_by_role("button", name="下一页", exact=True)


def _resource_search_mode_link(page):  # noqa: ANN001, ANN202
    return page.get_by_role("link", name="Web 视频资源", exact=False)


def run_browser_acceptance(base_url: str) -> dict[str, object]:
    origin = _loopback_origin(base_url)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserAcceptanceError("browser runtime is unavailable") from exc

    visited: list[str] = []
    browser_errors = 0
    download_posts = 0
    detail_cases: dict[str, bool] = {}
    resource_search_cases: dict[str, bool] = {}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                viewport={"width": 1440, "height": 960},
                locale="zh-CN",
                reduced_motion="reduce",
            )
            config = AuthConfig.from_env()
            if config.enabled:
                if not config.configured or not config.secret_persistent:
                    raise BrowserAcceptanceError("authentication is not ready")
                cookie = SimpleCookie()
                cookie.load(make_session_cookie(config))
                session = cookie.get(SESSION_COOKIE)
                if session is None:
                    raise BrowserAcceptanceError("acceptance session is unavailable")
                context.add_cookies(
                    [
                        {
                            "name": SESSION_COOKIE,
                            "value": session.value,
                            "url": origin,
                            "httpOnly": True,
                            "sameSite": "Lax",
                        }
                    ]
                )
            page = context.new_page()

            def record_console(message) -> None:
                nonlocal browser_errors
                if message.type == "error":
                    browser_errors += 1

            def record_page_error(_error) -> None:
                nonlocal browser_errors
                browser_errors += 1

            def record_request(request) -> None:
                nonlocal download_posts
                if request.method != "POST":
                    return
                path = urlsplit(request.url).path
                if path in _FORBIDDEN_POST_PATHS:
                    download_posts += 1

            page.on("console", record_console)
            page.on("pageerror", record_page_error)
            page.on("request", record_request)
            for path, heading in _PAGES:
                response = page.goto(
                    f"{origin}{path}",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                if response is None or response.status != 200:
                    raise BrowserAcceptanceError("application page is unavailable")
                page.get_by_role("heading", name=heading, exact=True).wait_for(
                    state="visible", timeout=15_000
                )
                if page.evaluate("window.scrollY") != 0:
                    raise BrowserAcceptanceError("window scroll invariant failed")
                if not page.evaluate(
                    "document.documentElement.scrollWidth <= "
                    "document.documentElement.clientWidth"
                ):
                    raise BrowserAcceptanceError("page overflow invariant failed")
                visited.append(path)

            detail_state = {"with_magnets": True}

            def fulfill_detail(route) -> None:  # noqa: ANN001
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(
                        _detail_fixture(with_magnets=detail_state["with_magnets"]),
                        separators=(",", ":"),
                    ),
                )

            def fulfill_empty_web_downloads(route) -> None:  # noqa: ANN001
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(_empty_web_downloads(), separators=(",", ":")),
                )

            page.route(f"{origin}/api/works?*", fulfill_detail)
            page.route(f"{origin}/api/web-downloads?*", fulfill_empty_web_downloads)

            resource_state = {
                "created": False,
                "continued": False,
                "create_posts": 0,
                "continue_posts": 0,
                "continued_gets": 0,
            }

            def fulfill_settings(route) -> None:  # noqa: ANN001
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(
                        {"settings": _resource_search_settings(origin), "revision": "a" * 64},
                        separators=(",", ":"),
                    ),
                )

            def fulfill_resource_search(route) -> None:  # noqa: ANN001
                parsed = urlsplit(route.request.url)
                method = str(route.request.method).upper()
                if parsed.path == "/api/resource-searches" and method == "POST":
                    payload = route.request.post_data_json
                    if (
                        not isinstance(payload, dict)
                        or payload.get("query") != "DEMO"
                        or payload.get("source_id") != "all"
                    ):
                        raise BrowserAcceptanceError(
                            "resource search request is invalid"
                        )
                    resource_state["created"] = True
                    resource_state["create_posts"] += 1
                    search = _resource_search_fixture(
                        status="queued",
                        item_count=0,
                        result_limit=25,
                        revision=1,
                    )
                    route.fulfill(
                        status=202,
                        content_type="application/json; charset=utf-8",
                        body=json.dumps(
                            {"ok": True, "search": search},
                            separators=(",", ":"),
                        ),
                    )
                    return
                if parsed.path == "/api/resource-searches/action" and method == "POST":
                    payload = route.request.post_data_json
                    if (
                        not isinstance(payload, dict)
                        or payload.get("session_id") != "b" * 32
                        or payload.get("action") != "continue"
                        or payload.get("result_limit") != 75
                    ):
                        raise BrowserAcceptanceError(
                            "resource search continuation is invalid"
                        )
                    resource_state["continued"] = True
                    resource_state["continue_posts"] += 1
                    search = _resource_search_fixture(
                        status="queued",
                        item_count=25,
                        result_limit=75,
                        revision=8,
                    )
                    route.fulfill(
                        status=202,
                        content_type="application/json; charset=utf-8",
                        body=json.dumps(
                            {"ok": True, "search": search},
                            separators=(",", ":"),
                        ),
                    )
                    return
                if parsed.path != "/api/resource-searches" or method != "GET":
                    route.abort()
                    return
                params = parse_qs(parsed.query)
                if params.get("id", [""])[0] != "b" * 32:
                    raise BrowserAcceptanceError("resource search identity changed")
                limit = int(params.get("limit", ["20"])[0])
                offset = int(params.get("offset", ["0"])[0])
                continued = bool(resource_state["continued"])
                if continued:
                    resource_state["continued_gets"] += 1
                search = _resource_search_fixture(
                    status="completed" if continued else "limit_reached",
                    item_count=30 if continued else 25,
                    result_limit=75 if continued else 25,
                    revision=9 if continued else 7,
                    limit=limit,
                    offset=offset,
                )
                route.fulfill(
                    status=200,
                    content_type="application/json; charset=utf-8",
                    body=json.dumps(
                        {"ok": True, "search": search},
                        separators=(",", ":"),
                    ),
                )

            page.route(f"{origin}/api/settings", fulfill_settings)
            page.route(f"{origin}/api/resource-searches**", fulfill_resource_search)
            page.route(
                f"{origin}/api/resource-searches/action", fulfill_resource_search
            )
            response = page.goto(
                f"{origin}/search",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            if response is None or response.status != 200:
                raise BrowserAcceptanceError("resource search page is unavailable")
            _resource_search_mode_link(page).click()
            resource_site = page.get_by_role(
                "combobox",
                name="Web 视频资源站点",
                exact=True,
            )
            resource_site.wait_for(state="visible", timeout=15_000)
            # Settings load independently from the route shell.  Wait for the
            # default provider to be selected before filling the form so the
            # submit button is not sampled during that short initial state.
            for _attempt in range(300):
                if resource_site.locator("option").count() > 0 and resource_site.input_value():
                    break
                page.wait_for_timeout(50)
            else:
                raise BrowserAcceptanceError("resource search providers unavailable")
            page.get_by_role("textbox", name="番号或关键词", exact=True).fill("DEMO")
            result_limit = page.get_by_role("spinbutton", name="总结果上限", exact=True)
            result_limit.fill("25")
            page.get_by_role("button", name="开始搜索", exact=True).click()
            page.get_by_text("DEMO-001", exact=True).wait_for(
                state="visible", timeout=15_000
            )
            if f"resource_id={'b' * 32}" not in page.url:
                raise BrowserAcceptanceError("resource search session was not retained")
            page.get_by_role("combobox", name="每页显示", exact=True).select_option(
                "10"
            )
            page.wait_for_function(
                "() => document.querySelectorAll("
                "'.resource-result-list > li').length === 10",
                timeout=15_000,
            )
            if page.locator(".resource-result-list > li").count() != 10:
                raise BrowserAcceptanceError("resource search pagination is unbounded")
            page.get_by_role(
                "navigation",
                name="资源结果底部分页",
                exact=True,
            ).wait_for(state="visible", timeout=15_000)
            _resource_search_next_page_button(page).click()
            page.get_by_text("DEMO-011", exact=True).wait_for(
                state="visible", timeout=15_000
            )
            continuation_limit = page.get_by_role(
                "spinbutton", name="续搜累计上限", exact=True
            )
            continuation_limit.fill("75")
            page.get_by_role("button", name="继续搜索", exact=True).click()
            for _attempt in range(100):
                if (
                    resource_state["continue_posts"]
                    and resource_state["continued_gets"]
                ):
                    break
                page.wait_for_timeout(50)
            if (
                resource_state["continue_posts"] != 1
                or not resource_state["continued_gets"]
            ):
                raise BrowserAcceptanceError(
                    "resource search continuation did not refresh results"
                )
            _resource_search_next_page_button(page).click()
            page.get_by_text("DEMO-030", exact=True).wait_for(
                state="visible", timeout=15_000
            )
            if f"resource_id={'b' * 32}" not in page.url:
                raise BrowserAcceptanceError(
                    "resource search continuation replaced the session"
                )
            if (
                resource_state["create_posts"] != 1
                or resource_state["continue_posts"] != 1
            ):
                raise BrowserAcceptanceError("resource search actions were duplicated")
            page.set_viewport_size({"width": 390, "height": 844})
            main_content = page.locator(".main-content")
            main_content.evaluate(
                "element => { element.scrollTop = element.scrollHeight; }"
            )
            if int(main_content.evaluate("element => element.scrollTop")) <= 0:
                raise BrowserAcceptanceError("resource search mobile scroll failed")
            if page.evaluate("window.scrollY") != 0:
                raise BrowserAcceptanceError("resource search window scroll escaped")
            if not page.evaluate(
                "document.documentElement.scrollWidth <= "
                "document.documentElement.clientWidth"
            ):
                raise BrowserAcceptanceError(
                    "resource search page overflow invariant failed"
                )
            page.set_viewport_size({"width": 1440, "height": 960})
            resource_search_cases = {
                "pagination": True,
                "continuation": True,
                "mobile_scroll": True,
            }

            detail_url = f"{origin}/works/code:ACPT001?code=ACPT-001&source=javbus"
            for label, with_magnets in (
                ("with_magnets", True),
                ("without_magnets", False),
            ):
                detail_state["with_magnets"] = with_magnets
                response = page.goto(
                    detail_url,
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                if response is None or response.status != 200:
                    raise BrowserAcceptanceError("detail page is unavailable")
                web_download_region = page.locator('[aria-label="Web 视频下载"]')
                web_download_region.wait_for(state="visible", timeout=15_000)
                web_download_region.get_by_text("Web 视频下载", exact=True).wait_for(
                    state="visible", timeout=15_000
                )
                download_button = web_download_region.get_by_role(
                    "button", name="开始 Web 下载", exact=True
                )
                download_button.wait_for(state="visible", timeout=15_000)
                if not download_button.is_enabled():
                    raise BrowserAcceptanceError(
                        "detail Web download requires foreground quality discovery"
                    )
                magnet_rows = page.locator(".work-magnet-row").count()
                if magnet_rows != (1 if with_magnets else 0):
                    raise BrowserAcceptanceError("detail magnet fixture failed")
                if not with_magnets:
                    page.get_by_text("暂未发现磁链", exact=True).wait_for(
                        state="visible", timeout=15_000
                    )
                if page.evaluate("window.scrollY") != 0:
                    raise BrowserAcceptanceError("detail scroll invariant failed")
                if not page.evaluate(
                    "document.documentElement.scrollWidth <= "
                    "document.documentElement.clientWidth"
                ):
                    raise BrowserAcceptanceError("detail overflow invariant failed")
                detail_cases[label] = True
            context.close()
        finally:
            browser.close()
    if browser_errors:
        raise BrowserAcceptanceError("browser errors were detected")
    if download_posts:
        raise BrowserAcceptanceError("browser acceptance attempted a download")
    return {
        "ok": True,
        "pages": visited,
        "detail_cases": detail_cases,
        "resource_search_cases": resource_search_cases,
        "browser_errors": 0,
        "download_posts": 0,
    }


def _loopback_origin(value: object) -> str:
    clean = str(value or "").strip().rstrip("/")
    try:
        parsed = urlsplit(clean)
        port = parsed.port
    except ValueError as exc:
        raise BrowserAcceptanceError("acceptance URL is invalid") from exc
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "localhost"}
        or port is None
        or not 1 <= port <= 65535
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BrowserAcceptanceError("acceptance URL must be a loopback origin")
    return f"http://{parsed.hostname}:{port}"


__all__ = ["BrowserAcceptanceError", "run_browser_acceptance"]
