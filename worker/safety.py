"""Hard safety rules, enforced in code for recipe steps and model recovery alike."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from worker.recipe import Recipe

FORBIDDEN_LABEL = re.compile(
    r"\b(check\s*out|checkout|place\s+(?:my\s+)?order|pay(?:ment)?|buy\s+now|sign\s*in|sign\s*up|log\s*in"
    r"|create\s+(?:an\s+)?account|apple\s*pay|google\s*pay|shop\s*pay|paypal|purchase|complete\s+order)\b",
    re.I,
)


def is_allowed_url(url: str, recipe: Recipe) -> bool:
    if url == "about:blank":
        return True
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in recipe.allowed_origins:
        return False
    path = parts.path or "/"
    return any(path.startswith(p) for p in recipe.allowed_path_prefixes)


def is_forbidden_label(text: str) -> bool:
    return bool(FORBIDDEN_LABEL.search(text or ""))


def redact(text: str, secrets: list[str | None]) -> str:
    for s in secrets:
        if s:
            text = text.replace(s, "***")
    return re.sub(r"Bearer\s+\S+", "Bearer ***", text)
