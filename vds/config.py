"""Настройки приложения: хранение, резервное копирование, миграция со старых версий."""
from __future__ import annotations

import copy
import json
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths
from .platforms import DEFAULT_PLATFORM_OPTIONS, PLATFORMS

SCHEMA = 2

AUTH_METHODS = ("none", "firefox", "chrome", "edge", "brave", "opera", "file")


def _default_auth() -> dict[str, dict]:
    return {key: {"method": "none", "profile": "", "cookie_file": ""} for key in PLATFORMS}


DEFAULTS: dict[str, Any] = {
    "schema": SCHEMA,
    "download_dir": str(Path.home() / "Downloads"),
    "container": "mp4",            # mp4 | mkv | webm
    "quality": "best",             # best | 2160 | 1440 | 1080 | 720 | 480 | 360
    "video_codec": "auto",         # auto | h264 | h265 | av1
    "mode": "video",               # video | video_only | audio
    "audio_format": "mp3",         # mp3 | wav | m4a
    "subtitles": False,
    "sub_langs": "ru,en",
    "thumbnails": False,
    "filename_template": "%(title).160B [%(id)s].%(ext)s",
    "probe_after": True,
    "ios_compat": False,           # режим «Совместимость с iPhone» из v5.x (H.264 + AAC)
    "concurrency": 2,
    "rate_limit": "",              # например 2M, 500K
    "retries": 5,
    "stall_timeout": 180,
    "confirm_rights": True,        # требовать подтверждение права на скачивание
    "channel": "stable",           # stable | nightly
    "auto_check_updates": True,
    "ejs_remote": False,           # --remote-components ejs:github
    "allow_system_path": False,    # использовать yt-dlp/ffmpeg/deno из PATH, если своих нет
    "prevent_duplicates": True,
    "auth": _default_auth(),
    "platform_options": copy.deepcopy(DEFAULT_PLATFORM_OPTIONS),
    "presets": {},                 # пользовательские пресеты {имя: {настройки}}
    "extra": {},                   # неизвестные ключи прежних версий сохраняются здесь
}


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict) and key not in ("presets", "extra"):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


# --- миграция --------------------------------------------------------------

_LEGACY_MAP = {
    "folder": "download_dir", "output_dir": "download_dir", "download_folder": "download_dir", "download_dir": "download_dir",
    "iphone": "ios_compat", "ios_compat": "ios_compat", "iphone_mode": "ios_compat",
    "quality": "quality", "container": "container", "format": "container",
    "template": "filename_template", "filename_template": "filename_template",
    "concurrency": "concurrency", "parallel": "concurrency",
}


def migrate(raw: dict) -> dict:
    """Приводит настройки любой прежней версии к текущей схеме. Ничего не удаляет."""
    if not isinstance(raw, dict):
        return copy.deepcopy(DEFAULTS)
    if raw.get("schema") == SCHEMA:
        return _merge(DEFAULTS, raw)
    data = copy.deepcopy(DEFAULTS)
    extra: dict[str, Any] = dict(raw.get("extra", {})) if isinstance(raw.get("extra"), dict) else {}
    for key, value in raw.items():
        if key in ("schema", "extra"):
            continue
        target = _LEGACY_MAP.get(key)
        if target:
            data[target] = value
        elif key in DEFAULTS and key not in ("auth", "platform_options"):
            data[key] = value
        elif key in ("youtube_cookie", "yt_cookie", "cookies_youtube"):
            data["auth"]["youtube"].update(method="file" if value else "none", cookie_file=value or "")
        elif key in ("tiktok_cookie", "tt_cookie", "cookies_tiktok"):
            data["auth"]["tiktok"].update(method="file" if value else "none", cookie_file=value or "")
        elif key == "auth" and isinstance(value, dict):
            data["auth"] = _merge(data["auth"], value)
        elif key == "platform_options" and isinstance(value, dict):
            data["platform_options"] = _merge(data["platform_options"], value)
        else:
            extra[key] = value
    if str(data.get("quality")) in ("2160p", "4k", "4K"):
        data["quality"] = "2160"
    data["quality"] = str(data["quality"]).rstrip("p") or "best"
    data["extra"] = extra
    data["schema"] = SCHEMA
    return data


class Settings:
    """Потокобезопасное хранилище настроек с атомарной записью и резервной копией."""

    def __init__(self, path: Path | None = None):
        self.path = path or (paths.data_dir() / "settings.json")
        self._lock = threading.RLock()
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.migrated_from: str | None = None

    # -- загрузка/сохранение
    def load(self) -> "Settings":
        with self._lock:
            if self.path.exists():
                raw = self._read(self.path)
                if raw is None:  # повреждён — пробуем резервную копию
                    bak = self.path.with_suffix(".json.bak")
                    raw = self._read(bak) if bak.exists() else None
                if raw is not None:
                    if raw.get("schema") != SCHEMA:
                        self._backup("pre-migration")
                    self.data = migrate(raw)
                    self.save()
                    return self
            legacy = self._find_legacy()
            if legacy is not None:
                path, raw = legacy
                self._backup_file(path, "legacy")
                self.data = migrate(raw)
                self.migrated_from = str(path)
                self.save()
            return self

    @staticmethod
    def _read(path: Path) -> dict | None:
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            return value if isinstance(value, dict) else None
        except (OSError, ValueError):
            return None

    def _find_legacy(self) -> tuple[Path, dict] | None:
        for candidate in paths.legacy_settings_candidates():
            if candidate.resolve() == self.path.resolve():
                continue
            if candidate.is_file():
                raw = self._read(candidate)
                if raw is not None:
                    return candidate, raw
        return None

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                try:
                    shutil.copy2(self.path, self.path.with_suffix(".json.bak"))
                except OSError:
                    pass
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, self.path)

    def _backup(self, label: str) -> Path | None:
        return self._backup_file(self.path, label)

    @staticmethod
    def _backup_file(src: Path, label: str) -> Path | None:
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dst = paths.backup_dir() / f"{src.stem}-{label}-{stamp}{src.suffix}"
            shutil.copy2(src, dst)
            return dst
        except OSError:
            return None

    # -- доступ
    def get(self, key: str, default: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self.data.get(key, default))

    def set(self, key: str, value: Any) -> None:
        with self._lock:
            self.data[key] = value

    def update(self, values: dict[str, Any]) -> None:
        with self._lock:
            self.data.update(values)

    def auth_for(self, platform: str) -> dict:
        with self._lock:
            auth = self.data.get("auth", {})
            return copy.deepcopy(auth.get(platform) or auth.get("other") or {"method": "none", "profile": "", "cookie_file": ""})

    def set_auth(self, platform: str, method: str, profile: str = "", cookie_file: str = "") -> None:
        with self._lock:
            self.data.setdefault("auth", {})[platform] = {"method": method, "profile": profile, "cookie_file": cookie_file}

    def reset_auth(self, platform: str | None = None) -> None:
        with self._lock:
            if platform:
                self.data.setdefault("auth", {})[platform] = {"method": "none", "profile": "", "cookie_file": ""}
            else:
                self.data["auth"] = _default_auth()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data)
