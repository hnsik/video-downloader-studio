import pytest

from vds.platforms import dedupe_key, detect_platform


@pytest.mark.parametrize("url,platform", [
    ("https://www.youtube.com/shorts/U8nrRMitpWI", "youtube"),
    ("https://youtu.be/abc", "youtube"),
    ("https://www.instagram.com/reel/xyz/", "instagram"),
    ("https://vm.tiktok.com/abc", "tiktok"),
    ("https://fb.watch/abc", "facebook"),
    ("https://www.facebook.com/reel/1", "facebook"),
    ("https://x.com/u/status/1", "twitter"),
    ("https://twitter.com/u/status/1", "twitter"),
    ("https://www.reddit.com/r/a/comments/1", "reddit"),
    ("https://example.org/v.mp4", "other"),
    ("https://notyoutube.com/x", "other"),
])
def test_detect(url, platform):
    assert detect_platform(url) == platform


def test_dedupe_youtube_variants_equal():
    a = dedupe_key("https://www.youtube.com/watch?v=jNQXAC9IP4c&t=5")
    b = dedupe_key("https://youtu.be/jNQXAC9IP4c")
    c = dedupe_key("https://www.youtube.com/shorts/jNQXAC9IP4c")
    assert a == b == c == "youtube:jNQXAC9IP4c"
