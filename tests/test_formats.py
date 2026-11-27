from vds.formats import build_plan


def test_mp4_default_prefers_compatible_without_transcode():
    p = build_plan("mp4", "best", "auto", "video")
    assert p.merge == "mp4/mkv" and "vcodec:h264" in p.sort and "acodec:m4a" in p.sort
    assert p.format.endswith("/b") and "bv*+ba" in p.format


def test_height_limit_applied_everywhere():
    p = build_plan("mp4", "720", "auto", "video")
    assert "[height<=720]" in p.format and all("720" in part for part in p.format.split("/") if part != "b" and "ba" in part or part.startswith("b["))


def test_explicit_codec_has_auto_fallback():
    p = build_plan("mkv", "1080", "av1", "video")
    parts = p.format.split("/")
    assert parts[0].startswith("bv*[vcodec^=av01]") and "bv*[height<=1080]+ba" in parts


def test_h265_filter():
    assert "hev1" in build_plan("mp4", "best", "h265", "video").format


def test_webm_merge_and_sort():
    p = build_plan("webm", "best", "auto", "video")
    assert p.merge == "webm/mkv" and "vcodec:vp9" in p.sort


def test_audio_modes_announce_transcoding():
    mp3 = build_plan(mode="audio", audio_format="mp3")
    assert mp3.audio_format == "mp3" and any("перекодирование" in n for n in mp3.notes)
    m4a = build_plan(mode="audio", audio_format="m4a")
    assert m4a.format.startswith("ba[ext=m4a]")


def test_video_only_has_no_audio_stream():
    p = build_plan(mode="video_only", quality="480")
    assert "+ba" not in p.format and "[height<=480]" in p.format
