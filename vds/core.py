"""Связывает настройки, компоненты, движок и очередь в единый объект приложения."""
from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from . import paths
from .auth import cookie_args
from .components import ComponentManager
from .config import Settings
from .engine import Engine, JobOptions, Tools
from .jobqueue import JobQueue
from .platforms import detect_platform
from .redact import clean
from .store import Store


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = clean(record.getMessage())
        record.args = ()
        return True


def setup_logging() -> logging.Logger:
    logger = logging.getLogger("vds")
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)
    handler = logging.handlers.RotatingFileHandler(paths.log_dir() / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    return logger


class AppCore:
    def __init__(self, settings: Settings | None = None, store: Store | None = None,
                 components: ComponentManager | None = None):
        self.log = setup_logging()
        self.settings = settings or Settings().load()
        self.store = store or Store()
        self.queue: JobQueue
        self.components = components or ComponentManager(settings=self.settings, busy_check=lambda: self.queue.is_busy())
        self.engine = Engine(self.tools)
        self.queue = JobQueue(self.engine, self.store, int(self.settings.get("concurrency", 2)))
        self.queue.restore()

    def tools(self) -> Tools:
        c = self.components
        return Tools(c.path("ytdlp"), c.path("ffmpeg"), c.path("ffprobe"), c.path("deno"), c.path("node"))

    def options_for(self, url: str, overrides: dict | None = None) -> JobOptions:
        s = self.settings.snapshot()
        s.update(overrides or {})
        platform = detect_platform(url)
        auth = self.settings.auth_for(platform)
        opts = JobOptions(
            url=url, out_dir=s["download_dir"], platform=platform, container=s["container"], quality=s["quality"],
            video_codec=s["video_codec"], mode=s["mode"], audio_format=s["audio_format"], subtitles=s["subtitles"],
            sub_langs=s["sub_langs"], thumbnails=s["thumbnails"], template=s["filename_template"],
            rate_limit=s["rate_limit"], retries=int(s["retries"]), stall_timeout=int(s["stall_timeout"]),
            probe_after=s["probe_after"], ios_compat=s["ios_compat"], ejs_remote=s["ejs_remote"],
            impersonate=(s.get("platform_options", {}).get(platform, {}) or {}).get("impersonate", ""),
            cookie_args=cookie_args(auth),
        )
        return opts

    def shutdown(self) -> None:
        self.queue.stop_all_for_exit()
        self.settings.save()
