"""Preference-based job filtering (pure functions, fully unit-tested).

Confirmed user preferences (Phase 0):
- Any SWE role matching the stack (Java, Spring Boot, React, PostgreSQL, AWS,
  REST APIs, microservices); 4+ years experience.
- US: remote + onsite. Europe: remote only.
- Work authorization: US work visa (H1B/L1) → US employers must be sponsorable.
"""
from __future__ import annotations

import re

EU_COUNTRIES = {
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR",
    "HU", "IE", "IT", "LV", "LI", "LT", "LU", "MT", "NL", "NO", "PL", "PT",
    "RO", "SK", "SI", "ES", "SE", "CH", "GB",
}

# City/state-level geos for feeds that only give a city name (e.g. Greenhouse
# "Amsterdam" with no country code). Substring-matched, case-insensitive.
US_PLACE_NAMES = {
    "austin", "new york", "san francisco", "seattle", "chicago", "boston",
    "denver", "los angeles", "mountain view", "sunnyvale", "santa clara",
    "san jose", "portland", "bellevue", "redmond", "cambridge", "washington",
    "texas", "california", "florida", "new jersey", "massachusetts",
    "washington dc", "maryland", "virginia", "north carolina", "georgia",
    "ohio", "michigan", "illinois", "pennsylvania", "oregon", "utah",
    "colorado", "nevada", "arizona", "tampa", "rtp", "raleigh",
}
EU_PLACE_NAMES = {
    "amsterdam", "berlin", "paris", "london", "dublin", "munich", "madrid",
    "barcelona", "brussels", "copenhagen", "stockholm", "oslo", "helsinki",
    "vienna", "zurich", "geneva", "warsaw", "krakow", "kraków", "lisbon",
    "rotterdam", "the hague", "antwerp", "prague", "budapest", "tallinn",
    "riga", "vilnius", "malmö", "gothenburg", "lyon", "milan", "rome",
    "porto", "valencia", "thessaloniki", "sofia", "bucharest", "cluj",
    "bratislava", "ljubljana", "zagreb", "gdańsk", "wrocław", "poznań",
    "luxembourg", "frankfurt", "hamburg", "heidelberg", "aachen", "bremen",
    "nantes", "bordeaux", "toulouse", "lyon", "sevilla", "valencia",
    "brno", "katowice", "gdansk", "bydgoszcz", "poznan", "wroclaw",
    "netherlands", "germany", "france", "ireland", "sweden", "denmark",
    "finland", "spain", "portugal", "belgium", "austria", "switzerland",
    "poland", "czech", "hungary", "romania", "slovakia", "slovenia",
    "croatia", "estonia", "latvia", "lithuania", "luxembourg", "norway",
    "italy", "greece", "bulgaria", "uk", "england", "scotland", "wales",
}


def regions_of(job: dict) -> tuple[bool, bool]:
    countries = [c for c in (job.get("countries") or []) if c]
    location = (job.get("location") or "").lower()
    is_us = (
        "US" in countries
        or "United States" in (job.get("location") or "")
        or re.search(r"\busa\b|\bunited states\b", location)
        is not None
        or any(p in location for p in US_PLACE_NAMES)
    )
    is_eu = any(c in EU_COUNTRIES for c in countries) or any(
        p in location for p in EU_PLACE_NAMES
    )
    return is_us, is_eu


SWE_TITLE_RE = re.compile(
    r"(software (engineer|developer|development|engineers|developers)"
    r"|software engineer"
    r"|full[- ]?stack (engineer|developer)"
    r"|back(?:end|[- ]end) (engineer|developer)"
    r"|\bswe[- ]?(ii|3|3\+)?\b"
    r"|java (engineer|developer)"
    r"|backend"
    r"|platform engineer"
    r"|site reliability"
    r"|devops engineer"
    r"|engineering manager.*software)",
    re.IGNORECASE,
)

STACK_RE = re.compile(
    r"(java|spring boot|spring|react|postgresql|postgres|\bsql\b|\baws\b|kubernetes"
    r"|\bpython\b|\.net|\bc#\b|\bgo\b|microservices|rest ?api|node\.?js|\bts\b|typescript)",
    re.IGNORECASE,
)


def job_passes_filters(job: dict) -> tuple[bool, list[str]]:
    """Return (keep, reasons-dropped-for). Reasons only populated when not kept."""
    reasons: list[str] = []
    is_us, is_eu = regions_of(job)

    if not is_us and not is_eu:
        reasons.append("outside US/Europe")
    if is_eu and job.get("remote") is False:
        reasons.append("EU role is not remote")

    title = job.get("title") or ""
    if not SWE_TITLE_RE.search(title):
        reasons.append("title is not an SWE/software role")

    yoe = job.get("yoe")
    if isinstance(yoe, (int, float)) and yoe < 4:
        reasons.append(f"requires {yoe:g} YOE (below 4+ target)")

    if is_us and job.get("visa_sponsorship") is False:
        reasons.append("explicitly no visa sponsorship (US)")

    return (len(reasons) == 0), reasons


def stack_match_score(job: dict) -> int:
    """0-4: how many distinct stack families the title+meta mention. (Display aid.)"""
    parts = [job.get("title") or "", *(job.get("commitment") or []), job.get("location") or ""]
    text = " ".join(p for p in parts if isinstance(p, str))
    hits = set()
    for fam, rx in (
        ("java/spring", re.compile(r"java|spring", re.I)),
        ("react/frontend", re.compile(r"react|frontend|front[- ]end", re.I)),
        ("data/sql", re.compile(r"sql|postgres|data", re.I)),
        ("aws/cloud", re.compile(r"aws|cloud|kubernetes", re.I)),
    ):
        if rx.search(text):
            hits.add(fam)
    return len(hits)
