"""Проверка итогового файла через FFprobe и подготовка команды совместимости."""
from __future__ import annotations

import json
from pathlib import Path

from .components import run_capture


def probe_file(ffprobe: Path, file: Path, timeout: float = 60.0) -> dict:
    code, out = run_capture([str(ffprobe), "-v", "error", "-show_format", "-show_streams", "-of", "json", str(file)], timeout)
    if code != 0:
        raise RuntimeError(f"ffprobe завершился с кодом {code}: {out[-300:]}")
    start = out.find("{")
    try:
        return json.loads(out[start:])
    except ValueError as exc:
        raise RuntimeError("ffprobe вернул некорректный JSON") from exc


def summarize(info: dict) -> dict:
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")), None)
    audio = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = info.get("format", {})
    try:
        duration = float(fmt.get("duration") or (video or audio or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "container": (fmt.get("format_name") or "").split(",")[0],
        "duration": duration,
        "size": int(fmt.get("size") or 0),
        "video_codec": (video or {}).get("codec_name", ""),
        "pix_fmt": (video or {}).get("pix_fmt", ""),
        "width": (video or {}).get("width", 0),
        "height": (video or {}).get("height", 0),
        "audio_codec": (audio or {}).get("codec_name", ""),
        "has_video": video is not None,
        "has_audio": audio is not None,
    }


def validate(summary: dict, mode: str) -> list[str]:
    """Возвращает список проблем итогового файла (пустой список — файл в порядке)."""
    problems = []
    if mode in ("video", "video_only") and not summary["has_video"]:
        problems.append("В файле нет видеодорожки")
    if mode in ("video", "audio") and not summary["has_audio"]:
        problems.append("В файле нет аудиодорожки")
    if summary["duration"] <= 0:
        problems.append("Не удалось определить длительность файла")
    return problems


def mp4_compat_issues(summary: dict) -> list[str]:
    """Что мешает универсальной совместимости MP4 (H.264 + AAC, yuv420p)."""
    issues = []
    if summary["has_video"]:
        if summary["video_codec"] != "h264":
            issues.append(f"видеокодек {summary['video_codec'] or '?'} (нужен H.264)")
        elif summary["pix_fmt"] and summary["pix_fmt"] != "yuv420p":
            issues.append(f"формат пикселей {summary['pix_fmt']} (нужен yuv420p)")
    if summary["has_audio"] and summary["audio_codec"] != "aac":
        issues.append(f"аудиокодек {summary['audio_codec']} (нужен AAC)")
    return issues


def build_compat_cmd(ffmpeg: Path, src: Path, dst: Path, summary: dict) -> tuple[list[str], str]:
    """Команда ffmpeg: потоки, которые уже совместимы, копируются без перекодирования."""
    video_ok = summary["video_codec"] == "h264" and summary["pix_fmt"] in ("", "yuv420p")
    audio_ok = summary["audio_codec"] == "aac"
    cmd = [str(ffmpeg), "-hide_banner", "-nostdin", "-loglevel", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a?"]
    if video_ok:
        cmd += ["-c:v", "copy"]
    else:
        cmd += ["-c:v", "libx264", "-preset", "medium", "-crf", "21", "-pix_fmt", "yuv420p"]
    if audio_ok:
        cmd += ["-c:a", "copy"]
    else:
        cmd += ["-c:a", "aac", "-b:a", "160k"]
    cmd += ["-movflags", "+faststart", str(dst)]
    what = []
    if not video_ok:
        what.append("видео → H.264")
    if not audio_ok:
        what.append("аудио → AAC")
    return cmd, ", ".join(what) or "только упаковка контейнера"
