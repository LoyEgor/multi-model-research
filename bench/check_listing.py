#!/usr/bin/env python3
"""Live-check a marketplace listing URL: HTTP status, extracted price, ad state.

Used by the effort benchmark (gold verification + run evaluation). Stdlib-only, like the
rest of the project. Not wired into research.py yet — it is the seed of the Stage 2
page-content verification adapters.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.request

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
PRICE_RE = re.compile(r'"price"\s*:\s*"?([0-9]+(?:\.[0-9]+)?)"?')
PRICE_TEXT_RE = re.compile("([0-9][0-9\\s  ]{3,9})\\s*грн")
# OLX embeds the ad state in __PRERENDERED_STATE__ (JSON-escaped). Text markers like
# "більше не доступне" are USELESS here — they sit in the i18n dictionary on every page.
AD_STATUS_RE = re.compile(r'\\?"status\\?"\s*:\s*\\?"([a-z_]+)\\?"')


def check(url: str, timeout: float = 20.0) -> dict:
    result = {"url": url, "status": "error", "http": None, "price": None, "ad_status": None}
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            result["http"] = int(getattr(response, "status", response.getcode()))
            html = response.read(2_500_000).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        result["http"] = exc.code
        result["status"] = "dead" if exc.code in {404, 410} else "error"
        return result
    except Exception as exc:
        result["status"] = f"error:{exc.__class__.__name__}"
        return result

    status_match = AD_STATUS_RE.search(html)
    if status_match:
        result["ad_status"] = status_match.group(1)
        if status_match.group(1) != "active":
            result["status"] = "inactive"
            return result

    match = PRICE_RE.search(html)
    if match:
        result["price"] = float(match.group(1))
    else:
        text_match = PRICE_TEXT_RE.search(html)
        if text_match:
            result["price"] = float(re.sub(r"\D", "", text_match.group(1)))
    result["status"] = "live"
    return result


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(json.dumps(check(arg), ensure_ascii=False))
