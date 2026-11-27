"""Форматирование данных для интерфейса (без зависимости от Qt — покрыто тестами)."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_UNITS = ("Б", "КБ", "МБ", "ГБ", "ТБ")


def fmt_size(n: float | int | None) -> str:
    if not n or n < 0:
        return "—"
    value = float(n)
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return "—"


def fmt_speed(bps: float | int | None) -> str:
    if not bps or bps <= 0:
        return "—"
    return fmt_size(bps) + "/с"


def fmt_eta(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


_URL_SPLIT = re.compile(r"[\s,;]+")


def parse_links(text: str) -> tuple[list[str], list[str]]:
    """Разбирает вставленный текст на ссылки. Возвращает (корректные без повторов, некорректные)."""
    good: list[str] = []
    bad: list[str] = []
    seen: set[str] = set()
    for raw in _URL_SPLIT.split(text or ""):
        token = raw.strip().strip("‪‫‬‎‏﻿<>\"'")
        if not token:
            continue
        parts = urlsplit(token)
        if parts.scheme in ("http", "https") and parts.hostname:
            if token not in seen:
                seen.add(token)
                good.append(token)
        else:
            bad.append(token)
    return good, bad


def job_row(job) -> dict:
    """Строка таблицы очереди для задачи."""
    from .jobqueue import STATUS_RU

    total = job.total or job.size
    return {
        "title": job.title or job.url,
        "platform": job.platform,
        "status": STATUS_RU.get(job.status, job.status),
        "percent": int(max(0.0, min(100.0, job.percent))),
        "speed": fmt_speed(job.speed),
        "size": fmt_size(total),
        "eta": fmt_eta(job.eta),
    }


def short_path(path: str, limit: int = 70) -> str:
    if len(path) <= limit:
        return path
    return path[:limit // 2 - 1] + "…" + path[-(limit // 2):]
