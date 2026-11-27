"""Менеджер обновляемых компонентов: yt-dlp, EJS, Deno, FFmpeg/FFprobe, Node.js.

Принципы безопасности:
* загрузка только по HTTPS с официальных доменов (GitHub, nodejs.org), в том числе после редиректов;
* проверка SHA-256 по контрольным суммам, которые публикует разработчик (SHA2-256SUMS, checksums.sha256,
  .sha256sum, SHASUMS256.txt); если сумма не опубликована — статус «не проверена» фиксируется в истории;
* загрузка в .part-файл и распаковка в staging-каталог; активная версия не затрагивается до успешной проверки;
* проверка запуском нового компонента перед активацией, автоматический откат при неудаче;
* предыдущие версии хранятся для ручного отката; замена запрещена, пока идут загрузки видео.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from . import paths
from .redact import clean

TRUSTED_HOSTS = {
    "github.com", "api.github.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com", "github-releases.githubusercontent.com", "nodejs.org",
}
USER_AGENT = "VideoDownloaderStudio/6 (+https://github.com/hnsik/video-downloader-studio)"

ProgressCb = Callable[[int, "int | None", str], None]


def exe(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def no_window_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def clean_env() -> dict[str, str]:
    """Окружение для дочерних процессов без переменных, мешающих внешним программам."""
    env = dict(os.environ)
    for key in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "PYTHONEXECUTABLE", "_MEIPASS2"):
        env.pop(key, None)
    env["NO_COLOR"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def run_capture(cmd: list[str], timeout: float = 25.0) -> tuple[int, str]:
    """Запускает процесс и возвращает (код, объединённый вывод). Исключения превращаются в код -1."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, timeout=timeout, env=clean_env(),
            creationflags=no_window_flags(), stdin=subprocess.DEVNULL,
        )
        out = (proc.stdout or b"") + b"\n" + (proc.stderr or b"")
        return proc.returncode, out.decode("utf-8", errors="replace").strip()
    except subprocess.TimeoutExpired:
        return -1, "Превышено время ожидания"
    except OSError as exc:
        return -1, f"{type(exc).__name__}: {exc}"


# --- версии ------------------------------------------------------------------

def vkey(version: str) -> tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", version or ""))


def is_newer(latest: str, installed: str) -> bool:
    a, b = vkey(latest), vkey(installed)
    return bool(a) and a > b


def exe_name(key: str) -> str:
    """Имя исполняемого файла компонента (ключ ytdlp → yt-dlp.exe)."""
    return exe("yt-dlp" if key == "ytdlp" else key)


def parse_version(key: str, output: str) -> str:
    """Извлекает номер версии из вывода программы."""
    text = output or ""
    key = "ytdlp" if key == "yt-dlp" else key
    if key == "ytdlp":
        m = re.search(r"^\s*(\d{4}\.\d{2}\.\d{2}(?:\.\d+)?)\s*$", text, re.M)
        return m.group(1) if m else ""
    if key == "deno":
        m = re.search(r"deno\s+(\d+\.\d+\.\d+\S*)", text)
        return m.group(1) if m else ""
    if key in ("ffmpeg", "ffprobe"):
        m = re.search(r"(?:ffmpeg|ffprobe) version\s+(\S+)", text)
        return m.group(1) if m else ""
    if key == "node":
        m = re.search(r"v?(\d+\.\d+\.\d+)", text)
        return m.group(1) if m else ""
    return ""


# --- сеть --------------------------------------------------------------------

class UpdateError(Exception):
    pass


class ComponentBusyError(UpdateError):
    pass


class Cancelled(UpdateError):
    pass


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_trusted(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_trusted(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme != "https" or (parts.hostname or "").lower() not in TRUSTED_HOSTS:
        raise UpdateError(f"Недоверенный источник: {parts.scheme}://{parts.hostname}")


class HttpClient:
    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_SafeRedirect())

    def _open(self, url: str):
        check_trusted(url)
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json, */*"})
        return self._opener.open(req, timeout=self.timeout)

    def get_text(self, url: str) -> str:
        try:
            with self._open(url) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, OSError) as exc:
            raise UpdateError(f"Не удалось получить {urlsplit(url).hostname}: {exc}") from exc

    def get_json(self, url: str):
        return json.loads(self.get_text(url))

    def download(self, url: str, dest: Path, progress: ProgressCb | None = None, cancel: threading.Event | None = None) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._open(url) as resp, open(dest, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0) or None
                done = 0
                while True:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled("Загрузка отменена")
                    chunk = resp.read(1024 * 256)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total, "Загрузка")
                if total and done != total:
                    raise UpdateError(f"Загрузка прервана: получено {done} из {total} байт")
        except (urllib.error.URLError, OSError) as exc:
            raise UpdateError(f"Ошибка загрузки: {exc}") from exc


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def parse_checksums(text: str, filename: str) -> str:
    """Находит SHA-256 для файла в форматах `hash  name`, `hash *name` либо единственный `hash`."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) == 1 and re.fullmatch(r"[0-9a-fA-F]{64}", lines[0].split()[0] if lines[0].split() else ""):
        return lines[0].split()[0].lower()
    for ln in lines:
        m = re.match(r"^([0-9a-fA-F]{64})\s+\*?(.+)$", ln)
        if m and Path(m.group(2).strip()).name == filename:
            return m.group(1).lower()
    return ""


# --- описание релизов ----------------------------------------------------------

@dataclass
class Release:
    key: str
    version: str                 # человекочитаемая версия
    asset_name: str
    url: str
    checksum_url: str = ""
    digest: str = ""             # sha256 из метаданных GitHub, если нет файла контрольных сумм
    stamp: str = ""              # идентификатор «плавающей» сборки (дата обновления файла)
    channel: str = "stable"
    notes: str = ""


def _gh(http, repo: str, tag: str | None = None) -> dict:
    path = f"releases/tags/{tag}" if tag else "releases/latest"
    return http.get_json(f"https://api.github.com/repos/{repo}/{path}")


def _asset(data: dict, name: str | re.Pattern) -> dict | None:
    for a in data.get("assets", []):
        n = a.get("name", "")
        if (isinstance(name, str) and n == name) or (not isinstance(name, str) and name.fullmatch(n)):
            return a
    return None


def _digest(asset: dict | None) -> str:
    d = (asset or {}).get("digest") or ""
    return d.split(":", 1)[1].lower() if d.startswith("sha256:") else ""


def latest_ytdlp(http, channel: str = "stable") -> Release:
    repo = "yt-dlp/yt-dlp-nightly-builds" if channel == "nightly" else "yt-dlp/yt-dlp"
    data = _gh(http, repo)
    asset = _asset(data, "yt-dlp.exe")
    if not asset:
        raise UpdateError("В релизе yt-dlp нет yt-dlp.exe")
    sums = _asset(data, "SHA2-256SUMS")
    return Release("ytdlp", data["tag_name"].lstrip("v"), "yt-dlp.exe", asset["browser_download_url"],
                   sums["browser_download_url"] if sums else "", _digest(asset), asset.get("updated_at", ""), channel)


def latest_deno(http) -> Release:
    data = _gh(http, "denoland/deno")
    name = "deno-x86_64-pc-windows-msvc.zip"
    asset = _asset(data, name)
    if not asset:
        raise UpdateError("В релизе Deno нет сборки для Windows x64")
    sums = _asset(data, name + ".sha256sum")
    return Release("deno", data["tag_name"].lstrip("v"), name, asset["browser_download_url"],
                   sums["browser_download_url"] if sums else "", _digest(asset), asset.get("updated_at", ""))


_FF_REL = re.compile(r"ffmpeg-n(\d+)\.(\d+)-latest-win64-gpl-\d+\.\d+\.zip")


def latest_ffmpeg(http) -> Release:
    """Сборка FFmpeg (ffmpeg.exe + ffprobe.exe из одного архива) — BtbN/FFmpeg-Builds, рекомендуемая yt-dlp."""
    data = _gh(http, "BtbN/FFmpeg-Builds", "latest")
    best = None
    for a in data.get("assets", []):
        m = _FF_REL.fullmatch(a.get("name", ""))
        if m:
            ver = (int(m.group(1)), int(m.group(2)))
            if best is None or ver > best[0]:
                best = (ver, a)
    if best:
        asset, version = best[1], f"{best[0][0]}.{best[0][1]}"
    else:
        asset = _asset(data, "ffmpeg-master-latest-win64-gpl.zip")
        version = "master"
    if not asset:
        raise UpdateError("В релизе FFmpeg-Builds нет подходящего win64-архива")
    sums = _asset(data, "checksums.sha256")
    return Release("ffmpeg", version, asset["name"], asset["browser_download_url"],
                   sums["browser_download_url"] if sums else "", _digest(asset), asset.get("updated_at", ""))


def latest_node(http) -> Release:
    items = http.get_json("https://nodejs.org/dist/index.json")
    for item in items:
        if item.get("lts") and "win-x64-zip" in item.get("files", []) and vkey(item["version"])[0] >= 22:
            v = item["version"]
            name = f"node-{v}-win-x64.zip"
            return Release("node", v.lstrip("v"), name, f"https://nodejs.org/dist/{v}/{name}",
                           f"https://nodejs.org/dist/{v}/SHASUMS256.txt")
    raise UpdateError("Не найдена подходящая LTS-версия Node.js")


def latest_ejs(http) -> Release:
    data = _gh(http, "yt-dlp/ejs")
    return Release("ejs", data["tag_name"].lstrip("v"), "", "")


@dataclass
class Spec:
    key: str
    title: str
    files: tuple[str, ...]              # исполняемые файлы, которые нужны из архива
    kind: str                           # "single" | "zip" | "virtual"
    install_key: str = ""               # ключ реальной установки для виртуальных компонентов
    optional: bool = False


SPECS: dict[str, Spec] = {
    "ytdlp": Spec("ytdlp", "yt-dlp (движок загрузки)", ("yt-dlp",), "single"),
    "ejs": Spec("ejs", "yt-dlp EJS (решатель JS-проверок)", (), "virtual", "ytdlp"),
    "deno": Spec("deno", "Deno (JavaScript runtime)", ("deno",), "zip"),
    "ffmpeg": Spec("ffmpeg", "FFmpeg", ("ffmpeg", "ffprobe"), "zip"),
    "ffprobe": Spec("ffprobe", "FFprobe", (), "virtual", "ffmpeg"),
    "node": Spec("node", "Node.js (доп. JS runtime)", ("node",), "zip", optional=True),
}
ORDER = ["ytdlp", "ejs", "deno", "ffmpeg", "ffprobe", "node"]


@dataclass
class Resolved:
    path: Path | None
    source: str  # managed | bundled | legacy | system | missing


@dataclass
class ComponentStatus:
    key: str
    title: str
    installed: str = ""
    latest: str = ""
    path: str = ""
    source: str = "missing"
    healthy: bool = False
    health: str = ""
    checked_at: str = ""
    update_available: bool = False
    note: str = ""
    checksum: str = ""

    def to_dict(self) -> dict:
        return dict(self.__dict__)


class ComponentManager:
    def __init__(self, root: Path | None = None, http=None, settings=None,
                 busy_check: Callable[[], bool] | None = None,
                 providers: dict[str, Callable] | None = None,
                 bundled: Path | None = None, legacy: list[Path] | None = None):
        self.root = root or paths.components_dir()
        self.root.mkdir(parents=True, exist_ok=True)
        self.http = http or HttpClient()
        self.settings = settings
        self.busy_check = busy_check or (lambda: False)
        self.bundled = bundled if bundled is not None else paths.bundled_dir()
        self.legacy = legacy if legacy is not None else paths.legacy_component_dirs()
        self.providers = providers or {
            "ytdlp": lambda http, **kw: latest_ytdlp(http, kw.get("channel", "stable")),
            "deno": lambda http, **kw: latest_deno(http),
            "ffmpeg": lambda http, **kw: latest_ffmpeg(http),
            "node": lambda http, **kw: latest_node(http),
            "ejs": lambda http, **kw: latest_ejs(http),
        }
        self._lock = threading.RLock()
        self._op_lock = threading.Lock()
        self._probe_cache: dict[tuple, str] = {}

    # -- состояние на диске
    @property
    def _state_path(self) -> Path:
        return self.root / "state.json"

    def _load(self) -> dict:
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("components", {})
                data.setdefault("latest", {})
                data.setdefault("history", [])
                return data
        except (OSError, ValueError):
            pass
        return {"components": {}, "latest": {}, "history": []}

    def _save(self, state: dict) -> None:
        tmp = self._state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._state_path)

    def _log(self, state: dict, key: str, action: str, ok: bool, message: str, old: str = "", new: str = "") -> None:
        state["history"].append({
            "time": datetime.now().isoformat(timespec="seconds"), "component": key, "action": action,
            "ok": ok, "from": old, "to": new, "message": message,
        })
        state["history"] = state["history"][-200:]

    def history(self) -> list[dict]:
        with self._lock:
            return list(reversed(self._load()["history"]))

    # -- поиск исполняемых файлов
    def _managed_exe(self, key: str) -> Path | None:
        if key == "ejs":
            return None
        base = SPECS[key].install_key or key
        info = self._load()["components"].get(base)
        if not info or not info.get("active"):
            return None
        path = self.root / base / info["active"] / exe_name(key)
        return path if path.is_file() else None

    def resolve(self, key: str) -> Resolved:
        if key == "ejs":
            r = self.resolve("ytdlp")
            return Resolved(r.path, r.source)
        managed = self._managed_exe(key)
        bundled = self.bundled / exe_name(key)
        bundled = bundled if bundled.is_file() else None
        if managed and bundled and key in ("ytdlp", "deno", "node"):
            if is_newer(self.probe(key, bundled), self.probe(key, managed)):
                return Resolved(bundled, "bundled")
        if managed:
            return Resolved(managed, "managed")
        if bundled:
            return Resolved(bundled, "bundled")
        if key in ("ffmpeg", "ffprobe"):
            for d in self.legacy:
                p = d / exe_name(key)
                if p.is_file():
                    return Resolved(p, "legacy")
        if self.settings is not None and self.settings.get("allow_system_path", False):
            found = shutil.which("yt-dlp" if key == "ytdlp" else key)
            if found:
                return Resolved(Path(found), "system")
        return Resolved(None, "missing")

    def path(self, key: str) -> Path | None:
        return self.resolve(key).path

    def ffmpeg_dir(self) -> Path | None:
        """Каталог, где лежат И ffmpeg, И ffprobe (одна совместимая сборка)."""
        ff, fp = self.resolve("ffmpeg"), self.resolve("ffprobe")
        if ff.path and fp.path and ff.path.parent == fp.path.parent:
            return ff.path.parent
        return ff.path.parent if ff.path else None

    def probe(self, key: str, path: Path) -> str:
        """Версия по запуску файла (результат кэшируется по пути/размеру/времени)."""
        try:
            st = path.stat()
        except OSError:
            return ""
        ck = (key, str(path), st.st_size, int(st.st_mtime))
        if ck in self._probe_cache:
            return self._probe_cache[ck]
        arg = "-version" if key in ("ffmpeg", "ffprobe") else "--version"
        code, out = run_capture([str(path), arg], timeout=20)
        version = parse_version(key, out) if code == 0 else ""
        self._probe_cache[ck] = version
        return version

    # -- проверка работоспособности
    def health(self, key: str) -> tuple[bool, str]:
        res = self.resolve(key)
        if not res.path:
            return False, "Не найден"
        if key == "ejs":
            ok, msg = self.health("ytdlp")
            return ok, ("Встроен в yt-dlp; " if ok else "") + msg
        arg = "-version" if key in ("ffmpeg", "ffprobe") else "--version"
        code, out = run_capture([str(res.path), arg], timeout=20)
        version = parse_version(key, out)
        if code != 0 or not version:
            return False, f"Не запускается или неверный вывод (код {code}): {clean(out)[:200]}"
        if key == "deno":
            code, out = run_capture([str(res.path), "eval", "console.log(40+2)"], timeout=30)
            if code != 0 or "42" not in out:
                return False, f"Deno не выполняет JavaScript: {clean(out)[:200]}"
        elif key == "node":
            code, out = run_capture([str(res.path), "-e", "console.log(40+2)"], timeout=30)
            if code != 0 or "42" not in out:
                return False, f"Node.js не выполняет JavaScript: {clean(out)[:200]}"
        elif key == "ffmpeg":
            code, out = run_capture([str(res.path), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                                     "color=c=black:s=64x64:d=0.1", "-f", "null", "-"], timeout=30)
            if code != 0:
                return False, f"Тестовое кодирование не удалось: {clean(out)[:200]}"
        return True, f"Работает, версия {version}"

    # -- проверка обновлений
    def _channel(self) -> str:
        return (self.settings.get("channel", "stable") if self.settings is not None else "stable")

    def fetch_latest(self, key: str) -> Release:
        base = SPECS[key].install_key or key
        if key == "ejs":
            base = "ejs"
        provider = self.providers[base if key != "ffprobe" else "ffmpeg"]
        return provider(self.http, channel=self._channel())

    def check_latest(self, keys: list[str] | None = None) -> dict[str, str]:
        """Запрашивает последние версии; ошибки сети возвращаются в словаре (key → текст ошибки)."""
        errors: dict[str, str] = {}
        done: dict[str, Release] = {}
        for key in keys or ORDER:
            base = key if key not in ("ffprobe",) else "ffmpeg"
            try:
                rel = done.get(base) or self.fetch_latest(base)
                done[base] = rel
                with self._lock:
                    state = self._load()
                    state["latest"][key] = {"version": rel.version, "stamp": rel.stamp, "channel": rel.channel,
                                            "checked_at": datetime.now().isoformat(timespec="seconds")}
                    self._save(state)
            except Exception as exc:  # noqa: BLE001 — сеть/JSON; показываем пользователю
                errors[key] = clean(str(exc))
        return errors

    def statuses(self) -> list[ComponentStatus]:
        state = self._load()
        result: list[ComponentStatus] = []
        for key in ORDER:
            spec = SPECS[key]
            res = self.resolve(key)
            st = ComponentStatus(key, spec.title, path=str(res.path) if res.path else "", source=res.source)
            if res.path and key != "ejs":
                st.installed = self.probe(key, res.path)
            ok, msg = self.health(key) if res.path else (False, "Не установлен" + (" (необязательный)" if spec.optional else ""))
            st.healthy, st.health = ok, msg
            latest = state["latest"].get(key, {})
            st.latest, st.checked_at = latest.get("version", ""), latest.get("checked_at", "")
            base = spec.install_key or key
            meta = self._meta(state, base)
            st.checksum = meta.get("checksum_status", "")
            if key == "ejs":
                st.installed = self._ejs_version() or ("встроен в yt-dlp" if res.path else "")
                st.update_available = bool(st.latest and self._ejs_version() and is_newer(st.latest, self._ejs_version()))
                st.note = "Обновляется вместе с yt-dlp"
            elif latest:
                st.update_available = self._needs_update(key, st.installed, latest, meta, res)
            result.append(st)
        return result

    def _meta(self, state: dict, base: str) -> dict:
        info = state["components"].get(base) or {}
        return (info.get("meta") or {}).get(info.get("active", ""), {})

    def _needs_update(self, key: str, installed: str, latest: dict, meta: dict, res: Resolved) -> bool:
        if not res.path:
            return not SPECS[key].optional
        if key == "ytdlp":
            if res.source == "managed" and meta.get("channel") and meta["channel"] != self._channel():
                return True
            return is_newer(latest.get("version", ""), installed)
        if key in ("ffmpeg", "ffprobe"):
            if res.source == "managed" and meta.get("stamp"):
                return bool(latest.get("stamp")) and latest["stamp"] != meta["stamp"]
            return is_newer(latest.get("version", ""), installed) if re.match(r"n?\d", installed or "") else False
        return is_newer(latest.get("version", ""), installed)

    def _ejs_version(self) -> str:
        return self._load().get("ejs_version", "")

    def record_ejs_version(self, version: str) -> None:
        with self._lock:
            state = self._load()
            if version and state.get("ejs_version") != version:
                state["ejs_version"] = version
                self._save(state)

    # -- установка
    def _guard(self) -> None:
        if self.busy_check():
            raise ComponentBusyError("Идут загрузки видео. Дождитесь их завершения или остановите очередь перед обновлением.")

    def update(self, key: str, *, force: bool = False, progress: ProgressCb | None = None,
               cancel: threading.Event | None = None) -> str:
        """Устанавливает последнюю версию компонента. Возвращает сообщение для пользователя."""
        spec = SPECS[key]
        if spec.kind == "virtual":
            if key == "ejs":
                return self.update("ytdlp", force=force, progress=progress, cancel=cancel) + " (EJS обновляется вместе с yt-dlp)"
            return self.update("ffmpeg", force=force, progress=progress, cancel=cancel) + " (FFprobe обновляется вместе с FFmpeg)"
        self._guard()
        if not self._op_lock.acquire(blocking=False):
            raise UpdateError("Другое обновление уже выполняется")
        try:
            self._guard()
            return self._install(key, force, progress, cancel)
        finally:
            self._op_lock.release()

    def update_all(self, progress: ProgressCb | None = None, cancel: threading.Event | None = None) -> list[tuple[str, bool, str]]:
        results = []
        for key in ("ytdlp", "deno", "ffmpeg"):
            try:
                results.append((key, True, self.update(key, progress=progress, cancel=cancel)))
            except Cancelled:
                results.append((key, False, "Отменено"))
                break
            except UpdateError as exc:
                results.append((key, False, str(exc)))
        return results

    def _install(self, key: str, force: bool, progress: ProgressCb | None, cancel: threading.Event | None) -> str:
        spec = SPECS[key]
        say = (lambda m: progress(0, None, m)) if progress else (lambda m: None)
        say(f"{spec.title}: поиск последней версии…")
        rel = self.fetch_latest(key)
        with self._lock:
            state = self._load()
        info = state["components"].get(key, {})
        old_active = info.get("active", "")
        dirname = (f"nightly-{rel.version}" if rel.channel == "nightly" else rel.version)
        if rel.stamp and key == "ffmpeg":
            dirname = f"{rel.version}-{re.sub(r'[^0-9]', '', rel.stamp)[:12]}"
        meta_old = (info.get("meta") or {}).get(old_active, {})
        if not force and old_active and meta_old.get("stamp", "") == rel.stamp and old_active == dirname and (self.root / key / dirname).exists():
            return f"{spec.title}: уже установлена последняя версия ({rel.version})"

        tmp_root = self.root / ".tmp"
        tmp_root.mkdir(exist_ok=True)
        part = tmp_root / (rel.asset_name + ".part")
        staging = self.root / key / f".staging-{int(time.time())}"
        final = self.root / key / dirname
        try:
            say(f"{spec.title}: загрузка {rel.asset_name}…")
            self.http.download(rel.url, part, progress, cancel)
            checksum_status = self._verify_checksum(rel, part, say)
            staging.mkdir(parents=True, exist_ok=True)
            self._extract(spec, rel, part, staging)
            say(f"{spec.title}: проверка работоспособности новой версии…")
            version = self._verify_staging(spec, staging)
            self._guard()  # на случай, если загрузки начались во время скачивания
            if final.exists():
                shutil.rmtree(final, ignore_errors=True)
            os.replace(staging, final)
        except Exception as exc:
            shutil.rmtree(staging, ignore_errors=True)
            with self._lock:
                st = self._load()
                self._log(st, key, "update", False, clean(str(exc)), old_active, rel.version)
                self._save(st)
            if isinstance(exc, UpdateError):
                raise
            raise UpdateError(f"{spec.title}: {clean(str(exc))}") from exc
        finally:
            try:
                part.unlink()
            except OSError:
                pass

        with self._lock:
            state = self._load()
            entry = state["components"].setdefault(key, {"active": "", "previous": [], "meta": {}})
            prev = entry.get("active", "")
            if prev and prev != dirname:
                entry["previous"] = [p for p in [prev] + entry.get("previous", []) if p != dirname][:3]
            entry["active"] = dirname
            entry.setdefault("meta", {})[dirname] = {
                "version": version, "channel": rel.channel, "stamp": rel.stamp, "source": rel.url,
                "checksum_status": checksum_status, "installed_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._save(state)
        # итоговая проверка активной версии; при неудаче — автоматический откат
        self._probe_cache.clear()
        ok, msg = self.health(key)
        if not ok:
            self.rollback(key, _auto=True, reason=msg)
            raise UpdateError(f"{spec.title}: новая версия не прошла проверку ({msg}); выполнен автоматический откат")
        with self._lock:
            state = self._load()
            self._prune(state, key)
            self._log(state, key, "update" if not force else "reinstall", True,
                      f"Установлена {version}; контрольная сумма: {checksum_status}", old_active, dirname)
            self._save(state)
        return f"{spec.title}: установлена версия {version} (контрольная сумма: {checksum_status})"

    def _verify_checksum(self, rel: Release, file: Path, say) -> str:
        expected = ""
        if rel.checksum_url:
            try:
                expected = parse_checksums(self.http.get_text(rel.checksum_url), rel.asset_name)
            except UpdateError:
                expected = ""
        source = "контрольные суммы разработчика"
        if not expected and rel.digest:
            expected, source = rel.digest, "SHA-256 из метаданных GitHub"
        if not expected:
            say("Контрольная сумма для этого файла не опубликована — проверка пропущена")
            return "не проверена (не опубликована)"
        actual = sha256_file(file)
        if actual.lower() != expected.lower():
            raise UpdateError(f"Контрольная сумма не совпала (ожидалась {expected[:12]}…, получена {actual[:12]}…). Файл отброшен.")
        say(f"SHA-256 проверена ({source})")
        return f"проверена ({source})"

    def _extract(self, spec: Spec, rel: Release, archive: Path, staging: Path) -> None:
        if spec.kind == "single":
            shutil.copy2(archive, staging / exe(spec.files[0]))
            self._chmod(staging / exe(spec.files[0]))
            return
        wanted = {exe(f).lower(): f for f in spec.files}
        found: set[str] = set()
        with zipfile.ZipFile(archive) as zf:
            if zf.testzip() is not None:
                raise UpdateError("Архив повреждён")
            for info in zf.infolist():
                if info.is_dir():
                    continue
                base = Path(info.filename).name  # выравниваем: защита от path traversal
                low = base.lower()
                if low in wanted or (low.startswith("license") and low.endswith(".txt") and "license" not in found):
                    target = staging / base
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    self._chmod(target)
                    found.add(low if low in wanted else "license")
        missing = [f for f in wanted if f not in found]
        if missing:
            raise UpdateError("В архиве нет файлов: " + ", ".join(missing))

    @staticmethod
    def _chmod(path: Path) -> None:
        if os.name != "nt":
            path.chmod(0o755)

    def _verify_staging(self, spec: Spec, staging: Path) -> str:
        version = ""
        for f in spec.files:
            arg = "-version" if f in ("ffmpeg", "ffprobe") else "--version"
            code, out = run_capture([str(staging / exe(f)), arg], timeout=30)
            v = parse_version(f, out) if code == 0 else ""
            if not v:
                raise UpdateError(f"{f} не запускается после распаковки: {clean(out)[:200]}")
            version = version or v
        return version

    def _prune(self, state: dict, key: str) -> None:
        entry = state["components"].get(key, {})
        keep = {entry.get("active", "")} | set(entry.get("previous", []))
        base = self.root / key
        if base.is_dir():
            for d in base.iterdir():
                if d.is_dir() and d.name not in keep:
                    shutil.rmtree(d, ignore_errors=True)
        entry["meta"] = {k: v for k, v in entry.get("meta", {}).items() if k in keep}

    # -- откат
    def rollback(self, key: str, *, _auto: bool = False, reason: str = "") -> str:
        spec = SPECS[key]
        base = spec.install_key or key
        if not _auto:
            self._guard()
        with self._lock:
            state = self._load()
            entry = state["components"].get(base)
            if not entry or not entry.get("previous"):
                raise UpdateError("Нет предыдущей версии для отката")
            failed = entry.get("active", "")
            target = None
            for cand in entry["previous"]:
                if (self.root / base / cand).is_dir():
                    target = cand
                    break
            if not target:
                raise UpdateError("Файлы предыдущей версии не найдены")
            entry["previous"] = [p for p in entry["previous"] if p != target]
            entry["active"] = target
            self._log(state, base, "auto-rollback" if _auto else "rollback", True,
                      reason or "Откат по запросу пользователя", failed, target)
            self._save(state)
        self._probe_cache.clear()
        return f"{SPECS[base].title}: выполнен откат к версии {target}"

    def can_rollback(self, key: str) -> bool:
        base = SPECS[key].install_key or key
        entry = self._load()["components"].get(base) or {}
        return any((self.root / base / p).is_dir() for p in entry.get("previous", []))

    def open_dir(self, key: str) -> Path:
        res = self.resolve(key)
        return res.path.parent if res.path else self.root

    # -- окружение yt-dlp
    def ytdlp_environment(self, ffmpeg_dir: Path | None = None) -> dict:
        """Запрашивает у yt-dlp его «debug header»: версии EJS, ffmpeg, deno. Лучшая попытка, без исключений."""
        yt = self.path("ytdlp")
        info: dict = {"ejs": "", "exe_versions": "", "js_runtimes": "", "curl_cffi": False, "raw": ""}
        if not yt:
            return info
        cmd = [str(yt), "--ignore-config", "-v", "--list-impersonate-targets"]
        if ffmpeg_dir:
            cmd += ["--ffmpeg-location", str(ffmpeg_dir)]
        code, out = run_capture(cmd, timeout=40)
        info["raw"] = clean(out)[:3000]
        m = re.search(r"yt[_-]dlp[_-]ejs[-_ ]?v?(\d[\d.]*)", out, re.I)
        if m:
            info["ejs"] = m.group(1)
            self.record_ejs_version(m.group(1))
        m = re.search(r"exe versions:\s*(.+)", out)
        if m:
            info["exe_versions"] = m.group(1).strip()
        m = re.search(r"JS runtimes?:\s*(.+)", out)
        if m:
            info["js_runtimes"] = m.group(1).strip()
        info["curl_cffi"] = bool(re.search(r"curl[_-]cffi", out, re.I))
        return info
