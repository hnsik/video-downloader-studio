"""Полная диагностика и экспорт отчёта (TXT/JSON) без чувствительных данных."""
from __future__ import annotations

import json
import platform
import re
import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from . import __version__
from .auth import METHOD_NAMES, check_auth, detect_browsers, validate_cookie_file
from .components import ORDER, SPECS, vkey
from .platforms import PLATFORMS
from .redact import clean, redact_obj

OK, WARN, FAIL, INFO = "ok", "warn", "fail", "info"
ICON = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]", INFO: "[INFO]"}


@dataclass
class Check:
    group: str
    name: str
    status: str
    detail: str = ""
    hint: str = ""


@dataclass
class Report:
    created: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    checks: list[Check] = field(default_factory=list)

    def add(self, group: str, name: str, status: str, detail: str = "", hint: str = "") -> None:
        self.checks.append(Check(group, name, status, detail, hint))

    @property
    def worst(self) -> str:
        statuses = {c.status for c in self.checks}
        return FAIL if FAIL in statuses else WARN if WARN in statuses else OK

    def to_dict(self) -> dict:
        return redact_obj({
            "created": self.created, "app_version": __version__, "summary": self.worst,
            "checks": [c.__dict__ for c in self.checks],
        })

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    def to_text(self) -> str:
        data = self.to_dict()
        lines = [f"Video Downloader Studio {__version__} — диагностический отчёт", f"Создан: {data['created']}",
                 f"Итог: {data['summary']}", "Cookies, токены, пароли и подписанные URL скрыты автоматически.", ""]
        group = None
        for c in data["checks"]:
            if c["group"] != group:
                group = c["group"]
                lines += ["", f"== {group} =="]
            lines.append(f"{ICON.get(c['status'], '[ ?? ]')} {c['name']}: {c['detail']}")
            if c["hint"]:
                lines.append(f"        → {c['hint']}")
        return "\n".join(lines) + "\n"


def _mm(version: str) -> tuple[int, int] | None:
    m = re.search(r"(\d+)\.(\d+)", version or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def run_full(core, *, check_logins: bool = True, progress: Callable[[str], None] | None = None) -> Report:
    say = progress or (lambda s: None)
    rep = Report()
    cm, settings = core.components, core.settings

    say("Система…")
    rep.add("Система", "Операционная система", INFO, f"{platform.platform()} ({platform.machine()})")
    rep.add("Система", "Приложение", INFO, f"версия {__version__}; {'установленная сборка' if getattr(sys, 'frozen', False) else 'запуск из исходников, Python ' + platform.python_version()}")
    if sys.platform == "win32" and platform.machine().lower() not in ("amd64", "x86_64"):
        rep.add("Система", "Архитектура", FAIL, platform.machine(), "Приложение рассчитано на Windows x64.")

    say("Компоненты…")
    statuses = {s.key: s for s in cm.statuses()}
    for key in ORDER:
        s = statuses[key]
        if s.healthy:
            rep.add("Компоненты", SPECS[key].title, OK, f"версия {s.installed or '?'}; источник: {s.source}; путь: {s.path}")
        elif SPECS[key].optional and not s.path:
            rep.add("Компоненты", SPECS[key].title, INFO, "не установлен (необязателен)")
        else:
            rep.add("Компоненты", SPECS[key].title, FAIL, f"{s.health}; путь: {s.path or '—'}",
                    "Откройте «Компоненты и обновления» и установите/переустановите компонент.")
        if s.update_available:
            rep.add("Компоненты", f"Обновление: {SPECS[key].title}", WARN, f"доступна {s.latest} (установлена {s.installed or '—'})")

    say("Совместимость…")
    ff, fp = cm.resolve("ffmpeg"), cm.resolve("ffprobe")
    if ff.path and fp.path:
        same_dir = ff.path.parent == fp.path.parent
        a, b = _mm(statuses["ffmpeg"].installed), _mm(statuses["ffprobe"].installed)
        if same_dir and (a == b or a is None or b is None):
            rep.add("Совместимость", "FFmpeg и FFprobe", OK, "находятся в одной папке, версии совпадают")
        else:
            rep.add("Совместимость", "FFmpeg и FFprobe", WARN,
                    f"ffmpeg {statuses['ffmpeg'].installed}, ffprobe {statuses['ffprobe'].installed}, одна папка: {same_dir}",
                    "Переустановите FFmpeg в «Компоненты и обновления» — FFmpeg и FFprobe ставятся из одной сборки.")
    deno = statuses["deno"].installed
    if deno:
        ok = vkey(deno) >= (2, 3, 0)
        rep.add("Совместимость", "Deno для EJS", OK if ok else FAIL, f"Deno {deno} (минимум 2.3.0)",
                "" if ok else "Обновите Deno.")
    else:
        rep.add("Совместимость", "Deno для EJS", FAIL if not statuses["node"].healthy else WARN, "Deno не найден",
                "YouTube требует JavaScript runtime: установите Deno.")
    env = cm.ytdlp_environment(cm.ffmpeg_dir())
    if env["ejs"]:
        rep.add("Совместимость", "yt-dlp EJS", OK, f"yt-dlp-ejs {env['ejs']}" + (f"; {env['js_runtimes']}" if env["js_runtimes"] else ""))
    else:
        rep.add("Совместимость", "yt-dlp EJS", INFO, "версия EJS не определена (в официальных сборках yt-dlp EJS встроен)")
    if env["exe_versions"]:
        rep.add("Совместимость", "Что видит yt-dlp", INFO, env["exe_versions"])
    rep.add("Совместимость", "Имитация браузера (curl_cffi)", OK if env["curl_cffi"] else INFO,
            "доступна" if env["curl_cffi"] else "не обнаружена; TikTok может требовать её")

    say("Сеть…")
    for host in ("github.com", "www.youtube.com"):
        try:
            socket.create_connection((host, 443), timeout=6).close()
            rep.add("Сеть", f"Доступность {host}", OK, "соединение установлено")
        except OSError as exc:
            rep.add("Сеть", f"Доступность {host}", FAIL, f"{type(exc).__name__}: {exc}", "Проверьте интернет, VPN/прокси и брандмауэр.")

    say("Диск…")
    folder = Path(settings.get("download_dir"))
    try:
        folder.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=folder, prefix=".vds_write_test_", delete=True):
            pass
        rep.add("Диск", "Папка загрузок", OK, f"{folder} — запись разрешена")
        free = shutil.disk_usage(folder).free
        gb = free / 1024 ** 3
        rep.add("Диск", "Свободное место", OK if gb >= 2 else WARN if gb >= 0.5 else FAIL, f"{gb:.1f} ГБ",
                "" if gb >= 2 else "Освободите место или выберите другую папку.")
    except OSError as exc:
        rep.add("Диск", "Папка загрузок", FAIL, f"{folder}: {exc.strerror or exc}", "Выберите другую папку в настройках.")

    say("Браузеры…")
    browsers = detect_browsers()
    if browsers:
        for b in browsers:
            rep.add("Браузеры", b.name, INFO, f"профилей: {len(b.profiles)}" + (f"; {b.exe}" if b.exe else ""))
    else:
        rep.add("Браузеры", "Установленные браузеры", INFO, "не обнаружены (или система не Windows)")

    say("Авторизация…")
    yt = cm.path("ytdlp")
    for key, title in PLATFORMS.items():
        auth = settings.auth_for(key)
        method = auth.get("method", "none")
        if method == "none":
            rep.add("Авторизация", title, INFO, "не используется")
            continue
        label = METHOD_NAMES.get(method, method)
        if method == "file":
            info = validate_cookie_file(auth.get("cookie_file", ""))
            rep.add("Авторизация", title, OK if info.ok else FAIL, f"{label}: {info.message}")
        elif check_logins and key != "other":
            res = check_auth(yt, auth, key)
            status = OK if res.ok and res.logged_in is not False else WARN if res.ok else FAIL
            rep.add("Авторизация", title, status, f"{label}: {res.message}", res.hint)
        else:
            rep.add("Авторизация", title, INFO, f"{label} (проверка пропущена)")
    return rep


def export(report: Report, path: Path) -> None:
    text = report.to_json() if path.suffix.lower() == ".json" else report.to_text()
    path.write_text(clean(text) if path.suffix.lower() != ".json" else text, encoding="utf-8")
