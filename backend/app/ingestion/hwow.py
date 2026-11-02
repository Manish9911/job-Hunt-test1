"""hiring-without-whiteboards: fetch + parse the curated company list.

The repo README is a markdown list, not a table:
    - [Adyen](https://www.adyen.com) | Amsterdam, NL | Take-home assignment, ...
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import httpx

HWOW_README_URL = (
    "https://raw.githubusercontent.com/poteto/hiring-without-whiteboards/master/README.md"
)

LINE_RE = re.compile(r"^\s*[-*]\s*\[([^\]]+)\]\((https?://[^)\s]+)\)(?:\s*\|\s*(.*))?$")

# Country -> ISO used for region classification
US_MARKERS = {"USA", "US", "United States"}
US_STATE_CODES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}
EU_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LI", "LT", "LU", "MT", "NL", "NO", "PL", "PT",
    "RO", "SK", "SI", "ES", "SE", "CH", "GB",
}
EU_COUNTRY_NAMES = {
    "Austria", "Belgium", "Bulgaria", "Croatia", "Cyprus", "Czech", "Denmark",
    "Estonia", "Finland", "France", "Germany", "Greece", "Hungary", "Ireland",
    "Italy", "Latvia", "Liechtenstein", "Lithuania", "Luxembourg", "Malta",
    "Netherlands", "Norway", "Poland", "Portugal", "Romania", "Slovakia",
    "Slovenia", "Spain", "Sweden", "Switzerland", "UK", "England", "Scotland",
    "Wales", "Northern Ireland", "Iceland",
}
# Countries outside US/EU — codes that must NOT be mistaken for US state codes
# (DE=Germany vs Delaware, IN=India vs Indiana, CA=Canada vs California, ...)
OTHER_COUNTRY_CODES = {
    "IN", "SG", "AU", "NZ", "JP", "CN", "BR", "MX", "ZA", "KE", "NG", "PH",
    "VN", "TH", "MY", "ID", "TR", "IL", "KR", "CL", "AR", "CO", "PE", "EC",
    "UY", "EG", "AE", "SA", "QA", "BD", "LK", "TZ", "GH",
}
OTHER_COUNTRY_NAMES = {
    "India", "Canada", "Singapore", "Brazil", "Mexico", "Japan", "China",
    "Australia", "New Zealand", "Kenya", "Nigeria", "South Africa", "Israel",
    "Turkey", "Korea", "Argentina", "Chile", "Colombia", "Peru", "UAE",
    "Dubai", "Bangladesh", "Sri Lanka",
}


def fetch_hwow_readme(timeout: float = 30.0) -> str:
    resp = httpx.get(HWOW_README_URL, timeout=timeout, headers={"User-Agent": "jobflow-local"})
    resp.raise_for_status()
    return resp.text


def _classify_region(locations: list[str]) -> str:
    """'us' | 'eu' | 'other' — us wins over eu when both present.

    2-letter code precedence: EU country codes first, then known non-US
    country codes, then US state codes (avoids DE=Germany vs Delaware,
    IN=India vs Indiana collisions).
    """
    has_us = has_eu = False
    for loc in locations:
        low = loc.strip().lower()
        if re.search(r"\b(usa|united states|u\.s\.?)\b", low):
            has_us = True
        for name in EU_COUNTRY_NAMES:
            if name.lower() in low:
                has_eu = True
        m = re.search(r",\s*([A-Z]{2})\b", loc)
        if m:
            code = m.group(1)
            if code in EU_COUNTRIES:
                has_eu = True
            elif code in OTHER_COUNTRY_CODES:
                pass  # known non-US/EU country — no flag
            elif code in US_MARKERS or code in US_STATE_CODES:
                has_us = True
    if has_us:
        return "us"
    if has_eu:
        return "eu"
    return "other"


def parse_hwow_readme(text: str) -> list[dict]:
    """Parse README markdown into company dicts. Pure function (testable offline)."""
    companies: list[dict] = []
    seen: set[str] = set()
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            continue
        name, url, rest = m.group(1), m.group(2), m.group(3) or ""
        parts = [p.strip() for p in rest.split("|")]
        locations_raw = parts[0] if parts else ""
        notes = parts[1] if len(parts) > 1 else ""
        locations = [
            loc.strip()
            for loc in re.split(r"\s*/\s*|\s*;\s*,?\s*", locations_raw)
            if loc.strip()
        ]
        is_remote = any(re.search(r"\bremote\b", loc, re.IGNORECASE) for loc in locations)
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        companies.append(
            {
                "name": name.strip(),
                "website": url,
                "locations": locations,
                "remote": is_remote,
                "region": _classify_region(locations),
                "notes": notes,
            }
        )
    return companies


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower()
    return host[4:] if host.startswith("www.") else host
