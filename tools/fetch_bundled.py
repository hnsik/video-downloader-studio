"""Скачивает начальные yt-dlp, Deno, FFmpeg/FFprobe в папку bundled/ для включения в установщик.

Источники — только официальные (см. TRUSTED_HOSTS в vds/components.py); контрольные суммы проверяются.
Запуск: python tools/fetch_bundled.py [папка]
"""
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from vds.components import HttpClient, latest_deno, latest_ffmpeg, latest_ytdlp, parse_checksums, sha256_file  # noqa: E402


def verify(http, rel, file: Path) -> None:
    expected = ""
    if rel.checksum_url:
        expected = parse_checksums(http.get_text(rel.checksum_url), rel.asset_name)
    expected = expected or rel.digest.split(":")[-1]
    if not expected:
        print(f"  ! нет опубликованной контрольной суммы для {rel.asset_name}")
        return
    if sha256_file(file).lower() != expected.lower():
        raise SystemExit(f"Контрольная сумма не совпала: {rel.asset_name}")
    print("  контрольная сумма OK")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "bundled")
    out.mkdir(parents=True, exist_ok=True)
    tmp = out / "_dl"
    tmp.mkdir(exist_ok=True)
    http = HttpClient(120)
    for finder, wanted in ((latest_ytdlp, ("yt-dlp.exe",)), (latest_deno, ("deno.exe",)), (latest_ffmpeg, ("ffmpeg.exe", "ffprobe.exe"))):
        rel = finder(http)
        print(f"{rel.key} {rel.version}: {rel.asset_name}")
        dest = tmp / rel.asset_name
        http.download(rel.url, dest)
        verify(http, rel, dest)
        if dest.suffix == ".zip":
            with zipfile.ZipFile(dest) as z:
                for name in z.namelist():
                    if Path(name).name in wanted:
                        (out / Path(name).name).write_bytes(z.read(name))
        else:
            dest.replace(out / dest.name)
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()
    missing = [n for n in ("yt-dlp.exe", "deno.exe", "ffmpeg.exe", "ffprobe.exe") if not (out / n).is_file()]
    if missing:
        raise SystemExit("Не удалось получить: " + ", ".join(missing))
    print("Готово:", ", ".join(sorted(p.name for p in out.iterdir())))


if __name__ == "__main__":
    main()
