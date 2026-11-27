"""Движок загрузки: запускает внешний yt-dlp как отдельный процесс.

yt-dlp не импортируется в приложение — его версия не зависит от версии программы,
а обновляется менеджером компонентов без переустановки.
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .components import clean_env, no_window_flags
from .errors import ErrorInfo, classify
from .formats import build_plan
from .redact import clean

PROGRESS_PREFIX = "VDSP|"
PROGRESS_TEMPLATE = (
    "download:" + PROGRESS_PREFIX +
    "%(progress.status)s|%(progress.downloaded_bytes)s|%(progress.total_bytes)s|%(progress.total_bytes_estimate)s|"
    "%(progress.speed)s|%(progress.eta)s|%(progress.fragment_index)s|%(progress.fragment_count)s"
)


@dataclass
class Tools:
    ytdlp: Path | None
    ffmpeg: Path | None = None
    ffprobe: Path | None = None
    deno: Path | None = None
    node: Path | None = None

    @property
    def ffmpeg_dir(self) -> Path | None:
        return self.ffmpeg.parent if self.ffmpeg else (self.ffprobe.parent if self.ffprobe else None)


@dataclass
class JobOptions:
    url: str
    out_dir: str
    platform: str = "other"
    container: str = "mp4"
    quality: str = "best"
    video_codec: str = "auto"
    mode: str = "video"
    audio_format: str = "mp3"
    subtitles: bool = False
    sub_langs: str = "ru,en"
    thumbnails: bool = False
    template: str = "%(title).160B [%(id)s].%(ext)s"
    rate_limit: str = ""
    retries: int = 5
    stall_timeout: int = 180
    probe_after: bool = True
    ios_compat: bool = False
    ejs_remote: bool = False
    impersonate: str = ""
    cookie_args: list[str] = field(default_factory=list)
    extra_args: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict) -> "JobOptions":
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def build_command(opts: JobOptions, tools: Tools, *, use_impersonate: bool = True, remote_ejs: bool | None = None) -> list[str]:
    if not tools.ytdlp:
        raise RuntimeError("yt-dlp не найден")
    plan = build_plan(opts.container, opts.quality, opts.video_codec, opts.mode, opts.audio_format)
    cmd = [str(tools.ytdlp), "--ignore-config", "--color", "never", "--newline", "--no-playlist",
           "--windows-filenames", "--no-overwrites", "--continue",
           "--progress-template", PROGRESS_TEMPLATE,
           "--retries", str(max(0, opts.retries)), "--fragment-retries", str(max(0, opts.retries)),
           "--socket-timeout", "30", "--concurrent-fragments", "4",
           "-o", str(Path(opts.out_dir) / opts.template),
           "-f", plan.format]
    if plan.sort:
        cmd += ["-S", plan.sort]
    if opts.mode != "audio":
        if plan.merge:
            cmd += ["--merge-output-format", plan.merge]
    else:
        cmd += ["-x", "--audio-format", plan.audio_format, "--audio-quality", "0"]
    if tools.ffmpeg_dir:
        cmd += ["--ffmpeg-location", str(tools.ffmpeg_dir)]
    runtimes = []
    if tools.deno:
        runtimes.append(f"deno:{tools.deno}")
    if tools.node:
        runtimes.append(f"node:{tools.node}")
    if runtimes:
        cmd.append("--no-js-runtimes")      # игнорируем системные runtime: используем только свои
        for r in runtimes:
            cmd += ["--js-runtimes", r]
    if (opts.ejs_remote if remote_ejs is None else remote_ejs):
        cmd += ["--remote-components", "ejs:github"]
    if opts.subtitles and opts.mode != "audio":
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", opts.sub_langs or "ru,en", "--convert-subs", "srt"]
    if opts.thumbnails:
        cmd += ["--write-thumbnail", "--convert-thumbnails", "jpg"]
    if opts.rate_limit:
        cmd += ["-r", opts.rate_limit]
    if opts.impersonate and use_impersonate:
        cmd += ["--impersonate", opts.impersonate]
    cmd += list(opts.cookie_args)
    cmd += list(opts.extra_args)
    cmd += ["--", opts.url]
    return cmd


# --- разбор вывода -----------------------------------------------------------------

@dataclass
class Progress:
    status: str = ""
    percent: float = 0.0
    downloaded: int = 0
    total: int = 0
    speed: float = 0.0
    eta: int = -1


def _num(value: str, cast=float):
    try:
        return cast(float(value)) if value not in ("", "NA", "None") else None
    except ValueError:
        return None


def parse_progress(line: str) -> Progress | None:
    if not line.startswith(PROGRESS_PREFIX):
        return None
    parts = line[len(PROGRESS_PREFIX):].split("|")
    if len(parts) < 8:
        return None
    status, done, total, est, speed, eta, fidx, fcount = parts[:8]
    p = Progress(status=status)
    p.downloaded = _num(done, int) or 0
    p.total = _num(total, int) or _num(est, int) or 0
    p.speed = _num(speed) or 0.0
    p.eta = _num(eta, int) if _num(eta, int) is not None else -1
    if p.total:
        p.percent = min(100.0, p.downloaded * 100.0 / p.total)
    elif _num(fcount, int):
        p.percent = min(100.0, (_num(fidx, int) or 0) * 100.0 / _num(fcount, int))
    if status == "finished":
        p.percent = 100.0
    return p


_DEST_RX = [
    ("final", re.compile(r'^\[Merger\] Merging formats into "(.+)"$')),
    ("final", re.compile(r"^\[ExtractAudio\] Destination: (.+)$")),
    ("final", re.compile(r"^\[download\] (.+) has already been downloaded$")),
    ("final", re.compile(r'^\[MoveFiles\] Moving file ".+" to "(.+)"$')),
    ("final", re.compile(r"^\[VideoConvertor\] Converting video from \S+ to \S+; Destination: (.+)$")),
    ("part", re.compile(r"^\[download\] Destination: (.+)$")),
]
_SIDE_EXT = {".vtt", ".srt", ".jpg", ".jpeg", ".webp", ".png", ".json", ".part", ".ytdl", ".description"}


def parse_destination(line: str) -> tuple[str, str] | None:
    for kind, rx in _DEST_RX:
        m = rx.match(line.strip())
        if m:
            path = m.group(1).strip().strip('"')
            if Path(path).suffix.lower() in _SIDE_EXT:
                return None
            return kind, path
    return None


# --- процесс -----------------------------------------------------------------------

class JobHandle:
    """Управление запущенным процессом: пауза (остановка с сохранением .part) и отмена."""

    def __init__(self):
        self.proc: subprocess.Popen | None = None
        self.cancel_requested = False
        self.pause_requested = False
        self._lock = threading.Lock()

    def stop(self, *, pause: bool = False) -> None:
        with self._lock:
            self.pause_requested = pause
            self.cancel_requested = not pause
            proc = self.proc
        if proc is not None:
            kill_tree(proc)

    def attach(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self.proc = proc

    @property
    def stopped(self) -> bool:
        return self.cancel_requested or self.pause_requested


def kill_tree(proc: subprocess.Popen) -> None:
    """yt-dlp.exe (onefile) порождает дочерние процессы, поэтому завершаем всё дерево."""
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                           creationflags=no_window_flags(), timeout=15)
        else:
            proc.kill()
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


@dataclass
class JobResult:
    ok: bool = False
    state: str = "failed"            # done | failed | canceled | paused
    returncode: int | None = None
    filepath: str = ""
    title: str = ""
    size: int = 0
    error: ErrorInfo | None = None
    notes: list[str] = field(default_factory=list)
    probe: dict = field(default_factory=dict)


LogCb = Callable[[str], None]
ProgressCb = Callable[[Progress], None]


class Engine:
    def __init__(self, tools_provider: Callable[[], Tools]):
        self.tools_provider = tools_provider

    # -- один запуск процесса
    def _spawn(self, cmd: list[str], handle: JobHandle, on_line: Callable[[str], None], stall_timeout: int) -> int:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            env=clean_env(), creationflags=no_window_flags(), bufsize=0,
        )
        handle.attach(proc)
        if handle.stopped:
            kill_tree(proc)
        lines: "queue.Queue[bytes | None]" = queue.Queue()

        def reader():
            buf = b""
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                while True:
                    m = re.search(rb"[\r\n]", buf)
                    if not m:
                        break
                    lines.put(buf[:m.start()])
                    buf = buf[m.end():]
            if buf:
                lines.put(buf)
            lines.put(None)

        threading.Thread(target=reader, daemon=True).start()
        last = time.monotonic()
        stalled = False
        while True:
            try:
                item = lines.get(timeout=1.0)
            except queue.Empty:
                if stall_timeout and time.monotonic() - last > stall_timeout and not handle.stopped:
                    stalled = True
                    on_line("VDS_STALL_TIMEOUT: нет вывода от yt-dlp в течение %d с" % stall_timeout)
                    kill_tree(proc)
                    last = time.monotonic()
                continue
            if item is None:
                break
            last = time.monotonic()
            text = item.decode("utf-8", errors="replace")
            if text.strip():
                on_line(text)
        proc.wait()
        return 124 if stalled else (proc.returncode if proc.returncode is not None else -1)

    # -- полное задание
    def run(self, opts: JobOptions, handle: JobHandle, on_progress: ProgressCb | None = None,
            on_log: LogCb | None = None) -> JobResult:
        log = on_log or (lambda s: None)
        tools = self.tools_provider()
        result = JobResult()
        if not tools.ytdlp:
            result.error = classify("yt-dlp not found")
            result.error.title = "Не найден движок yt-dlp"
            result.error.cause = "Компонент yt-dlp не установлен."
            result.error.solution = "Откройте «Компоненты и обновления» и установите yt-dlp."
            result.error.actions = ["update_ytdlp"]
            return result
        Path(opts.out_dir).mkdir(parents=True, exist_ok=True)
        started = time.time()
        tail: deque[str] = deque(maxlen=500)
        state = {"final": "", "part": ""}

        def on_line(raw: str) -> None:
            line = clean(raw)
            prog = parse_progress(line)
            if prog is not None:
                if on_progress:
                    on_progress(prog)
                return
            tail.append(line)
            dest = parse_destination(line)
            if dest:
                state[dest[0]] = dest[1]
            log(line)

        use_imp, remote = True, opts.ejs_remote
        tried_imp = tried_ejs = False
        code = -1
        for _ in range(3):  # ограниченное число повторов: без бесконечных попыток
            cmd = build_command(opts, tools, use_impersonate=use_imp, remote_ejs=remote)
            tail.clear()
            code = self._spawn(cmd, handle, on_line, opts.stall_timeout)
            if handle.stopped:
                break
            if code == 0:
                break
            info = classify("\n".join(tail), code)
            if info.code == "IMPERSONATE_UNAVAILABLE" and opts.impersonate and not tried_imp:
                tried_imp, use_imp = True, False
                log("Имитация браузера недоступна — повтор без неё (однократно).")
                continue
            if info.code in ("JS_CHALLENGE", "EJS_INCOMPAT") and not remote and not tried_ejs:
                tried_ejs, remote = True, True
                log("Ошибка JavaScript-проверки — повтор с загрузкой EJS из GitHub (однократно).")
                continue
            break

        result.returncode = code
        if handle.cancel_requested:
            result.state = "canceled"
            return result
        if handle.pause_requested:
            result.state = "paused"
            return result
        if code != 0:
            result.error = classify("\n".join(tail), code)
            return result

        path = self._locate(state, opts, started)
        if not path:
            result.error = classify("Output file not found after download")
            result.error.title = "Файл не найден после загрузки"
            result.error.cause = "yt-dlp завершился успешно, но итоговый файл не обнаружен в папке загрузок."
            result.error.solution = "Проверьте папку загрузок и журнал; повторите загрузку."
            result.error.code = "NO_OUTPUT"
            return result
        result.filepath = str(path)
        result.title = re.sub(r"\s*\[[^\]]+\]$", "", path.stem)
        try:
            result.size = path.stat().st_size
        except OSError:
            pass
        result.ok, result.state = True, "done"
        self._postprocess(opts, tools, path, result, handle, log)
        return result

    @staticmethod
    def _locate(state: dict, opts: JobOptions, started: float) -> Path | None:
        for key in ("final", "part"):
            if state[key]:
                p = Path(state[key])
                if not p.is_absolute():
                    p = Path(opts.out_dir) / p
                if p.is_file():
                    return p
        # запасной вариант: самый свежий медиафайл, изменённый после старта
        best, best_t = None, started - 2
        for p in Path(opts.out_dir).glob("*"):
            if p.is_file() and p.suffix.lower() not in _SIDE_EXT and p.stat().st_mtime >= best_t:
                best, best_t = p, p.stat().st_mtime
        return best

    def _postprocess(self, opts: JobOptions, tools: Tools, path: Path, result: JobResult,
                     handle: JobHandle, log: LogCb) -> None:
        from . import probe  # локальный импорт: probe использует components

        want = opts.container if opts.mode != "audio" else opts.audio_format
        if opts.mode != "audio" and path.suffix.lstrip(".").lower() != want and opts.mode == "video":
            result.notes.append(f"Файл сохранён как .{path.suffix.lstrip('.')} вместо .{want}: потоки несовместимы с контейнером, перекодирование не выполнялось.")
        if not (opts.probe_after or opts.ios_compat):
            return
        if not tools.ffprobe:
            result.notes.append("FFprobe не найден — итоговая проверка файла пропущена. Установите FFprobe в «Компоненты и обновления».")
            return
        try:
            summary = probe.summarize(probe.probe_file(tools.ffprobe, path))
        except RuntimeError as exc:
            result.ok, result.state = False, "failed"
            result.error = classify("ffprobe error: " + str(exc))
            result.error.cause = "FFprobe не смог прочитать итоговый файл — он может быть повреждён."
            return
        result.probe = summary
        problems = probe.validate(summary, opts.mode)
        if problems:
            result.ok, result.state = False, "failed"
            result.error = classify("ffmpeg " + "; ".join(problems))
            result.error.title = "Итоговый файл не прошёл проверку"
            result.error.cause = "; ".join(problems)
            return
        if opts.mode == "video" and path.suffix.lower() == ".mp4":
            issues = probe.mp4_compat_issues(summary)
            if issues and opts.ios_compat and tools.ffmpeg:
                self._make_compatible(opts, tools, path, summary, result, handle, log)
            elif issues:
                result.notes.append("MP4 может не открыться на некоторых устройствах: " + "; ".join(issues)
                                    + ". Включите «Совместимость с iPhone», чтобы получить H.264 + AAC (перекодирование выполняется только при необходимости).")

    def _make_compatible(self, opts, tools: Tools, path: Path, summary: dict, result: JobResult,
                         handle: JobHandle, log: LogCb) -> None:
        from . import probe
        tmp = path.with_name(path.stem + ".vds_compat.mp4")
        cmd, what = probe.build_compat_cmd(tools.ffmpeg, path, tmp, summary)
        log(f"Перекодирование необходимо для совместимости: {what}.")
        out: list[str] = []
        code = self._spawn(cmd, handle, out.append, 0)
        if code != 0 or not tmp.exists() or tmp.stat().st_size == 0:
            if tmp.exists():
                tmp.unlink()
            result.notes.append("Не удалось сделать MP4 совместимым: " + clean(" ".join(out))[-300:])
            return
        os.replace(tmp, path)
        result.notes.append(f"Выполнено перекодирование для совместимости: {what}.")
        try:
            result.size = path.stat().st_size
            result.probe = probe.summarize(probe.probe_file(tools.ffprobe, path))
        except (OSError, RuntimeError):
            pass
