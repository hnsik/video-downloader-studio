# Сборка установщика
Автоматически: GitHub Actions `build-windows.yml` (windows-latest): тесты → `tools/fetch_bundled.py` (yt-dlp, Deno, FFmpeg с проверкой SHA-256)
→ PyInstaller (`installer/VideoDownloaderStudio.spec`) → проверка запуска → Inno Setup (`installer/VideoDownloaderStudio.iss`).
Результат: артефакт `VideoDownloaderStudio-Update-Windows-x64-Setup.exe`; по тегу `v*` создаётся GitHub Release.

Вручную (Windows, Python 3.12, Inno Setup 6):
```
pip install -r requirements-dev.txt
python tools/fetch_bundled.py bundled
pyinstaller --noconfirm installer/VideoDownloaderStudio.spec
iscc /DAppVersion=6.0.0 installer\VideoDownloaderStudio.iss
```
