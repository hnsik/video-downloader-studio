"""Очистка текста: удаление ANSI-последовательностей и скрытие чувствительных данных.

Cookies, токены, пароли и подписанные URL не должны попадать ни в журнал,
ни в диагностический отчёт.
"""
from __future__ import annotations

import re
from typing import Any

MASK = "<скрыто>"

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

_SENSITIVE_PARAMS = (
    "sig|signature|lsig|token|access_token|refresh_token|id_token|auth|authorization|key|apikey|api_key|"
    "policy|hdnts|hmac|password|passwd|secret|session|sessionid|sid|code|"
    "x-amz-[a-z0-9-]+|x-goog-[a-z0-9-]+|key-pair-id|signed|sparams_sig|ei_sig|oauth_[a-z_]+"
)
_URL_PARAM = re.compile(rf"([?&;](?:{_SENSITIVE_PARAMS})=)[^&\s\"'<>]+", re.IGNORECASE)
_HEADER = re.compile(r"(?im)^(\s*(?:set-)?cookie|\s*authorization|\s*proxy-authorization|\s*x-[a-z-]*token)\s*:\s*.*$")
_KV = re.compile(
    r"(?i)\b(cookie|cookies|authorization|token|access_token|password|passwd|secret|api[_-]?key|sessionid|sapisid|"
    r"po[_-]?token|visitor[_-]?data|data[_-]?sync[_-]?id)\b(\s*[:=]\s*)(?!<скрыто>)[^\s,;\"']+"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_NETSCAPE = re.compile(r"(?m)^((?:#HttpOnly_)?[^\t\n#][^\t\n]*\t(?:TRUE|FALSE)\t[^\t\n]*\t(?:TRUE|FALSE)\t\d+\t[^\t\n]*\t)[^\t\n]*$")
_USER_PATH = re.compile(r"(?i)([A-Z]:\\Users\\)[^\\/\s\"']+")
_POSIX_HOME = re.compile(r"(/home/|/Users/)[^/\s\"']+")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{64,}\b")


def strip_ansi(text: str) -> str:
    return _CTRL.sub("", _ANSI.sub("", text))


def redact(text: str, *, hide_user_path: bool = True) -> str:
    """Скрывает cookies, токены, пароли, подписи в URL и длинные ключи."""
    if not text:
        return text
    text = _HEADER.sub(lambda m: f"{m.group(1)}: {MASK}", text)
    text = _NETSCAPE.sub(lambda m: m.group(1) + MASK, text)
    text = _URL_PARAM.sub(lambda m: m.group(1) + MASK, text)
    text = _KV.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}", text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _JWT.sub(MASK, text)
    # Длинные ключи скрываем, но не хэши (чисто шестнадцатеричные строки, SHA-256 и т.п.).
    text = _LONG_TOKEN.sub(lambda m: m.group(0) if re.fullmatch(r"[0-9a-fA-F]+", m.group(0)) else MASK, text)
    if hide_user_path:
        text = _USER_PATH.sub(lambda m: m.group(1) + "<user>", text)
        text = _POSIX_HOME.sub(lambda m: m.group(1) + "<user>", text)
    return text


def clean(text: str) -> str:
    """ANSI-символы удаляются, чувствительные данные скрываются."""
    return redact(strip_ansi(text))


def redact_obj(value: Any) -> Any:
    """Рекурсивная очистка структур (для JSON-отчётов)."""
    if isinstance(value, str):
        return clean(value)
    if isinstance(value, dict):
        return {k: (MASK if _is_secret_key(k) else redact_obj(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_obj(v) for v in value]
    return value


def _is_secret_key(key: Any) -> bool:
    return isinstance(key, str) and bool(re.search(r"(?i)(password|passwd|secret|token|cookie_value|authorization)", key))
