import os
import sys
import shutil
import subprocess
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QFileDialog, QComboBox, QTextEdit, QMessageBox,
    QProgressBar, QCheckBox
)
import yt_dlp
from yt_dlp.postprocessor.common import PostProcessor
from yt_dlp.networking.impersonate import ImpersonateTarget

APP_VERSION = "5.1"

def get_ffmpeg():
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if Path(path).is_file():
            return path
    except Exception:
        pass
    return None

def is_tiktok(url):
    host = (urlsplit(url).hostname or "").lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")

def is_youtube(url):
    host = (urlsplit(url).hostname or "").lower()
    return host in ("youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com")

class IphoneConverter(PostProcessor):
    def __init__(self, downloader, ffmpeg_exe, emit):
        super().__init__(downloader)
        self.ffmpeg_exe = ffmpeg_exe
        self.emit = emit

    def run(self, info):
        original = Path(info["filepath"])
        if not original.is_file():
            raise RuntimeError("Файл для конвертации не найден: " + str(original))
        target = original.with_suffix(".mp4")
        temp = original.with_name(original.stem + ".iphone_converting.mp4")
        if temp.exists():
            temp.unlink()
        self.emit("Конвертация для iPhone: H.264 + AAC. Это может занять несколько минут...")
        cmd = [
            self.ffmpeg_exe, "-hide_banner", "-nostdin", "-loglevel", "error",
            "-y", "-i", str(original), "-map", "0:v:0", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "medium", "-crf", "21",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(temp)
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
            if proc.returncode != 0:
                raise RuntimeError("FFmpeg: " + (proc.stderr or "ошибка конвертации")[-900:])
            if not temp.exists() or temp.stat().st_size == 0:
                raise RuntimeError("FFmpeg не создал выходной файл")
            os.replace(str(temp), str(target))
            # Preserve the source if the extension changed.
            info["filepath"] = str(target)
            info["ext"] = "mp4"
            self.emit("Готов MP4 для iPhone: " + str(target))
        finally:
            if temp.exists():
                temp.unlink()
        return [], info

class AppLogger:
    def __init__(self, emit):
        self.emit = emit

    def debug(self, msg):
        if msg.startswith("[debug]"):
            return
        self.emit(msg)

    def warning(self, msg):
        self.emit("ПРЕДУПРЕЖДЕНИЕ: " + msg)

    def error(self, msg):
        self.emit("ОШИБКА yt-dlp: " + msg)


class Worker(QThread):
    message = Signal(str)
    percent = Signal(int)
    completed = Signal(bool, str)

    def __init__(self, links, folder, yt_cookie, tt_cookie, quality, iphone):
        super().__init__()
        self.links = links
        self.folder = folder
        self.yt_cookie = yt_cookie
        self.tt_cookie = tt_cookie
        self.quality = quality
        self.iphone = iphone

    def hook(self, data):
        status = data.get("status")
        if status == "downloading":
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            if total:
                self.percent.emit(min(100, int(data.get("downloaded_bytes", 0) * 100 / total)))
        elif status == "finished":
            self.message.emit("Загрузка файла завершена, выполняется обработка...")

    def run(self):
        ffmpeg = get_ffmpeg()
        self.message.emit("FFmpeg: " + (ffmpeg or "не найден"))
        self.message.emit("Deno: " + (shutil.which("deno") or "не найден"))
        errors = []
        for number, url in enumerate(self.links, 1):
            self.percent.emit(0)
            self.message.emit(f"[{number}/{len(self.links)}] {url}")
            try:
                if self.iphone and not ffmpeg:
                    raise RuntimeError("Для режима iPhone требуется FFmpeg.")
                if self.quality == "best":
                    fmt = "bv*+ba/b"
                else:
                    fmt = f"bv*[height<={self.quality}]+ba/b[height<={self.quality}]/b"
                opts = {
                    "format": fmt,
                    "outtmpl": str(Path(self.folder) / "%(title).160B [%(id)s].%(ext)s"),
                    "noplaylist": True,
                    "windowsfilenames": True,
                    "progress_hooks": [self.hook],
                    "retries": 5,
                    "fragment_retries": 5,
                    "continuedl": True,
                    "overwrites": False,
                    "ignoreerrors": False,
                    "quiet": True,
                    "no_warnings": False,
                    "logger": AppLogger(self.message.emit),
                    "js_runtimes": {"deno": {}},
                }
                if ffmpeg:
                    opts["ffmpeg_location"] = ffmpeg
                    opts["merge_output_format"] = "mp4"
                if is_tiktok(url):
                    # Tested successfully against TikTok with curl_cffi installed.
                    opts["impersonate"] = ImpersonateTarget("chrome")
                    if self.tt_cookie:
                        opts["cookiefile"] = self.tt_cookie
                    self.message.emit("TikTok: Chrome impersonation включена")
                elif is_youtube(url):
                    if self.yt_cookie:
                        opts["cookiefile"] = self.yt_cookie
                    self.message.emit("YouTube: Deno и cookies (если указаны)")
                else:
                    self.message.emit("Другой сайт: стандартный режим yt-dlp")
                with yt_dlp.YoutubeDL(opts) as ydl:
                    if self.iphone:
                        ydl.add_post_processor(IphoneConverter(ydl, ffmpeg, self.message.emit), when="after_move")
                    result = ydl.download([url])
                    if result:
                        raise RuntimeError(f"yt-dlp вернул код {result}")
                self.message.emit("✓ Готово")
                self.percent.emit(100)
            except Exception as exc:
                details = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                self.message.emit("✗ " + (str(exc) or repr(exc)))
                self.message.emit(details)
                errors.append(str(exc) or repr(exc))
        count = len(self.links) - len(errors)
        self.completed.emit(not errors, f"Загружено: {count} из {len(self.links)}." + (
            "\nОшибки:\n" + "\n".join(errors[:3]) if errors else ""
        ))

class Window(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Video Downloader Studio v5.1")
        self.resize(820, 760)
        self.setStyleSheet("""
            QWidget {font-size: 13px;}
            QPushButton {padding: 8px;}
            QLineEdit, QTextEdit, QComboBox {padding: 6px;}
        """)
        layout = QVBoxLayout(self)
        title = QLabel("Video Downloader Studio  •  v5.1")
        title.setStyleSheet("font-size: 23px; font-weight: bold;")
        layout.addWidget(title)
        layout.addWidget(QLabel("Ссылки на видео (по одной на строку):"))
        self.links = QTextEdit()
        self.links.setMaximumHeight(100)
        self.links.setPlaceholderText("https://www.tiktok.com/@.../video/...\nhttps://youtube.com/shorts/...")
        layout.addWidget(self.links)
        quality_row = QHBoxLayout()
        quality_row.addWidget(QLabel("Качество:"))
        self.quality = QComboBox()
        for label, value in (
            ("Лучшее доступное", "best"), ("До 2160p", "2160"),
            ("До 1440p", "1440"), ("До 1080p", "1080"),
            ("До 720p", "720"), ("До 480p", "480"), ("До 360p", "360")
        ):
            self.quality.addItem(label, value)
        quality_row.addWidget(self.quality)
        layout.addLayout(quality_row)
        self.iphone = QCheckBox("Совместимость с iPhone (MP4, H.264 + AAC)")
        self.iphone.setToolTip("Перекодирует видео после скачивания; занимает дополнительное время.")
        layout.addWidget(self.iphone)
        self.youtube_cookie = self.file_row(layout, "Cookies YouTube (.txt):")
        self.tiktok_cookie = self.file_row(layout, "Cookies TikTok (.txt, необязательно):")
        layout.addWidget(QLabel("Папка для скачивания:"))
        folder_row = QHBoxLayout()
        self.folder = QLineEdit(str(Path.home() / "Downloads"))
        folder_row.addWidget(self.folder)
        browse = QPushButton("Обзор")
        browse.clicked.connect(self.choose_folder)
        folder_row.addWidget(browse)
        layout.addLayout(folder_row)
        self.status = QLabel()
        layout.addWidget(self.status)
        self.refresh_status()
        self.rights = QCheckBox("У меня есть право скачивать выбранные видео")
        layout.addWidget(self.rights)
        self.start = QPushButton("Скачать видео")
        self.start.clicked.connect(self.download)
        layout.addWidget(self.start)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        layout.addWidget(QLabel("Журнал загрузок:"))
        self.log = QTextEdit()
        self.log.setReadOnly(True)
        layout.addWidget(self.log)
        open_folder = QPushButton("Открыть папку загрузок")
        open_folder.clicked.connect(self.open_folder)
        layout.addWidget(open_folder)

    def file_row(self, layout, label):
        layout.addWidget(QLabel(label))
        row = QHBoxLayout()
        field = QLineEdit()
        field.setPlaceholderText("Не выбраны")
        row.addWidget(field)
        browse = QPushButton("Выбрать")
        browse.clicked.connect(lambda: self.choose_file(field))
        row.addWidget(browse)
        clear = QPushButton("Очистить")
        clear.clicked.connect(field.clear)
        row.addWidget(clear)
        layout.addLayout(row)
        return field

    def choose_file(self, field):
        filename, _ = QFileDialog.getOpenFileName(self, "Выберите cookies", "", "Text files (*.txt);;All files (*)")
        if filename:
            field.setText(filename)

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def refresh_status(self):
        ffmpeg = "найден" if get_ffmpeg() else "не найден"
        deno = "найден" if shutil.which("deno") else "не найден"
        try:
            import curl_cffi
            curl = "найден"
        except ImportError:
            curl = "не найден"
        self.status.setText(f"FFmpeg: {ffmpeg}  |  Deno: {deno}  |  curl_cffi: {curl}")

    def open_folder(self):
        path = Path(self.folder.text().strip())
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform == "win32":
            os.startfile(str(path))

    def download(self):
        links = [s.strip() for s in self.links.toPlainText().splitlines() if s.strip()]
        if not links or any(not s.startswith(("http://", "https://")) for s in links):
            QMessageBox.warning(self, "Ссылки", "Укажите ссылки HTTP(S), по одной в строке.")
            return
        if not self.rights.isChecked():
            QMessageBox.warning(self, "Подтверждение", "Подтвердите право скачивать видео.")
            return
        clean = lambda value: value.strip().lstrip("\u202a\u202b\u202c\u200e\u200f")
        yt_cookie = clean(self.youtube_cookie.text())
        tt_cookie = clean(self.tiktok_cookie.text())
        for cookie in (yt_cookie, tt_cookie):
            if cookie and not Path(cookie).is_file():
                QMessageBox.warning(self, "Cookies", f"Файл не найден:\n{cookie}")
                return
        try:
            folder = Path(self.folder.text().strip())
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            QMessageBox.warning(self, "Папка", str(exc))
            return
        self.refresh_status()
        self.start.setEnabled(False)
        self.worker = Worker(links, str(folder), yt_cookie, tt_cookie, self.quality.currentData(), self.iphone.isChecked())
        self.worker.message.connect(self.append_log)
        self.worker.percent.connect(self.progress.setValue)
        self.worker.completed.connect(self.finished)
        self.worker.start()

    def append_log(self, message):
        self.log.append(message)
        try:
            folder = Path(self.folder.text().strip())
            folder.mkdir(parents=True, exist_ok=True)
            with (folder / "video_downloader_studio.log").open("a", encoding="utf-8") as file:
                file.write(f"[{datetime.now().isoformat(timespec='seconds')}] {message}\\n")
        except OSError:
            pass

    def finished(self, ok, text):
        self.start.setEnabled(True)
        self.append_log(text)
        if not ok:
            QMessageBox.warning(self, "Результат", text)

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = Window()
    window.show()
    sys.exit(app.exec())
