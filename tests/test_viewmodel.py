from types import SimpleNamespace

from vds.viewmodel import fmt_eta, fmt_size, fmt_speed, job_row, parse_links, short_path


def test_fmt_size():
    assert fmt_size(0) == "—"
    assert fmt_size(None) == "—"
    assert fmt_size(512) == "512 Б"
    assert fmt_size(1536) == "1.5 КБ"
    assert fmt_size(5 * 1024 ** 3) == "5.0 ГБ"


def test_fmt_speed_eta():
    assert fmt_speed(0) == "—"
    assert fmt_speed(2 * 1024 * 1024) == "2.0 МБ/с"
    assert fmt_eta(-1) == "—"
    assert fmt_eta(65) == "1:05"
    assert fmt_eta(3725) == "1:02:05"


def test_parse_links_splits_dedupes_and_flags_invalid():
    text = "https://youtu.be/abc\n  https://youtu.be/abc ,https://x.com/a/status/1;  not a link\u202a\nftp://x.y/z"
    good, bad = parse_links(text)
    assert good == ["https://youtu.be/abc", "https://x.com/a/status/1"]
    assert "ftp://x.y/z" in bad and "not" in bad


def test_parse_links_strips_invisible_marks():
    good, bad = parse_links("\u202ahttps://www.tiktok.com/@a/video/1\u202c")
    assert good == ["https://www.tiktok.com/@a/video/1"] and not bad


def test_job_row():
    job = SimpleNamespace(title="", url="https://a.b/c", platform="other", status="running", percent=150.0,
                          speed=1024.0, eta=30, total=0, size=2048)
    row = job_row(job)
    assert row["title"] == "https://a.b/c"
    assert row["status"] == "Загрузка"
    assert row["percent"] == 100
    assert row["size"] == "2.0 КБ" and row["eta"] == "0:30"


def test_short_path():
    assert short_path("C:\\a") == "C:\\a"
    assert len(short_path("x" * 300)) <= 71
