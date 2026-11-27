import json

from vds import paths
from vds.config import DEFAULTS, SCHEMA, Settings, migrate


def test_defaults_roundtrip(tmp_path):
    s = Settings(tmp_path / "settings.json").load()
    s.set("download_dir", "F:/Videos")
    s.set_auth("youtube", "firefox", "abc.default-release")
    s.save()
    again = Settings(tmp_path / "settings.json").load()
    assert again.get("download_dir") == "F:/Videos"
    assert again.auth_for("youtube")["method"] == "firefox"
    assert again.get("schema") == SCHEMA


def test_migrate_legacy_v5_settings_keeps_everything():
    old = {"folder": "F:/Down", "iphone": True, "quality": "1080p", "youtube_cookie": "C:/c.txt", "tt_cookie": "",
           "weird_future_key": 42}
    new = migrate(old)
    assert new["download_dir"] == "F:/Down" and new["ios_compat"] is True and new["quality"] == "1080"
    assert new["auth"]["youtube"] == {"method": "file", "profile": "", "cookie_file": "C:/c.txt"}
    assert new["auth"]["tiktok"]["method"] == "none"
    assert new["extra"]["weird_future_key"] == 42   # неизвестные ключи не теряются
    assert new["schema"] == SCHEMA


def test_migrate_current_schema_fills_new_keys():
    new = migrate({"schema": SCHEMA, "container": "mkv"})
    assert new["container"] == "mkv" and new["concurrency"] == DEFAULTS["concurrency"]


def test_load_migrates_and_backs_up(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"folder": "D:/x"}), encoding="utf-8")
    s = Settings(p).load()
    assert s.get("download_dir") == "D:/x"
    backups = list(paths.backup_dir().glob("settings-pre-migration-*.json"))
    assert backups, "перед миграцией должна создаваться резервная копия"
    assert json.loads(backups[0].read_text(encoding="utf-8")) == {"folder": "D:/x"}


def test_corrupt_file_falls_back_to_bak(tmp_path):
    p = tmp_path / "settings.json"
    s = Settings(p).load()
    s.set("download_dir", "E:/ok")
    s.save()
    s.save()  # создаёт .bak с корректным содержимым
    p.write_text("{broken", encoding="utf-8")
    assert Settings(p).load().get("download_dir") == "E:/ok"


def test_legacy_file_discovered(tmp_path, monkeypatch):
    legacy_dir = tmp_path / "local" / "VideoDownloaderStudio"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "settings.json").write_text(json.dumps({"output_dir": "G:/old"}), encoding="utf-8")
    s = Settings(tmp_path / "new" / "settings.json").load()
    assert s.get("download_dir") == "G:/old" and s.migrated_from


def test_reset_auth():
    s = Settings(paths.data_dir() / "s.json")
    s.set_auth("tiktok", "chrome", "Default")
    s.reset_auth("tiktok")
    assert s.auth_for("tiktok")["method"] == "none"
    s.set_auth("reddit", "edge")
    s.reset_auth()
    assert s.auth_for("reddit")["method"] == "none"
