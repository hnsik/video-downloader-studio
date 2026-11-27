from vds.redact import clean, redact, redact_obj, strip_ansi


def test_strip_ansi():
    assert strip_ansi("\x1b[0;31mERROR\x1b[0m: boom\x1b[K") == "ERROR: boom"


def test_signed_url_params_hidden():
    url = "https://rr1.googlevideo.com/videoplayback?expire=1&sig=AAAA1234&lsig=BBBB&key=yt8&id=abc"
    out = redact(url)
    assert "AAAA1234" not in out and "BBBB" not in out and "yt8" not in out
    assert "id=abc" in out and "expire=1" in out


def test_cookie_header_and_netscape_lines_hidden():
    text = "Cookie: SID=secret123; HSID=other\nSet-Cookie: a=b\n.youtube.com\tTRUE\t/\tTRUE\t1893456000\tSAPISID\tSUPERSECRETVALUE"
    out = redact(text)
    assert "secret123" not in out and "SUPERSECRETVALUE" not in out
    assert "SAPISID" in out  # имя cookie остаётся


def test_tokens_and_bearer_and_jwt():
    out = redact("Authorization: Bearer abcdefghijklmnop1234 token=xyz987 po_token=MnQabc eyJhbGciOi12345.eyJzdWIiOiIx2345.sigsigsig12345")
    for secret in ("abcdefghijklmnop1234", "xyz987", "MnQabc", "eyJhbGciOi12345"):
        assert secret not in out


def test_sha256_not_masked_and_user_path_hidden():
    digest = "a" * 64
    assert digest in clean(f"sha256 {digest}")
    assert "ivan" not in clean(r"C:\Users\ivan\AppData\file.txt")


def test_normal_text_preserved():
    assert clean("Extracted 74 cookies from firefox") == "Extracted 74 cookies from firefox"


def test_redact_obj_nested():
    out = redact_obj({"password": "p", "nested": [{"msg": "token=abc123"}], "ok": "fine"})
    assert out["password"] != "p" and "abc123" not in str(out) and out["ok"] == "fine"
