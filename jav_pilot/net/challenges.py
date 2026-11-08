"""Recognize blocking documents without mistaking passive scripts for gates."""

from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


def age_gate(html: str, url: str) -> tuple[str, str, str] | None:
    """Known source controls only: gate selector, submit selector, token selector."""
    host = urlsplit(url).hostname
    if host == "fc2db.net" and "/__verify_age__" in html:
        return (
            'form[action="/__verify_age__"]',
            'button.confirm[type="submit"]',
            'input[name="cf-turnstile-response"]',
        )
    if host == "www.mgstage.com" and (
        'id="entry_column"' in html or 'id="entry"' in html
    ):
        return ("#entry_column, body#entry", '#AC, a[href="javascript:goSite();"]', "")
    if host == "adult.contents.fc2.com" and 'data-modal="agecheck"' in html:
        selector = '[data-modal="agecheck"][aria-hidden="false"]'
        if BeautifulSoup(html, "html.parser").select_one(selector):
            return (selector, '[data-button="yes"]', "")
    return None


def try_age_confirmation(
    page: object, gate: tuple[str, str, str], deadline: float
) -> bool:
    try:
        container, submit, token = gate
        form = page.locator(container).first
        if token:
            field = form.locator(token)
            if not field.count() or not field.input_value(
                timeout=max(1, min(500, int((deadline - time.monotonic()) * 1000)))
            ):
                return False
        control = form.locator(submit).first
        if not control.is_visible():
            return False
        control.click(
            timeout=max(1, min(750, int((deadline - time.monotonic()) * 1000)))
        )
        return True
    except Exception:
        return False


def is_firewall_block(html: str) -> bool:
    return "cf-error-details" in html.lower() and bool(
        re.search(
            r"you have been blocked|why have i been blocked|error code:?\s*10(?:0[69]|20)",
            html,
            re.I,
        )
    )


def is_challenge(html: str) -> bool:
    if is_firewall_block(html):
        return False
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return bool(
        title
        and re.search(
            r"just a moment|attention required|verify.*human|请稍候|請稍候",
            title.group(1),
            re.I,
        )
        or re.search(
            r"(?:window\.)?_cf_chl_opt\s*=|id\s*=\s*['\"](?:challenge-form|cf-challenge-running)['\"]",
            html,
            re.I,
        )
    )


def try_verification_checkbox(page: object, deadline: float) -> bool:
    """Interact only with a visible checkbox in the trusted verification frame."""
    if time.monotonic() >= deadline:
        return False
    for frame in page.frames:
        parsed = urlsplit(frame.url)
        if (
            (parsed.scheme, parsed.hostname, parsed.port or 443)
            != (
                "https",
                "challenges.cloudflare.com",
                443,
            )
            or parsed.username
            or parsed.password
        ):
            continue
        for selector in ('input[type="checkbox"]', '[role="checkbox"]'):
            if time.monotonic() >= deadline:
                return False
            try:
                checkbox = frame.locator(selector).first
                if checkbox.count() and checkbox.is_visible():
                    checkbox.click(
                        timeout=max(
                            1, min(750, int((deadline - time.monotonic()) * 1000))
                        )
                    )
                    return True
            except Exception:
                continue
        # Turnstile may hide its input in a closed shadow root. The frame is
        # already origin-checked; restrict the fallback to its checkbox widget.
        try:
            box = frame.frame_element().bounding_box()
            if box and 180 <= box["width"] <= 500 and 40 <= box["height"] <= 180:
                frame.locator("body").click(
                    position={"x": 24, "y": box["height"] / 2},
                    timeout=max(1, min(750, int((deadline - time.monotonic()) * 1000))),
                )
                return True
        except Exception:
            continue
    # Managed challenges can host the checkbox in the top document's closed
    # shadow tree. Only its named challenge stage and compact widget qualify.
    try:
        if is_challenge(page.content()):
            stage = page.locator("#challenge-stage").first
            box = (
                stage.bounding_box(
                    timeout=max(1, min(500, int((deadline - time.monotonic()) * 1000)))
                )
                if stage.count() and stage.is_visible()
                else None
            )
            if box and 180 <= box["width"] <= 1024 and 40 <= box["height"] <= 180:
                stage.click(
                    position={"x": 24, "y": box["height"] / 2},
                    timeout=max(1, min(750, int((deadline - time.monotonic()) * 1000))),
                )
                return True
    except Exception:
        pass
    return False
