import threading
import time
from pathlib import Path

import pytest

from vds.engine import (Engine, JobHandle, JobOptions, Tools, build_command, parse_destination, parse_progress)


def make(tmp_path, fake, **kw):
    out = tmp_path / "out"
    opts = JobOptions(url="https://www.youtube.com/watch?v=abc123", out_dir=str(out), probe_after=False, **kw)
    return opts, Engine(lambda: Tools(fake))


def test_parse_progress_lines():
    p = parse_progress("VDSP|downloading|500|1000|NA|2048.5|7|NA|NA")
    assert p.percent == 50.0 and p.speed == 2048.5 and p.eta == 7 and p.total == 1000
    est = parse_progress("VDSP|downloading|250|NA|1000|NA|NA|NA|NA")
    assert est.percent == 25.0 and est.eta == -1
    frag = parse_progress("VDSP|downloading|10|NA|NA|NA|NA|3|12")
    assert frag.percent == 25.0
    assert parse_progress("VDSP|finished|1|1|NA|NA|NA|NA|NA").percent == 100.0
    assert parse_progress("[download] 50%") is None and parse_progress("VDSP|bad") is None


def test_parse_destination_prefers_final_and_ignores_side_files():
    assert parse_destination('[Merger] Merging formats into "C:\\v\\a [id].mp4"') == ("final", "C:\\v\\a [id].mp4")
    assert parse_destination("[download] Destination: C:\\v\\a.f137.mp4") == ("part", "C:\\v\\a.f137.mp4")
    assert parse_destination("[download] Destination: C:\\v\\a.en.vtt") is None
    assert parse_destination("[download] C:\\v\\a.mp4 has already been downloaded") == ("final", "C:\\v\\a.mp4")
    assert parse_destination("[ExtractAudio] Destination: C:\\v\\a.mp3") == ("final", "C:\\v\\a.mp3")


def test_build_command_isolates_from_system_and_embeds_tools(tmp_path):
    opts = JobOptions(url="https://x.test/v", out_dir=str(tmp_path), subtitles=True, thumbnails=True, rate_limit="2M",
                      impersonate="chrome", cookie_args=["--cookies-from-browser", "firefox"], ejs_remote=True)
    tools = Tools(Path("C:/c/yt-dlp.exe"), Path("C:/c/ffmpeg.exe"), Path("C:/c/ffprobe.exe"), Path("C:/c/deno.exe"), Path("C:/c/node.exe"))
    cmd = build_command(opts, tools)
    assert cmd[1] == "--ignore-config" and cmd[-2:] == ["--", "https://x.test/v"]
    assert "--no-js-runtimes" in cmd and "deno:" + str(tools.deno) in cmd and "node:" + str(tools.node) in cmd
    assert cmd[cmd.index("--ffmpeg-location") + 1] == str(Path("C:/c"))
    assert "--remote-components" in cmd and "--impersonate" in cmd and "--write-subs" in cmd and "--write-thumbnail" in cmd
    assert cmd[cmd.index("-r") + 1] == "2M" and "--cookies-from-browser" in cmd
    assert "--no-playlist" in cmd and "--no-overwrites" in cmd and "--continue" in cmd
    no_imp = build_command(opts, tools, use_impersonate=False, remote_ejs=False)
    assert "--impersonate" not in no_imp and "--remote-components" not in no_imp


def test_build_command_audio_mode(tmp_path):
    cmd = build_command(JobOptions(url="u", out_dir=str(tmp_path), mode="audio", audio_format="mp3"), Tools(Path("yt")))
    assert "-x" in cmd and cmd[cmd.index("--audio-format") + 1] == "mp3" and "--merge-output-format" not in cmd


def test_successful_download(tmp_path, fake_ytdlp, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "ok")
    opts, eng = make(tmp_path, fake_ytdlp)
    seen, logs = [], []
    res = eng.run(opts, JobHandle(), seen.append, logs.append)
    assert res.ok and res.state == "done" and Path(res.filepath).name == "Test [abc123].mp4"
    assert res.title == "Test" and res.size == 100
    assert seen and seen[-1].percent == 100.0
    assert any("Merging formats" in l for l in logs) and not any(l.startswith("VDSP") for l in logs)


def test_impersonation_fallback_is_single(tmp_path, fake_ytdlp, monkeypatch):
    counter = tmp_path / "count"
    monkeypatch.setenv("FAKE_MODE", "imp_fail")
    monkeypatch.setenv("FAKE_COUNT", str(counter))
    opts, eng = make(tmp_path, fake_ytdlp, impersonate="chrome")
    res = eng.run(opts, JobHandle())
    assert res.ok
    runs = counter.read_text().splitlines()
    assert len(runs) == 2 and "--impersonate" in runs[0] and "--impersonate" not in runs[1]


def test_ejs_challenge_retries_once_with_remote_components(tmp_path, fake_ytdlp, monkeypatch):
    counter = tmp_path / "count"
    monkeypatch.setenv("FAKE_MODE", "ejs_fail")
    monkeypatch.setenv("FAKE_COUNT", str(counter))
    opts, eng = make(tmp_path, fake_ytdlp)
    res = eng.run(opts, JobHandle())
    assert res.ok
    runs = counter.read_text().splitlines()
    assert len(runs) == 2 and "--remote-components" in runs[1]


def test_no_endless_retries_on_hard_error(tmp_path, fake_ytdlp, monkeypatch):
    counter = tmp_path / "count"
    monkeypatch.setenv("FAKE_MODE", "e403")
    monkeypatch.setenv("FAKE_COUNT", str(counter))
    opts, eng = make(tmp_path, fake_ytdlp)
    res = eng.run(opts, JobHandle())
    assert not res.ok and res.error.code == "HTTP_403" and res.returncode == 1
    assert len(counter.read_text().splitlines()) == 1


def test_cancel_kills_process_quickly(tmp_path, fake_ytdlp, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "slow")
    opts, eng = make(tmp_path, fake_ytdlp)
    handle, out = JobHandle(), {}
    t = threading.Thread(target=lambda: out.setdefault("r", eng.run(opts, handle)))
    t.start()
    time.sleep(1.0)
    start = time.time()
    handle.stop(pause=False)
    t.join(10)
    assert not t.is_alive() and time.time() - start < 5
    assert out["r"].state == "canceled" and not out["r"].ok


def test_pause_state(tmp_path, fake_ytdlp, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "slow")
    opts, eng = make(tmp_path, fake_ytdlp)
    handle, out = JobHandle(), {}
    t = threading.Thread(target=lambda: out.setdefault("r", eng.run(opts, handle)))
    t.start()
    time.sleep(0.8)
    handle.stop(pause=True)
    t.join(10)
    assert out["r"].state == "paused"


def test_stall_watchdog_reports_timeout(tmp_path, fake_ytdlp, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "silent")
    opts, eng = make(tmp_path, fake_ytdlp, stall_timeout=2)
    res = eng.run(opts, JobHandle())
    assert not res.ok and res.error.code == "TIMEOUT"


def test_missing_ytdlp_gives_actionable_error(tmp_path):
    eng = Engine(lambda: Tools(None))
    res = eng.run(JobOptions(url="u", out_dir=str(tmp_path)), JobHandle())
    assert not res.ok and "update_ytdlp" in res.error.actions
