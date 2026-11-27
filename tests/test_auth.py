import json

from vds.auth import (_chromium_profiles, _firefox_profiles, check_auth, cookie_args, session_cookie_names,
                      validate_cookie_file)

NETSCAPE = "# Netscape HTTP Cookie File\n" \
           ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tVALUE_SHOULD_NEVER_LEAK\n" \
           "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t1893456000\t__Secure-3PSID\tV2\n" \
           ".example.com\tTRUE\t/\tFALSE\t0\tfoo\tbar\n"


def test_cookie_args():
    assert cookie_args({"method": "none"}) == []
    assert cookie_args({"method": "firefox"}) == ["--cookies-from-browser", "firefox"]
    assert cookie_args({"method": "chrome", "profile": "Profile 1"}) == ["--cookies-from-browser", "chrome:Profile 1"]
    assert cookie_args({"method": "file", "cookie_file": "\u202aC:\\c.txt"}) == ["--cookies", "C:\\c.txt"]
    assert cookie_args({"method": "file", "cookie_file": ""}) == []


def test_validate_cookie_file(tmp_path):
    good = tmp_path / "c.txt"
    good.write_text(NETSCAPE, encoding="utf-8")
    info = validate_cookie_file(str(good))
    assert info.ok and info.count == 3 and "youtube.com" in info.domains
    bad = tmp_path / "bad.txt"
    bad.write_text("hello", encoding="utf-8")
    assert not validate_cookie_file(str(bad)).ok
    assert not validate_cookie_file(str(tmp_path / "missing.txt")).ok
    empty = tmp_path / "e.txt"
    empty.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    assert not validate_cookie_file(str(empty)).ok


def test_session_names_only_never_values(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text(NETSCAPE, encoding="utf-8")
    names = session_cookie_names(f, "youtube")
    assert names == ["SAPISID", "__Secure-3PSID"]
    assert session_cookie_names(f, "instagram") == []


def test_check_auth_file_reports_logged_in(tmp_path):
    f = tmp_path / "c.txt"
    f.write_text(NETSCAPE, encoding="utf-8")
    res = check_auth(None, {"method": "file", "cookie_file": str(f)}, "youtube")
    assert res.ok and res.logged_in is True and "VALUE_SHOULD_NEVER_LEAK" not in res.message
    res2 = check_auth(None, {"method": "file", "cookie_file": str(f)}, "instagram")
    assert res2.ok and res2.logged_in is False


def test_check_auth_none_and_missing_engine():
    assert check_auth(None, {"method": "none"}, "youtube").ok
    assert not check_auth(None, {"method": "firefox"}, "youtube").ok


def test_check_auth_browser_with_fake_engine(tmp_path):
    fake = tmp_path / "fake"
    fake.write_text('''#!/usr/bin/env python3
import sys
a = sys.argv
out = a[a.index("--cookies") + 1]
open(out, "w").write("# Netscape HTTP Cookie File\\n.youtube.com\\tTRUE\\t/\\tTRUE\\t1893456000\\tSAPISID\\tsecretvalue\\n")
print("Extracted 74 cookies from firefox")
''')
    fake.chmod(0o755)
    res = check_auth(fake, {"method": "firefox", "profile": ""}, "youtube")
    assert res.ok and res.cookies_loaded == 74 and res.logged_in is True


def test_check_auth_dpapi_message(tmp_path):
    fake = tmp_path / "fake"
    fake.write_text("#!/bin/sh\necho 'ERROR: Failed to decrypt with DPAPI'\nexit 1\n")
    fake.chmod(0o755)
    res = check_auth(fake, {"method": "chrome"}, "youtube")
    assert not res.ok and "Firefox" in res.hint


def test_firefox_profiles_parse(tmp_path):
    d = tmp_path / "Mozilla" / "Firefox"
    d.mkdir(parents=True)
    (d / "profiles.ini").write_text(
        "[Install123]\nDefault=Profiles/abcd.default-release\n[Profile0]\nName=default-release\nPath=Profiles/abcd.default-release\n"
        "[Profile1]\nName=old\nPath=Profiles/zzzz.default\n", encoding="utf-8")
    profiles = _firefox_profiles(tmp_path)
    assert profiles[0].id == "abcd.default-release" and "по умолчанию" in profiles[0].name
    assert {p.id for p in profiles} == {"abcd.default-release", "zzzz.default"}


def test_chromium_profiles(tmp_path):
    ud = tmp_path / "User Data"
    for name in ("Default", "Profile 1", "Cache"):
        (ud / name).mkdir(parents=True)
    (ud / "Default" / "Preferences").write_text("{}")
    (ud / "Profile 1" / "Preferences").write_text("{}")
    (ud / "Local State").write_text(json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "Работа"}}}}), encoding="utf-8")
    profiles = _chromium_profiles(ud)
    assert [p.id for p in profiles] == ["Default", "Profile 1"] and "Работа" in profiles[1].name
