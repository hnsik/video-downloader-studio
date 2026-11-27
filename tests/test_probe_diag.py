import shutil
import subprocess
from pathlib import Path

import pytest

from vds import probe
from vds.components import ComponentManager
from vds.config import Settings
from vds.core import AppCore
from vds.diagnostics import FAIL, OK, Report, export, run_full
from vds.engine import Engine, JobHandle, JobOptions, Tools

FFMPEG, FFPROBE = shutil.which("ffmpeg"), shutil.which("ffprobe")
need_ff = pytest.mark.skipif(not (FFMPEG and FFPROBE), reason="ffmpeg/ffprobe недоступны")


def encoders():
    out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True).stdout
    return out


def make_video(path: Path, vcodec: str, acodec: str):
    cmd = [FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=128x96:r=10", "-f", "lavfi",
           "-i", "sine=d=1", "-c:v", vcodec, "-pix_fmt", "yuv420p", "-c:a", acodec, "-shortest", str(path)]
    subprocess.run(cmd, check=True)


@need_ff
def test_probe_summary_and_validation(tmp_path):
    f = tmp_path / "a.mp4"
    make_video(f, "mpeg4", "aac")
    s = probe.summarize(probe.probe_file(Path(FFPROBE), f))
    assert s["has_video"] and s["has_audio"] and s["duration"] > 0 and s["video_codec"] == "mpeg4"
    assert probe.validate(s, "video") == []
    assert any("H.264" in i for i in probe.mp4_compat_issues(s))


@need_ff
def test_probe_detects_missing_audio_and_garbage(tmp_path):
    f = tmp_path / "silent.mp4"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=d=1:s=64x64", "-c:v", "mpeg4", str(f)], check=True)
    s = probe.summarize(probe.probe_file(Path(FFPROBE), f))
    assert "В файле нет аудиодорожки" in probe.validate(s, "video")
    bad = tmp_path / "bad.mp4"
    bad.write_bytes(b"not a video")
    with pytest.raises(RuntimeError):
        probe.probe_file(Path(FFPROBE), bad)


def test_compat_cmd_copies_compatible_streams():
    s = {"video_codec": "h264", "pix_fmt": "yuv420p", "audio_codec": "opus"}
    cmd, what = probe.build_compat_cmd(Path("ffmpeg"), Path("a.mkv"), Path("b.mp4"), s)
    assert cmd[cmd.index("-c:v") + 1] == "copy" and cmd[cmd.index("-c:a") + 1] == "aac" and "AAC" in what
    s2 = {"video_codec": "vp9", "pix_fmt": "yuv420p", "audio_codec": "aac"}
    cmd2, what2 = probe.build_compat_cmd(Path("ffmpeg"), Path("a"), Path("b"), s2)
    assert cmd2[cmd2.index("-c:v") + 1] == "libx264" and cmd2[cmd2.index("-c:a") + 1] == "copy" and "H.264" in what2


@need_ff
@pytest.mark.skipif("libx264" not in (encoders() if FFMPEG else ""), reason="нет libx264")
def test_ios_compat_transcodes_only_when_needed(tmp_path, fake_ytdlp, monkeypatch):
    """Движок: после «скачивания» VP9-подобного (mpeg4) файла делается H.264+AAC; файл остаётся валидным."""
    out = tmp_path / "out"
    out.mkdir()
    src = out / "Test [abc123].mp4"
    make_video(src, "mpeg4", "aac")
    monkeypatch.setenv("FAKE_MODE", "keep")  # fake не перезаписывает файл
    script = tmp_path / "fake2"
    script.write_text(f"#!/bin/sh\necho '[Merger] Merging formats into \"{src}\"'\nexit 0\n")
    script.chmod(0o755)
    eng = Engine(lambda: Tools(script, Path(FFMPEG), Path(FFPROBE)))
    res = eng.run(JobOptions(url="u", out_dir=str(out), ios_compat=True, probe_after=True), JobHandle())
    assert res.ok, res.error
    assert any("перекодирование" in n.lower() for n in res.notes)
    assert res.probe["video_codec"] == "h264" and res.probe["audio_codec"] == "aac"


@need_ff
def test_engine_flags_corrupt_result(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    bad = out / "x [id].mp4"
    bad.write_bytes(b"garbage")
    script = tmp_path / "fake3"
    script.write_text(f"#!/bin/sh\necho '[Merger] Merging formats into \"{bad}\"'\nexit 0\n")
    script.chmod(0o755)
    eng = Engine(lambda: Tools(script, Path(FFMPEG), Path(FFPROBE)))
    res = eng.run(JobOptions(url="u", out_dir=str(out)), JobHandle())
    assert not res.ok and res.state == "failed"


def test_note_when_ffprobe_missing(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    f = out / "x [id].mp4"
    f.write_bytes(b"x")
    script = tmp_path / "fake4"
    script.write_text(f"#!/bin/sh\necho '[Merger] Merging formats into \"{f}\"'\nexit 0\n")
    script.chmod(0o755)
    res = Engine(lambda: Tools(script)).run(JobOptions(url="u", out_dir=str(out)), JobHandle())
    assert res.ok and any("FFprobe не найден" in n for n in res.notes)


def test_report_never_contains_secrets(tmp_path):
    rep = Report()
    rep.add("Тест", "cookies", "ok", "Cookie: SID=topsecret; url https://x/v?sig=SIGVALUE&id=1; token=abcd1234")
    rep.add("Тест", "path", "info", r"C:\Users\ivanov\Videos")
    for ext in (".txt", ".json"):
        target = tmp_path / ("r" + ext)
        export(rep, target)
        body = target.read_text(encoding="utf-8")
        assert "topsecret" not in body and "SIGVALUE" not in body and "abcd1234" not in body and "ivanov" not in body


def test_full_diagnostics_runs_on_empty_install(tmp_path):
    settings = Settings(tmp_path / "s.json").load()
    settings.set("download_dir", str(tmp_path / "dl"))
    cm = ComponentManager(root=tmp_path / "c", settings=settings, bundled=tmp_path / "b", legacy=[])
    core = AppCore(settings=settings, components=cm)
    rep = run_full(core, check_logins=False)
    names = {c.name: c for c in rep.checks}
    assert names["Папка загрузок"].status == OK
    assert names["yt-dlp (движок загрузки)"].status == FAIL and "Компоненты" in names["yt-dlp (движок загрузки)"].hint
    assert rep.worst == FAIL
    assert '"checks"' in rep.to_json() and "диагностический отчёт" in rep.to_text()
