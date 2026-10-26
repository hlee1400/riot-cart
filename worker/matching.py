"""Text normalization, title/option matching and price/quantity parsing."""
from __future__ import annotations

import re
import unicodedata
from typing import Literal

from worker.models import Price

_DASHES = re.compile(r"[‐-―−﹘﹣－-]+")
_MARKS = re.compile(r"[™®©]")
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    # Strip marks first: NFKC would turn ™ into "TM".
    t = unicodedata.normalize("NFKC", _MARKS.sub("", text))
    t = _DASHES.sub(" - ", t)
    return _SPACE.sub(" ", t).casefold().strip()


def _tokens(text: str) -> set[str]:
    return {w for w in re.split(r"[^\w']+", normalize(text)) if w}


def match_title(query: str, titles: list[str]) -> tuple[Literal["one", "none", "many"], list[int]]:
    q = normalize(query)
    exact = [i for i, t in enumerate(titles) if normalize(t) == q]
    if len(exact) == 1:
        return "one", exact
    if exact:
        return "many", exact
    qt = _tokens(query)
    scored = [(len(qt & _tokens(t)), i) for i, t in enumerate(titles)]
    ranked = [i for s, i in sorted(scored, key=lambda x: (-x[0], x[1])) if s > 0]
    return "none", ranked[:5]


_UNAVAILABLE = re.compile(
    r"\s*(?:[-–—:]\s*)?[\(\[]?\s*(sold\s*out|out\s+of\s+stock|unavailable)\s*[\)\]]?\s*$", re.I)


def strip_availability(label: str) -> tuple[str, bool]:
    m = _UNAVAILABLE.search(label)
    if not m:
        return label.strip(), False
    return label[: m.start()].strip(), True


SIZE_ALIASES: dict[str, str] = {
    "extra small": "xs", "x-small": "xs", "x small": "xs",
    "small": "s", "medium": "m", "large": "l",
    "extra large": "xl", "x-large": "xl", "x large": "xl",
    "double extra large": "xxl", "2x large": "xxl", "2xl": "xxl", "xx-large": "xxl", "xx large": "xxl",
    "3xl": "xxxl", "triple extra large": "xxxl",
}


def _canon(label: str) -> str:
    n = normalize(strip_availability(label)[0])
    n = n.replace(" - ", "-")
    return SIZE_ALIASES.get(n, n)


def match_option(requested: str, labels: list[str]) -> int | None:
    want = _canon(requested)
    for i, label in enumerate(labels):
        if _canon(label) == want:
            return i
    return None


_PRICE = re.compile(r"(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
_CURRENCY = re.compile(r"\b(USD|CAD|EUR|GBP|AUD)\b", re.I)


def parse_price(text: str) -> Price | None:
    m = _PRICE.search(text or "")
    if not m:
        return None
    whole = m.group(1).replace(",", "")
    cents = (m.group(2) or "00").ljust(2, "0")
    cur = _CURRENCY.search(text)
    currency = cur.group(1).upper() if cur else ("USD" if "$" in text else "USD")
    return Price(amount=f"{whole}.{cents}", currency=currency)


def parse_quantity(text: str) -> int | None:
    m = re.search(r"\d+", text or "")
    return int(m.group()) if m else None
