import pytest

from vds.errors import classify

CASES = [
    ("TypeError: a.addOnDisposeCallback is not a function", "EJS_INCOMPAT"),
    ("WARNING: [youtube] n challenge solving failed: Some formats may be missing", "JS_CHALLENGE"),
    ("ERROR: Failed to decrypt with DPAPI. See  https://github.com/yt-dlp/yt-dlp/issues/10927  for more info", "CHROME_DPAPI"),
    ("ERROR: Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271", "COOKIES_LOCKED"),
    ("ERROR: [youtube] U8nrRMitpWI: Sign in to confirm your age. This video may be inappropriate for some users.", "AGE_RESTRICTED"),
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot", "BOT_CHECK"),
    ("ERROR: [youtube] x: Private video. Sign in if you've been granted access to this video", "PRIVATE_VIDEO"),
    ("ERROR: [youtube] x: Video unavailable. This video has been removed by the uploader", "VIDEO_REMOVED"),
    ("ERROR: The uploader has not made this video available in your country", "GEO_BLOCK"),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", "HTTP_403"),
    ("ERROR: HTTP Error 429: Too Many Requests", "HTTP_429"),
    ("WARNING: [youtube] x: GVS PO Token (web client) was not provided", "PO_TOKEN"),
    ("ERROR: Postprocessing: ffprobe and ffmpeg not found. Please install or provide the path using --ffmpeg-location", "FFPROBE_MISSING"),
    ("ERROR: Postprocessing: Conversion failed!", "FFMPEG_ERROR"),
    ("OSError: [Errno 28] No space left on device", "DISK_FULL"),
    ("ERROR: Unable to download webpage: <urlopen error [Errno 11001] getaddrinfo failed>", "NETWORK"),
    ("ERROR: [youtube] x: Requested format is not available. Use --list-formats", "FORMAT_UNAVAILABLE"),
    ("ERROR: The read operation timed out", "TIMEOUT"),
    ("ERROR: [Instagram] x: Unable to extract data; please report this issue on https://github.com/yt-dlp/yt-dlp/issues", "OUTDATED"),
    ("ERROR: [Instagram] x: rate-limit reached or login required. Use --cookies", "AUTH_REQUIRED"),
    ("No supported JavaScript runtime could be found", "NO_JS_RUNTIME"),
    ("VDS_STALL_TIMEOUT: нет вывода", "TIMEOUT"),
]


@pytest.mark.parametrize("text,code", CASES)
def test_classify(text, code):
    info = classify(text)
    assert info.code == code, (text, info.code)
    assert info.title and info.cause and info.solution


def test_dpapi_recommends_firefox_and_not_retryable():
    info = classify("Failed to decrypt with DPAPI")
    assert "use_firefox" in info.actions and "Firefox" in info.solution and info.retryable is False


def test_unknown_has_guidance_and_redacted_technical():
    info = classify("something odd token=SECRET1234 happened", 2)
    assert info.code == "UNKNOWN" and "SECRET1234" not in info.technical and "2" in info.cause
