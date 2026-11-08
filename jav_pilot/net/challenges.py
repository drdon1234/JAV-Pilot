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
    # Script-only verification shells have no page content or challenge title.
    # Do not classify the passive Cloudflare script on normal pages as a gate.
    if (not title and not re.search(r"<(?:h1|main|article)\b", html, re.I)
            and re.search(r"function\s+_0x[0-9a-f]+\b", html, re.I)
            and '/cdn-cgi/challenge-platform/' in html):
        return True
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
            not trusted_verification_origin(parsed.scheme, parsed.hostname, parsed.port or 443)
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
    return _closed_verification_checkbox(page, deadline)


def _closed_verification_checkbox(page: object, deadline: float) -> bool:
    """Find the actual trusted iframe through closed roots; never click a spinner."""
    cdp = None
    try:
        if time.monotonic() >= deadline or not is_challenge(page.content()):
            return False
        expected = page.url
        cdp = page.context.new_cdp_session(page)
        tree = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
        candidates, pending, budget = [], [tree.get("root", {})], 20000
        while pending and budget:
            budget -= 1
            node = pending.pop()
            attributes = node.get("attributes", [])
            attrs = dict(zip(attributes[::2], attributes[1::2]))
            if node.get("nodeName") == "IFRAME":
                parsed = urlsplit(attrs.get("src", ""))
                if (trusted_verification_origin(parsed.scheme, parsed.hostname, parsed.port or 443)
                        and not parsed.username and not parsed.password):
                    candidates.append(node["nodeId"])
            pending.extend(node.get("children", []))
            pending.extend(node.get("shadowRoots", []))
            if node.get("contentDocument"):
                pending.append(node["contentDocument"])
        if pending or len(candidates) != 1 or time.monotonic() >= deadline:
            return False
        obj = cdp.send("DOM.resolveNode", {"nodeId": candidates[0]})["object"]["objectId"]

        def point():
            result = cdp.send("Runtime.callFunctionOn", {
                "objectId": obj, "returnByValue": True,
                "arguments": [{"value": expected}],
                "functionDeclaration": """function(expected) {
                  if (!this.isConnected || location.href !== expected || document.readyState !== 'complete') return null;
                  const r=this.getBoundingClientRect(), x=r.x+28, y=r.y+r.height/2;
                  if(r.width<290 || r.width>310 || r.height<60 || r.height>85 || r.x<0 || r.y<0 || r.right>innerWidth || r.bottom>innerHeight) return null;
                  let n=this;
                  for(let depth=0;depth<64;depth++) {
                    const s=getComputedStyle(n), root=n.getRootNode();
                    if(s.visibility!=='visible'||s.display==='none'||Number(s.opacity)===0||root.elementFromPoint(x,y)!==n) return null;
                    if(root===document) return {x,y};
                    n=root.host;if(!n)return null;
                  }
                  return null;
                }"""})
            return result.get("result", {}).get("value")

        target = point()
        if not target:
            return False
        cdp.send("Input.dispatchMouseEvent", {"type": "mouseMoved", **target})
        if target != point() or time.monotonic() >= deadline:
            return False
        cdp.send("Input.dispatchMouseEvent", {"type": "mousePressed", "button": "left", "clickCount": 1, **target})
        cdp.send("Input.dispatchMouseEvent", {"type": "mouseReleased", "button": "left", "clickCount": 1, **target})
        return True
    except Exception:
        return False
    finally:
        if cdp:
            try:
                cdp.detach()
            except Exception:
                pass  # Verification may navigate and destroy the target first.


def trusted_verification_origin(scheme: str, host: str | None, port: int) -> bool:
    return scheme == 'https' and port == 443 and bool(host) and (
        host == 'challenges.cloudflare.com' or host.endswith('.challenges.cloudflare.com')
    )
