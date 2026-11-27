"""Менеджер авторизации: обнаружение браузеров и профилей, cookies.txt, проверка наличия входа.

Пароли не запрашиваются и не сохраняются. Значения cookies никогда не читаются в память приложения
и не выводятся: проверяются только имена и количество. Для чтения cookies из браузера используется
штатный механизм yt-dlp (`--cookies-from-browser`).
"""
from __future__ import annotations

import configparser
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .components import run_capture
from .platforms import AUTH_TEST_URL, COOKIE_DOMAINS, SESSION_COOKIES
from .redact import clean

BROWSER_NAMES = {"firefox": "Mozilla Firefox", "chrome": "Google Chrome", "edge": "Microsoft Edge",
                 "brave": "Brave", "opera": "Opera"}
METHOD_NAMES = {"none": "Без авторизации", **BROWSER_NAMES, "file": "Файл cookies.txt (Netscape)"}


@dataclass
class Profile:
    id: str       # то, что передаётся yt-dlp после двоеточия
    name: str     # человекочитаемое имя


@dataclass
class Browser:
    key: str
    name: str
    exe: str = ""
    profiles: list[Profile] = field(default_factory=list)


def _env(name: str, default: str = "") -> Path:
    return Path(os.environ.get(name) or default)


def _first_existing(paths: list[Path]) -> Path | None:
    return next((p for p in paths if p and p.is_file()), None)


def _firefox_profiles(appdata: Path) -> list[Profile]:
    ini = appdata / "Mozilla" / "Firefox" / "profiles.ini"
    result: list[Profile] = []
    if not ini.is_file():
        return result
    cp = configparser.RawConfigParser()
    try:
        cp.read(ini, encoding="utf-8")
    except (configparser.Error, OSError):
        return result
    default_path = ""
    for section in cp.sections():
        if section.startswith("Install") and cp.has_option(section, "Default"):
            default_path = cp.get(section, "Default").replace("\\", "/").split("/")[-1]
    for section in cp.sections():
        if section.startswith("Profile") and cp.has_option(section, "Path"):
            folder = cp.get(section, "Path").replace("\\", "/").split("/")[-1]
            name = cp.get(section, "Name") if cp.has_option(section, "Name") else folder
            label = f"{name}" + (" (по умолчанию)" if folder == default_path else "")
            result.append(Profile(folder, label))
    result.sort(key=lambda p: "по умолчанию" not in p.name)
    return result


def _chromium_profiles(user_data: Path) -> list[Profile]:
    result: list[Profile] = []
    if not user_data.is_dir():
        return result
    names: dict[str, str] = {}
    try:
        state = json.loads((user_data / "Local State").read_text(encoding="utf-8"))
        for folder, info in (state.get("profile", {}).get("info_cache", {}) or {}).items():
            names[folder] = info.get("name", folder)
    except (OSError, ValueError):
        pass
    for d in sorted(user_data.iterdir()):
        if d.is_dir() and (d.name == "Default" or re.fullmatch(r"Profile \d+", d.name)) and (d / "Preferences").exists():
            label = names.get(d.name, d.name)
            result.append(Profile(d.name, f"{label} ({d.name})" if label != d.name else d.name))
    return result


def detect_browsers() -> list[Browser]:
    """Находит установленные браузеры и их профили (Windows; на других ОС вернёт пустой список)."""
    if os.name != "nt":
        return []
    pf, pf86 = _env("ProgramFiles", r"C:\Program Files"), _env("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local, roaming = _env("LOCALAPPDATA"), _env("APPDATA")
    found: list[Browser] = []

    ff = _first_existing([pf / "Mozilla Firefox" / "firefox.exe", pf86 / "Mozilla Firefox" / "firefox.exe", local / "Mozilla Firefox" / "firefox.exe"])
    ff_profiles = _firefox_profiles(roaming)
    if ff or ff_profiles:
        found.append(Browser("firefox", BROWSER_NAMES["firefox"], str(ff or ""), ff_profiles))

    specs = {
        "chrome": ([pf / "Google/Chrome/Application/chrome.exe", pf86 / "Google/Chrome/Application/chrome.exe", local / "Google/Chrome/Application/chrome.exe"],
                   local / "Google/Chrome/User Data"),
        "edge": ([pf86 / "Microsoft/Edge/Application/msedge.exe", pf / "Microsoft/Edge/Application/msedge.exe"], local / "Microsoft/Edge/User Data"),
        "brave": ([pf / "BraveSoftware/Brave-Browser/Application/brave.exe", pf86 / "BraveSoftware/Brave-Browser/Application/brave.exe",
                   local / "BraveSoftware/Brave-Browser/Application/brave.exe"], local / "BraveSoftware/Brave-Browser/User Data"),
        "opera": ([local / "Programs/Opera/opera.exe", pf / "Opera/opera.exe", local / "Programs/Opera GX/opera.exe"], roaming / "Opera Software/Opera Stable"),
    }
    for key, (exes, data) in specs.items():
        exe_path = _first_existing(exes)
        if not exe_path and not data.is_dir():
            continue
        if key == "opera":
            profiles = [Profile("", "Профиль Opera")] if data.is_dir() else []
        else:
            profiles = _chromium_profiles(data)
        found.append(Browser(key, BROWSER_NAMES[key], str(exe_path or ""), profiles))
    return found


# --- cookies ----------------------------------------------------------------------

def cookie_args(auth: dict) -> list[str]:
    """Аргументы yt-dlp для выбранного способа авторизации."""
    method = (auth or {}).get("method", "none")
    if method in BROWSER_NAMES:
        spec = method + (f":{auth['profile']}" if auth.get("profile") else "")
        return ["--cookies-from-browser", spec]
    if method == "file" and auth.get("cookie_file"):
        return ["--cookies", auth["cookie_file"].strip().lstrip("\u202a\u202b\u202c\u200e\u200f")]
    return []


@dataclass
class CookieFileInfo:
    ok: bool
    count: int = 0
    domains: list[str] = field(default_factory=list)
    message: str = ""


def validate_cookie_file(path: str) -> CookieFileInfo:
    """Проверяет формат Netscape. Значения cookies не сохраняются."""
    path = (path or "").strip().lstrip("\u202a\u202b\u202c\u200e\u200f")
    p = Path(path)
    if not path or not p.is_file():
        return CookieFileInfo(False, message="Файл не найден")
    try:
        text = p.read_text(encoding="utf-8-sig", errors="replace")
    except OSError as exc:
        return CookieFileInfo(False, message=f"Файл недоступен: {exc.strerror or exc}")
    first = text.lstrip().splitlines()[0] if text.strip() else ""
    if "Netscape" not in first and "HTTP Cookie File" not in first:
        return CookieFileInfo(False, message="Это не файл cookies в формате Netscape (нет заголовка «# Netscape HTTP Cookie File»)")
    domains: set[str] = set()
    count = 0
    for line in text.splitlines():
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        fields = line.split("\t")
        if len(fields) == 7:
            count += 1
            domains.add(fields[0].lstrip("#").removeprefix("HttpOnly_").lstrip("."))
    if not count:
        return CookieFileInfo(False, message="В файле нет ни одной записи cookies")
    return CookieFileInfo(True, count, sorted(domains), f"Найдено записей: {count}")


def session_cookie_names(path: Path, platform: str) -> list[str]:
    """Имена сессионных cookies платформы, найденные в файле (значения не читаются)."""
    wanted = SESSION_COOKIES.get(platform, ())
    domains = COOKIE_DOMAINS.get(platform, ())
    found: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            f = line.split("\t")
            if len(f) == 7:
                dom = f[0].lstrip("#").removeprefix("HttpOnly_").lstrip(".").lower()
                if f[5] in wanted and any(dom == d or dom.endswith("." + d) for d in domains):
                    found.add(f[5])
    except OSError:
        pass
    return sorted(found)


@dataclass
class AuthCheck:
    ok: bool
    cookies_loaded: int = 0
    logged_in: bool | None = None   # None — не удалось определить
    message: str = ""
    hint: str = ""
    session_names: list[str] = field(default_factory=list)


def check_auth(ytdlp: Path | None, auth: dict, platform: str, timeout: float = 90.0) -> AuthCheck:
    """«Проверить авторизацию»: доступны ли cookies и есть ли в них сессия платформы.

    Для браузера yt-dlp выгружает cookies во временный файл; читаются только имена. Файл удаляется.
    Проверка подтверждает наличие сессионных cookies, но не гарантирует, что сессия не истекла на сервере.
    """
    method = auth.get("method", "none")
    if method == "none":
        return AuthCheck(True, 0, None, "Авторизация не используется.")
    if method == "file":
        info = validate_cookie_file(auth.get("cookie_file", ""))
        if not info.ok:
            return AuthCheck(False, message=info.message)
        names = session_cookie_names(Path(auth["cookie_file"].strip().lstrip("\u202a\u202b\u202c\u200e\u200f")), platform)
        if platform in SESSION_COOKIES:
            logged = bool(names)
            return AuthCheck(True, info.count, logged,
                             f"Cookies: {info.count}. " + ("Найдены сессионные cookies платформы." if logged else "Сессионных cookies этой платформы в файле нет."),
                             "" if logged else "Экспортируйте cookies после входа на сайт.", names)
        return AuthCheck(True, info.count, None, f"Cookies: {info.count}.")
    if not ytdlp:
        return AuthCheck(False, message="yt-dlp не найден — установите его в разделе «Компоненты и обновления».")
    tmp = Path(tempfile.mkdtemp(prefix="vds-auth-")) / "check.txt"
    try:
        cmd = [str(ytdlp), "--ignore-config", "--color", "never", "--skip-download", "--simulate", "--no-warnings",
               "--cookies-from-browser", method + (f":{auth['profile']}" if auth.get("profile") else ""),
               "--cookies", str(tmp), "--playlist-items", "0", "--ignore-errors",
               AUTH_TEST_URL.get(platform, "https://www.youtube.com/")]
        code, out = run_capture(cmd, timeout)
        text = clean(out)
        m = re.search(r"Extracted (\d+) cookies", out)
        loaded = int(m.group(1)) if m else 0
        from .errors import classify
        if re.search(r"failed to decrypt with dpapi", out, re.I) or re.search(r"could not copy .*cookie database", out, re.I):
            info = classify(out)
            return AuthCheck(False, 0, None, info.title, info.solution)
        if not loaded and tmp.exists():
            loaded = sum(1 for ln in tmp.read_text(encoding="utf-8", errors="replace").splitlines() if ln.count("\t") == 6)
        if not loaded:
            return AuthCheck(False, 0, None, "Не удалось получить cookies из браузера. " + text[-200:],
                             "Закройте браузер, выберите правильный профиль или используйте Firefox / cookies.txt.")
        names = session_cookie_names(tmp, platform) if tmp.exists() else []
        if platform in SESSION_COOKIES:
            logged = bool(names)
            return AuthCheck(True, loaded, logged,
                             f"Получено cookies: {loaded}. " + ("Сессия платформы найдена." if logged else "Сессионных cookies платформы нет — вы не вошли в аккаунт в этом браузере/профиле."),
                             "" if logged else "Войдите на сайт в выбранном браузере и повторите проверку.", names)
        return AuthCheck(True, loaded, None, f"Получено cookies: {loaded}.")
    finally:
        try:
            tmp.unlink()
            tmp.parent.rmdir()
        except OSError:
            pass
