import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def vds_home(tmp_path, monkeypatch):
    """Все данные приложения во время тестов — во временной папке."""
    monkeypatch.setenv("VDS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    return tmp_path / "home"


FAKE_YTDLP = r'''#!/usr/bin/env python3
import os, sys, time
args = sys.argv[1:]
mode = os.environ.get("FAKE_MODE", "ok")
counter = os.environ.get("FAKE_COUNT")
if counter:
    with open(counter, "a") as f:
        f.write(" ".join(a for a in args if a.startswith("--impersonate") or a.startswith("--remote-components")) + "|\n")
url = args[-1]
tmpl = args[args.index("-o") + 1]
path = tmpl.replace("%(title).160B", "Test").replace("%(id)s", "abc123").replace("%(ext)s", "mp4")

def prog(status, done, total, speed=1000.0, eta=3):
    print(f"VDSP|{status}|{done}|{total}|NA|{speed}|{eta}|NA|NA", flush=True)

def finish():
    prog("downloading", 500, 1000)
    prog("finished", 1000, 1000)
    open(path, "wb").write(b"x" * 100)
    print(f"[download] Destination: {path}.f137.mp4", flush=True)
    print(f'[Merger] Merging formats into "{path}"', flush=True)
    sys.exit(0)

if mode == "ok":
    finish()
elif mode == "imp_fail":
    if "--impersonate" in args:
        print("ERROR: Impersonate target \"chrome\" is not available. Install curl_cffi", flush=True); sys.exit(1)
    finish()
elif mode == "ejs_fail":
    if "--remote-components" not in args:
        print("ERROR: [youtube] abc123: n challenge solving failed: Some formats may be missing", flush=True); sys.exit(1)
    finish()
elif mode == "e403":
    print("ERROR: unable to download video data: HTTP Error 403: Forbidden", flush=True); sys.exit(1)
elif mode == "slow":
    for i in range(300):
        prog("downloading", i, 1000)
        time.sleep(0.1)
elif mode == "silent":
    time.sleep(60)
'''


@pytest.fixture
def fake_ytdlp(tmp_path):
    script = tmp_path / "fake-yt-dlp"
    script.write_text(FAKE_YTDLP)
    script.chmod(0o755)
    return script
