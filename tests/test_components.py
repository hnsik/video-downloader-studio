import hashlib
import io
import threading
import zipfile
from pathlib import Path

import pytest

from vds.components import (Cancelled, ComponentBusyError, ComponentManager, Release, UpdateError, check_trusted,
                            exe, is_newer, parse_checksums, parse_version)

YTDLP_SCRIPT = b"#!/bin/sh\necho 2026.11.24\n"
YTDLP_OLD = b"#!/bin/sh\necho 2026.11.01\n"
BROKEN = b"#!/bin/sh\nexit 3\n"
DENO = b"#!/bin/sh\nif [ \"$1\" = eval ]; then echo 42; else echo 'deno 2.9.9 (stable, release)'; fi\n"
FFMPEG = b"#!/bin/sh\necho 'ffmpeg version n9.0.2-test Copyright'\n"
FFPROBE = b"#!/bin/sh\necho 'ffprobe version n9.0.2-test Copyright'\n"


def make_zip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return buf.getvalue()


class FakeHttp:
    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.texts: dict[str, str] = {}
        self.downloads = 0

    def get_text(self, url):
        if url in self.texts:
            return self.texts[url]
        raise UpdateError("нет такого URL")

    def get_json(self, url):
        import json
        return json.loads(self.get_text(url))

    def download(self, url, dest: Path, progress=None, cancel=None):
        self.downloads += 1
        if cancel is not None and cancel.is_set():
            raise Cancelled("отмена")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.blobs[url])
        if progress:
            progress(len(self.blobs[url]), len(self.blobs[url]), "x")


def sums(name: str, data: bytes) -> str:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n"


@pytest.fixture
def env(tmp_path):
    http = FakeHttp()
    state = {"ytdlp": YTDLP_SCRIPT, "version": "2026.11.24", "bad_sum": False}

    def ytdlp_provider(h, **kw):
        data = state["ytdlp"]
        h.blobs["u/yt"] = data
        h.texts["u/yt.sums"] = sums("yt-dlp.exe", b"tampered" if state["bad_sum"] else data)
        return Release("ytdlp", state["version"], "yt-dlp.exe", "u/yt", "u/yt.sums", stamp=state["version"], channel=kw.get("channel", "stable"))

    def deno_provider(h, **kw):
        z = make_zip({"deno": DENO})
        h.blobs["u/deno"] = z
        return Release("deno", "2.9.9", "deno.zip", "u/deno", digest=hashlib.sha256(z).hexdigest())

    def ff_provider(h, **kw):
        z = make_zip({"ffmpeg-n9.0/bin/ffmpeg": FFMPEG, "ffmpeg-n9.0/bin/ffprobe": FFPROBE, "ffmpeg-n9.0/LICENSE.txt": b"gpl"})
        h.blobs["u/ff"] = z
        return Release("ffmpeg", "9.0", "ff.zip", "u/ff", stamp="2026-11-20T00:00:00Z")

    busy = {"v": False}
    mgr = ComponentManager(root=tmp_path / "comp", http=http, busy_check=lambda: busy["v"],
                           providers={"ytdlp": ytdlp_provider, "deno": deno_provider, "ffmpeg": ff_provider,
                                      "ejs": lambda h, **kw: Release("ejs", "0.8.0", "", ""),
                                      "node": lambda h, **kw: Release("node", "22.1.0", "", "")},
                           bundled=tmp_path / "bundled", legacy=[])
    return mgr, http, state, busy


def test_helpers():
    assert is_newer("2026.11.24", "2026.11.23") and not is_newer("2026.11.23", "2026.11.23")
    assert is_newer("2026.11.23.234419", "2026.11.23")          # nightly новее stable того же дня
    assert parse_version("deno", "deno 2.9.8 (stable, release, x86_64-pc-windows-msvc)\nv8 14") == "2.9.8"
    assert parse_version("ffmpeg", "ffmpeg version 9.0.2-essentials_build Copyright") == "9.0.2-essentials_build"
    assert parse_version("ytdlp", "2026.11.23\n") == "2026.11.23"
    h = hashlib.sha256(b"a").hexdigest()
    assert parse_checksums(f"{h}  yt-dlp.exe\n{'0'*64} *other\n", "yt-dlp.exe") == h
    assert parse_checksums(h, "anything.zip") == h


def test_untrusted_sources_rejected():
    for bad in ("http://github.com/x", "https://evil.example.com/yt-dlp.exe", "https://github.com.evil.io/x"):
        with pytest.raises(UpdateError):
            check_trusted(bad)
    check_trusted("https://github.com/yt-dlp/yt-dlp/releases/download/x/yt-dlp.exe")
    check_trusted("https://release-assets.githubusercontent.com/x")


def test_install_ytdlp_verifies_checksum_and_activates(env):
    mgr, http, state, _ = env
    msg = mgr.update("ytdlp")
    assert "2026.11.24" in msg and "проверена" in msg
    res = mgr.resolve("ytdlp")
    assert res.source == "managed" and res.path.name == exe("yt-dlp")
    assert mgr.probe("ytdlp", res.path) == "2026.11.24"
    assert [h["action"] for h in mgr.history()] == ["update"]


def test_second_update_is_noop_unless_forced(env):
    mgr, http, *_ = env
    mgr.update("ytdlp")
    n = http.downloads
    assert "уже установлена" in mgr.update("ytdlp")
    assert http.downloads == n
    mgr.update("ytdlp", force=True)
    assert http.downloads == n + 1
    assert mgr.history()[0]["action"] == "reinstall"


def test_bad_checksum_rejected_and_nothing_activated(env):
    mgr, http, state, _ = env
    state["bad_sum"] = True
    with pytest.raises(UpdateError, match="Контрольная сумма не совпала"):
        mgr.update("ytdlp")
    assert mgr.resolve("ytdlp").source == "missing"
    assert not list((mgr.root / "ytdlp").glob(".staging-*"))
    assert mgr.history()[0]["ok"] is False


def test_update_then_rollback_and_history(env):
    mgr, http, state, _ = env
    state.update(ytdlp=YTDLP_OLD, version="2026.11.01")
    mgr.update("ytdlp")
    state.update(ytdlp=YTDLP_SCRIPT, version="2026.11.24")
    mgr.update("ytdlp")
    assert mgr.probe("ytdlp", mgr.path("ytdlp")) == "2026.11.24"
    assert mgr.can_rollback("ytdlp")
    assert "откат" in mgr.rollback("ytdlp")
    assert mgr.probe("ytdlp", mgr.path("ytdlp")) == "2026.11.01"
    with pytest.raises(UpdateError):
        mgr.rollback("ytdlp")   # дальше откатываться некуда
    assert [h["action"] for h in mgr.history()][:1] == ["rollback"]


def test_broken_new_version_never_activated(env):
    mgr, http, state, _ = env
    mgr.update("ytdlp")
    state.update(ytdlp=BROKEN, version="2026.12.01")
    with pytest.raises(UpdateError):
        mgr.update("ytdlp")
    # рабочая версия осталась активной
    assert mgr.probe("ytdlp", mgr.path("ytdlp")) == "2026.11.24"
    assert mgr.health("ytdlp")[0]


def test_busy_downloads_block_replacement(env):
    mgr, http, state, busy = env
    mgr.update("ytdlp")
    busy["v"] = True
    state.update(ytdlp=YTDLP_OLD, version="2026.12.31")
    with pytest.raises(ComponentBusyError):
        mgr.update("ytdlp")
    with pytest.raises(ComponentBusyError):
        mgr.rollback("ytdlp")
    assert mgr.probe("ytdlp", mgr.path("ytdlp")) == "2026.11.24"


def test_cancel_during_download_keeps_old(env):
    mgr, http, state, _ = env
    mgr.update("ytdlp")
    ev = threading.Event()
    ev.set()
    state.update(ytdlp=YTDLP_OLD, version="2027.01.01")
    with pytest.raises(Cancelled):
        mgr.update("ytdlp", cancel=ev)
    assert mgr.probe("ytdlp", mgr.path("ytdlp")) == "2026.11.24"
    assert not list((mgr.root / ".tmp").glob("*.part"))


def test_ffmpeg_and_ffprobe_from_one_build(env):
    mgr, *_ = env
    mgr.update("ffprobe")   # виртуальный компонент ставит всю сборку
    ff, fp = mgr.resolve("ffmpeg"), mgr.resolve("ffprobe")
    assert ff.path.parent == fp.path.parent == mgr.ffmpeg_dir()
    assert (ff.path.parent / "LICENSE.txt").exists()
    assert mgr.health("ffmpeg")[0] and mgr.health("ffprobe")[0]


def test_deno_install_with_github_digest_and_js_health(env):
    mgr, *_ = env
    assert "проверена" in mgr.update("deno")
    assert mgr.health("deno") == (True, "Работает, версия 2.9.9")


def test_zip_slip_is_flattened(env, tmp_path):
    mgr, http, *_ = env
    evil = make_zip({"../../evil": b"x", "deno": DENO})
    mgr.providers["deno"] = lambda h, **kw: (h.blobs.__setitem__("u/d", evil), Release("deno", "2.9.9", "d.zip", "u/d", digest=hashlib.sha256(evil).hexdigest()))[1]
    mgr.update("deno")
    assert not (tmp_path / "evil").exists() and not (mgr.root.parent / "evil").exists()


def test_statuses_and_update_flag(env):
    mgr, http, state, _ = env
    state.update(ytdlp=YTDLP_OLD, version="2026.11.01")
    mgr.update("ytdlp")
    state.update(ytdlp=YTDLP_SCRIPT, version="2026.11.24")
    assert mgr.check_latest(["ytdlp"]) == {}
    st = {s.key: s for s in mgr.statuses()}
    assert st["ytdlp"].installed == "2026.11.01" and st["ytdlp"].latest == "2026.11.24"
    assert st["ytdlp"].update_available and st["ytdlp"].healthy and st["ytdlp"].source == "managed"
    assert st["ytdlp"].checked_at
    assert not st["deno"].healthy and st["deno"].source == "missing"
    assert st["ejs"].installed  # встроен в yt-dlp


def test_check_latest_reports_network_errors_without_raising(env):
    mgr, *_ = env
    mgr.providers["deno"] = lambda h, **kw: (_ for _ in ()).throw(UpdateError("нет сети"))
    errors = mgr.check_latest(["deno"])
    assert "нет сети" in errors["deno"]


def test_bundled_used_when_no_managed_and_newer_bundled_wins(env, tmp_path):
    mgr, http, state, _ = env
    (tmp_path / "bundled").mkdir()
    b = tmp_path / "bundled" / exe("yt-dlp")
    b.write_bytes(YTDLP_OLD)
    b.chmod(0o755)
    assert mgr.resolve("ytdlp").source == "bundled"
    state.update(ytdlp=YTDLP_OLD, version="2026.11.01")
    mgr.update("ytdlp")                                    # managed == bundled по версии -> managed
    assert mgr.resolve("ytdlp").source == "managed"
    b.write_bytes(YTDLP_SCRIPT)                            # установщик принёс более новый bundled
    mgr._probe_cache.clear()
    assert mgr.resolve("ytdlp").source == "bundled"


def test_legacy_ffmpeg_found_and_system_path_off_by_default(tmp_path):
    legacy = tmp_path / "_internal"
    legacy.mkdir()
    for n, data in (("ffmpeg", FFMPEG), ("ffprobe", FFPROBE)):
        p = legacy / exe(n)
        p.write_bytes(data)
        p.chmod(0o755)
    mgr = ComponentManager(root=tmp_path / "c", http=FakeHttp(), bundled=tmp_path / "none", legacy=[legacy])
    assert mgr.resolve("ffmpeg").source == "legacy" and mgr.ffmpeg_dir() == legacy
    assert mgr.resolve("deno").source == "missing"
