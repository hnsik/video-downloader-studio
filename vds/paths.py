"""Расположение каталогов приложения.

* data_dir()       — настройки, история, очередь, журналы, резервные копии (%APPDATA%).
* components_dir() — обновляемые компоненты (%LOCALAPPDATA%), пишется без прав администратора.
* bundled_dir()    — компоненты, поставляемые с установщиком (только чтение).
Переменная окружения VDS_HOME переопределяет всё (тесты, портативный режим).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DIR_NAME = "VideoDownloaderStudio"


def _home_override() -> Path | None:
    value = os.environ.get("VDS_HOME")
    return Path(value) if value else None


def app_dir() -> Path:
    """Каталог установки (рядом с exe) либо корень репозитория при запуске из исходников."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    home = _home_override()
    if home:
        path = home / "data"
    elif os.name == "nt":
        path = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / DIR_NAME
    else:
        path = Path.home() / ".local" / "share" / DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def components_dir() -> Path:
    home = _home_override()
    if home:
        path = home / "components"
    elif os.name == "nt":
        path = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / DIR_NAME / "components"
    else:
        path = Path.home() / ".local" / "share" / DIR_NAME / "components"
    path.mkdir(parents=True, exist_ok=True)
    return path


def bundled_dir() -> Path:
    return app_dir() / "bundled"


def legacy_component_dirs() -> list[Path]:
    """Места, где старые версии держали ffmpeg/ffprobe (например F:\\VideoDownloaderStudio\\_internal)."""
    base = app_dir()
    return [base / "_internal", base]


def log_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def backup_dir() -> Path:
    path = data_dir() / "backups"
    path.mkdir(parents=True, exist_ok=True)
    return path


def legacy_settings_candidates() -> list[Path]:
    """Файлы настроек прежних версий, откуда возможна миграция."""
    local = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / DIR_NAME
    roaming = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / DIR_NAME
    base = app_dir()
    names = ("settings.json", "config.json", "vds_settings.json")
    result: list[Path] = []
    for folder in (local, roaming, base, base / "_internal"):
        for name in names:
            result.append(folder / name)
    return result
