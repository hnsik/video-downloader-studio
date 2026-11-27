# PyInstaller: onedir-сборка без консоли. Запуск: pyinstaller installer/VideoDownloaderStudio.spec
from pathlib import Path

root = Path(SPECPATH).parent
# Начальные yt-dlp/Deno/FFmpeg кладёт рядом с exe установщик (папка bundled), а не PyInstaller.
datas = []

a = Analysis([str(root / "app.py")], pathex=[str(root)], datas=datas, hiddenimports=[],
             excludes=["yt_dlp", "tkinter"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="VideoDownloaderStudio", console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.datas, name="VideoDownloaderStudio", upx=False)
