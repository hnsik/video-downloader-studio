"""Определение платформы по ссылке и платформенные настройки."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

PLATFORMS: dict[str, str] = {
    "youtube": "YouTube / Shorts",
    "instagram": "Instagram / Reels",
    "tiktok": "TikTok",
    "facebook": "Facebook / Reels",
    "twitter": "X / Twitter",
    "reddit": "Reddit",
    "other": "Другие сайты",
}

_HOSTS: dict[str, tuple[str, ...]] = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "instagram": ("instagram.com", "instagr.am"),
    "tiktok": ("tiktok.com",),
    "facebook": ("facebook.com", "fb.watch", "fb.com"),
    "twitter": ("twitter.com", "x.com", "t.co"),
    "reddit": ("reddit.com", "redd.it"),
}

# Домены, для которых имеет смысл проверять cookies при «Проверить авторизацию».
AUTH_TEST_URL: dict[str, str] = {
    "youtube": "https://www.youtube.com/",
    "instagram": "https://www.instagram.com/",
    "tiktok": "https://www.tiktok.com/",
    "facebook": "https://www.facebook.com/",
    "twitter": "https://x.com/",
    "reddit": "https://www.reddit.com/",
}

# Имена сессионных cookies (значения никогда не читаются и не выводятся).
SESSION_COOKIES: dict[str, tuple[str, ...]] = {
    "youtube": ("SAPISID", "__Secure-3PSID", "__Secure-1PSID", "SID", "LOGIN_INFO"),
    "instagram": ("sessionid",),
    "tiktok": ("sessionid", "sessionid_ss", "sid_tt", "sid_guard"),
    "facebook": ("c_user", "xs"),
    "twitter": ("auth_token",),
    "reddit": ("reddit_session", "token_v2"),
}

COOKIE_DOMAINS: dict[str, tuple[str, ...]] = {
    "youtube": ("youtube.com", "google.com"),
    "instagram": ("instagram.com",),
    "tiktok": ("tiktok.com",),
    "facebook": ("facebook.com",),
    "twitter": ("x.com", "twitter.com"),
    "reddit": ("reddit.com",),
}

# Настройки по умолчанию для каждой платформы.
DEFAULT_PLATFORM_OPTIONS: dict[str, dict] = {
    "youtube": {"impersonate": ""},
    "instagram": {"impersonate": ""},
    "tiktok": {"impersonate": "chrome"},
    "facebook": {"impersonate": ""},
    "twitter": {"impersonate": ""},
    "reddit": {"impersonate": ""},
    "other": {"impersonate": ""},
}


def detect_platform(url: str) -> str:
    host = (urlsplit(url.strip()).hostname or "").lower()
    for key, domains in _HOSTS.items():
        for domain in domains:
            if host == domain or host.endswith("." + domain):
                return key
    return "other"


_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def dedupe_key(url: str) -> str:
    """Нормализованный ключ ссылки для предотвращения повторных загрузок."""
    url = url.strip()
    parts = urlsplit(url)
    host = (parts.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    path = parts.path.rstrip("/")
    if host == "youtu.be":
        vid = path.lstrip("/").split("/")[0]
        if _YT_ID.match(vid):
            return f"youtube:{vid}"
    if host.endswith("youtube.com"):
        query = parse_qs(parts.query)
        if "v" in query and _YT_ID.match(query["v"][0]):
            return f"youtube:{query['v'][0]}"
        match = re.match(r"^/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})", path)
        if match:
            return f"youtube:{match.group(1)}"
    return f"{host}{path}" + (f"?{parts.query}" if parts.query and host not in ("tiktok.com", "instagram.com") else "")
