"""Построение строк формата yt-dlp по настройкам пользователя (чистые функции, без запуска процессов)."""
from __future__ import annotations

from dataclasses import dataclass, field

QUALITIES = {"best": "Максимальное", "2160": "4K (2160p)", "1440": "1440p", "1080": "1080p",
             "720": "720p", "480": "480p", "360": "360p"}
CONTAINERS = {"mp4": "MP4", "mkv": "MKV", "webm": "WebM"}
CODECS = {"auto": "Автоматически", "h264": "H.264", "h265": "H.265 (HEVC)", "av1": "AV1"}
MODES = {"video": "Видео + звук", "video_only": "Только видео", "audio": "Только аудио"}
AUDIO_FORMATS = {"mp3": "MP3", "wav": "WAV", "m4a": "M4A"}

_CODEC_FILTER = {
    "h264": "[vcodec^=avc1]",
    "h265": "[vcodec~='^(hev1|hvc1|h265|hevc)']",
    "av1": "[vcodec^=av01]",
}
_AUDIO_FILTER = {"mp4": "[acodec^=mp4a]", "webm": "[acodec~='^(opus|vorbis)']", "mkv": ""}


@dataclass
class FormatPlan:
    format: str
    sort: str = ""
    merge: str = ""
    audio_format: str = ""
    notes: list[str] = field(default_factory=list)  # сообщения о перекодировании и т.п.


def build_plan(container: str = "mp4", quality: str = "best", codec: str = "auto",
               mode: str = "video", audio_format: str = "mp3") -> FormatPlan:
    height = f"[height<={quality}]" if quality and quality != "best" else ""
    cf = _CODEC_FILTER.get(codec, "")

    if mode == "audio":
        notes = []
        if audio_format == "m4a":
            fmt = "ba[ext=m4a]/ba/b"
            notes.append("Аудио: при отсутствии M4A-дорожки потребуется перекодирование в M4A.")
        else:
            fmt = "ba/b"
            notes.append(f"Аудио: перекодирование в {audio_format.upper()} необходимо для выбранного формата.")
        return FormatPlan(fmt, audio_format=audio_format, notes=notes)

    if mode == "video_only":
        parts = []
        if cf:
            parts.append(f"bv*{cf}{height}")
        parts += [f"bv*{height}", f"b{height}", "b"]
        return FormatPlan("/".join(dict.fromkeys(parts)))

    af = _AUDIO_FILTER.get(container, "")
    parts = []
    if cf:
        parts += [f"bv*{cf}{height}+ba{af}", f"bv*{cf}{height}+ba"]
    if af and not cf:
        parts.append(f"bv*{height}+ba{af}")
    parts += [f"bv*{height}+ba", f"b{height}", "b"]
    fmt = "/".join(dict.fromkeys(parts))

    sort = ""
    if codec == "auto":
        if container == "mp4":
            sort = "res,vcodec:h264,acodec:m4a"      # без перекодирования: предпочитаем совместимые с MP4 потоки
        elif container == "webm":
            sort = "res,vcodec:vp9,acodec:opus"
    merge = {"mp4": "mp4/mkv", "mkv": "mkv", "webm": "webm/mkv"}.get(container, "mkv")
    notes = []
    if container == "mp4":
        notes.append("MP4: выбираются совместимые потоки без перекодирования; если подходящих нет — файл будет сохранён в MKV.")
    return FormatPlan(fmt, sort, merge, notes=notes)
