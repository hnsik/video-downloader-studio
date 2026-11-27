"""Графический интерфейс Video Downloader Studio 6 (PySide6).

Все длительные операции выполняются в фоновых потоках (BgTask); обратные вызовы очереди
(приходят из рабочих потоков) передаются в интерфейс только через сигналы Qt.
Логика форматирования вынесена в viewmodel.py и покрыта тестами.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget, QAbstractItemView,
)

from . import APP_NAME, __version__, diagnostics, paths
from .auth import check_auth, detect_browsers, validate_cookie_file
from .components import SPECS
from .core import AppCore
from .formats import AUDIO_FORMATS, CODECS, CONTAINERS, MODES, QUALITIES
from .jobqueue import DONE, FAILED
from .platforms import PLATFORMS
from .redact import clean
from .viewmodel import fmt_size, job_row, parse_links, short_path

STYLE = """
QWidget {font-size: 13px;}
QPushButton {padding: 7px 12px;}
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox {padding: 5px;}
QLabel#title {font-size: 22px; font-weight: bold;}
QLabel#hint {color: #666;}
"""


def open_path(path: str | Path) -> None:
    path = Path(path)
    target = path if path.is_dir() else path.parent
    target.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(str(target))  # noqa: S606 — открываем папку в проводнике
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(target)])
    else:
        subprocess.Popen(["xdg-open", str(target)])


def select_file(path: str | Path) -> None:
    """Открывает проводник с выделенным файлом."""
    p = Path(path)
    if sys.platform == "win32" and p.is_file():
        subprocess.Popen(["explorer", "/select,", str(p)])
    else:
        open_path(p.parent if p.is_file() else p)


class BgTask(QThread):
    """Выполняет функцию в фоне; результат/ошибка возвращаются сигналами."""
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self._fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self._fn())
        except Exception as exc:  # noqa: BLE001 — показываем пользователю, не роняем интерфейс
            self.failed.emit(clean(str(exc) or exc.__class__.__name__))


class Bridge(QObject):
    """Потокобезопасный мост: события из рабочих потоков → сигналы Qt."""
    job_event = Signal(object, str, str)
    log = Signal(str)
    comp_progress = Signal(str)


def run_bg(owner: QWidget, fn, on_done=None, on_fail=None) -> BgTask:
    task = BgTask(fn, owner)
    if on_done:
        task.done.connect(on_done)
    if on_fail:
        task.failed.connect(on_fail)
    task.finished.connect(task.deleteLater)
    owner._tasks = getattr(owner, "_tasks", [])
    owner._tasks.append(task)
    task.finished.connect(lambda t=task: owner._tasks.remove(t) if t in owner._tasks else None)
    task.start()
    return task


def combo(items: dict[str, str], current: str = "") -> QComboBox:
    box = QComboBox()
    for value, label in items.items():
        box.addItem(label, value)
    idx = box.findData(current)
    if idx >= 0:
        box.setCurrentIndex(idx)
    return box


def make_table(headers: list[str], stretch_col: int = 0) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.horizontalHeader().setSectionResizeMode(stretch_col, QHeaderView.Stretch)
    return table


def hbox(*widgets) -> QHBoxLayout:
    row = QHBoxLayout()
    for w in widgets:
        if w is None:
            row.addStretch(1)
        else:
            row.addWidget(w)
    return row


def button(text: str, handler, tip: str = "") -> QPushButton:
    b = QPushButton(text)
    b.clicked.connect(lambda _=False: handler())
    if tip:
        b.setToolTip(tip)
    return b


# ---------------------------------------------------------------------------------------------
class ErrorDialog(QDialog):
    """Понятное описание ошибки + раскрываемые технические подробности."""

    def __init__(self, info, parent=None, on_action=None):
        super().__init__(parent)
        self.setWindowTitle("Ошибка загрузки")
        self.resize(640, 420)
        lay = QVBoxLayout(self)
        head = QLabel(f"<b>{info.title}</b>")
        head.setWordWrap(True)
        lay.addWidget(head)
        for caption, text in (("Вероятная причина", info.cause), ("Что делать", info.solution)):
            lab = QLabel(f"<b>{caption}:</b> {text}")
            lab.setWordWrap(True)
            lay.addWidget(lab)
        if on_action:
            row = QHBoxLayout()
            names = {"update_ytdlp": ("Обновить yt-dlp", "ytdlp"), "update_deno": ("Обновить Deno", "deno"),
                     "update_ffmpeg": ("Обновить FFmpeg", "ffmpeg")}
            for act in info.actions:
                if act in names:
                    caption, key = names[act]
                    row.addWidget(button(caption, lambda k=key: (on_action("update", k), self.accept())))
                elif act == "use_firefox":
                    row.addWidget(button("Настроить Firefox", lambda: (on_action("auth", "firefox"), self.accept())))
            row.addStretch(1)
            lay.addLayout(row)
        box = QGroupBox("Технические подробности")
        box.setCheckable(True)
        box.setChecked(False)
        inner = QVBoxLayout(box)
        text = QPlainTextEdit(clean(info.technical or ""))
        text.setReadOnly(True)
        inner.addWidget(text)
        box.toggled.connect(text.setVisible)
        text.setVisible(False)
        lay.addWidget(box, 1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        copy = buttons.addButton("Копировать логи", QDialogButtonBox.ActionRole)
        copy.clicked.connect(lambda: QGuiApplication.clipboard().setText(clean(info.technical or "")))
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)


# ---------------------------------------------------------------------------------------------
class DownloadTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win, self.core = win, win.core
        s = self.core.settings
        lay = QVBoxLayout(self)
        title = QLabel(f"{APP_NAME}  •  v{__version__}")
        title.setObjectName("title")
        lay.addWidget(title)
        lay.addWidget(QLabel("Ссылки на видео (по одной в строке, можно вставить сразу несколько):"))
        self.links = QPlainTextEdit()
        self.links.setPlaceholderText("https://www.youtube.com/shorts/...\nhttps://www.tiktok.com/@.../video/...")
        self.links.setMaximumHeight(130)
        lay.addWidget(self.links)

        form = QFormLayout()
        self.container = combo(CONTAINERS, s.get("container"))
        self.quality = combo(QUALITIES, s.get("quality"))
        self.codec = combo(CODECS, s.get("video_codec"))
        self.mode = combo(MODES, s.get("mode"))
        self.audio = combo(AUDIO_FORMATS, s.get("audio_format"))
        form.addRow("Контейнер:", self.container)
        form.addRow("Качество:", self.quality)
        form.addRow("Видеокодек:", self.codec)
        form.addRow("Режим:", self.mode)
        form.addRow("Формат аудио:", self.audio)
        lay.addLayout(form)

        self.subs = QCheckBox("Скачать субтитры")
        self.subs.setChecked(bool(s.get("subtitles")))
        self.thumbs = QCheckBox("Скачать обложку")
        self.thumbs.setChecked(bool(s.get("thumbnails")))
        self.ios = QCheckBox("Совместимость с iPhone (MP4, H.264 + AAC; перекодирование при необходимости)")
        self.ios.setChecked(bool(s.get("ios_compat")))
        lay.addLayout(hbox(self.subs, self.thumbs, None))
        lay.addWidget(self.ios)

        lay.addWidget(QLabel("Папка для скачивания:"))
        self.folder = QLineEdit(s.get("download_dir"))
        lay.addLayout(hbox(self.folder, button("Обзор", self.choose_folder), button("Открыть", lambda: open_path(self.folder.text()))))
        lay.addWidget(QLabel("Шаблон имени файла (yt-dlp):"))
        self.template = QLineEdit(s.get("filename_template"))
        lay.addWidget(self.template)

        self.preset = QComboBox()
        self.preset.setEditable(False)
        self.reload_presets()
        lay.addLayout(hbox(QLabel("Пресет:"), self.preset, button("Применить", self.apply_preset),
                           button("Сохранить текущие как пресет…", self.save_preset), button("Удалить", self.delete_preset), None))

        self.rights = QCheckBox("У меня есть право скачивать выбранные видео")
        self.rights.setVisible(bool(s.get("confirm_rights", True)))
        lay.addWidget(self.rights)
        self.go = button("Добавить в очередь и скачать", self.start)
        self.go.setStyleSheet("font-weight: bold; padding: 10px;")
        lay.addWidget(self.go)
        self.status = QLabel("")
        self.status.setObjectName("hint")
        lay.addWidget(self.status)
        lay.addStretch(1)

    # -- настройки ⇄ форма
    def collect(self) -> dict:
        return {
            "container": self.container.currentData(), "quality": self.quality.currentData(),
            "video_codec": self.codec.currentData(), "mode": self.mode.currentData(),
            "audio_format": self.audio.currentData(), "subtitles": self.subs.isChecked(),
            "thumbnails": self.thumbs.isChecked(), "ios_compat": self.ios.isChecked(),
            "download_dir": self.folder.text().strip(), "filename_template": self.template.text().strip(),
        }

    def apply(self, values: dict) -> None:
        for box, key in ((self.container, "container"), (self.quality, "quality"), (self.codec, "video_codec"),
                          (self.mode, "mode"), (self.audio, "audio_format")):
            idx = box.findData(values.get(key))
            if idx >= 0:
                box.setCurrentIndex(idx)
        self.subs.setChecked(bool(values.get("subtitles", self.subs.isChecked())))
        self.thumbs.setChecked(bool(values.get("thumbnails", self.thumbs.isChecked())))
        self.ios.setChecked(bool(values.get("ios_compat", self.ios.isChecked())))
        if values.get("download_dir"):
            self.folder.setText(values["download_dir"])
        if values.get("filename_template"):
            self.template.setText(values["filename_template"])

    def reload_presets(self) -> None:
        self.preset.clear()
        self.preset.addItems(sorted(self.core.settings.get("presets", {}).keys()))

    def apply_preset(self) -> None:
        values = self.core.settings.get("presets", {}).get(self.preset.currentText())
        if values:
            self.apply(values)

    def save_preset(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "Пресет", "Название пресета:")
        if ok and name.strip():
            presets = dict(self.core.settings.get("presets", {}))
            presets[name.strip()] = self.collect()
            self.core.settings.set("presets", presets)
            self.core.settings.save()
            self.reload_presets()
            self.preset.setCurrentText(name.strip())

    def delete_preset(self) -> None:
        name = self.preset.currentText()
        presets = dict(self.core.settings.get("presets", {}))
        if name in presets:
            del presets[name]
            self.core.settings.set("presets", presets)
            self.core.settings.save()
            self.reload_presets()

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Выберите папку", self.folder.text())
        if folder:
            self.folder.setText(folder)

    def start(self) -> None:
        good, bad = parse_links(self.links.toPlainText())
        if not good:
            QMessageBox.warning(self, "Ссылки", "Укажите хотя бы одну ссылку HTTP(S).")
            return
        if bad:
            QMessageBox.information(self, "Ссылки", "Эти строки не похожи на ссылки и пропущены:\n" + "\n".join(bad[:10]))
        if self.core.settings.get("confirm_rights", True) and not self.rights.isChecked():
            QMessageBox.warning(self, "Подтверждение", "Подтвердите право скачивать выбранные видео.")
            return
        values = self.collect()
        try:
            Path(values["download_dir"]).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.warning(self, "Папка", f"Не удалось создать папку:\n{exc}")
            return
        self.core.settings.update(values)
        self.core.settings.save()
        if self.core.components.path("ytdlp") is None:
            QMessageBox.warning(self, "Движок не найден", "yt-dlp не установлен. Откройте «Компоненты и обновления» и нажмите «Обновить все».")
            self.win.show_tab("components")
            return
        added = skipped = 0
        for url in good:
            if self.core.settings.get("prevent_duplicates", True):
                dup = self.core.queue.find_duplicate(url)
                if dup:
                    where = "уже в очереди" if dup.get("where") == "queue" else "уже скачано"
                    answer = QMessageBox.question(self, "Повтор", f"{dup.get('title') or url}\n\nЭто видео {where}. Скачать ещё раз?")
                    if answer != QMessageBox.Yes:
                        skipped += 1
                        continue
            self.core.queue.add(url, self.core.options_for(url))
            added += 1
        self.status.setText(f"Добавлено в очередь: {added}" + (f", пропущено: {skipped}" if skipped else ""))
        if added:
            self.links.clear()
            self.win.show_tab("queue")


# ---------------------------------------------------------------------------------------------
class QueueTab(QWidget):
    COLS = ["Видео", "Платформа", "Статус", "Прогресс", "Скорость", "Размер", "Осталось"]

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win, self.core = win, win.core
        self.rows: dict[str, int] = {}
        lay = QVBoxLayout(self)
        self.table = make_table(self.COLS)
        self.table.doubleClicked.connect(lambda _=None: self.details())
        lay.addWidget(self.table, 1)
        self.overall = QProgressBar()
        self.overall_label = QLabel("Очередь пуста")
        lay.addLayout(hbox(self.overall_label, self.overall))
        lay.addLayout(hbox(
            button("Пауза", lambda: self.each(self.core.queue.pause)),
            button("Продолжить", lambda: self.each(self.core.queue.resume)),
            button("Отмена", lambda: self.each(self.core.queue.cancel)),
            button("Повторить", lambda: self.each(self.core.queue.retry)),
            button("Подробности / ошибка", self.details),
            button("Удалить из списка", lambda: self.each(self.core.queue.remove)),
            None))
        lay.addLayout(hbox(
            button("Пауза всех", self.core.queue.pause_all), button("Продолжить все", self.core.queue.resume_all),
            button("Очистить завершённые", self.core.queue.clear_finished),
            button("Открыть папку загрузок", lambda: open_path(self.core.settings.get("download_dir"))), None))
        for job in self.core.queue.jobs.values():
            self.on_event(job, "added", "")

    def selected_ids(self) -> list[str]:
        ids = []
        for idx in self.table.selectionModel().selectedRows():
            item = self.table.item(idx.row(), 0)
            if item:
                ids.append(item.data(Qt.UserRole))
        return ids

    def each(self, fn) -> None:
        ids = self.selected_ids()
        if not ids:
            QMessageBox.information(self, "Очередь", "Выберите строки в таблице.")
            return
        for job_id in ids:
            try:
                fn(job_id)
            except Exception as exc:  # noqa: BLE001
                QMessageBox.warning(self, "Очередь", clean(str(exc)))

    def details(self) -> None:
        ids = self.selected_ids()
        if not ids:
            return
        job = self.core.queue.jobs.get(ids[0])
        if not job:
            return
        if job.error:
            ErrorDialog(job.error, self, self.win.handle_action).exec()
        else:
            QMessageBox.information(self, "Задача", f"{job.title or job.url}\n\nСтатус: {row_status(job)}\nФайл: {job.path or '—'}\n\n" + "\n".join(job.notes[-8:]))

    def on_event(self, job, event: str, extra: str) -> None:
        if event == "removed":
            self.rebuild()
            return
        if event == "added" or job.id not in self.rows:
            self.rebuild()
            return
        self.update_row(self.rows[job.id], job)
        self.update_overall()

    def rebuild(self) -> None:
        jobs = list(self.core.queue.jobs.values())
        order = [self.core.queue.jobs[i] for i in self.core.queue._order if i in self.core.queue.jobs] or jobs
        selected = set(self.selected_ids())
        self.table.setRowCount(len(order))
        self.rows = {}
        for r, job in enumerate(order):
            self.rows[job.id] = r
            self.update_row(r, job)
            if job.id in selected:
                self.table.selectRow(r)
        self.update_overall()

    def update_row(self, r: int, job) -> None:
        row = job_row(job)
        values = [row["title"], PLATFORMS.get(row["platform"], row["platform"]), row["status"], f'{row["percent"]}%',
                  row["speed"], row["size"], row["eta"]]
        for c, v in enumerate(values):
            item = self.table.item(r, c) or QTableWidgetItem()
            item.setText(str(v))
            if c == 0:
                item.setData(Qt.UserRole, job.id)
                item.setToolTip(job.url)
            if c == 2 and job.status == FAILED and job.error:
                item.setText(f"Ошибка: {job.error.title}")
                item.setToolTip(job.error.solution)
            self.table.setItem(r, c, item)

    def update_overall(self) -> None:
        pct, done, total = self.core.queue.overall()
        self.overall.setValue(int(pct))
        self.overall_label.setText(f"Готово {done} из {total}" if total else "Очередь пуста")


def row_status(job) -> str:
    from .jobqueue import STATUS_RU
    return STATUS_RU.get(job.status, job.status)


# ---------------------------------------------------------------------------------------------
class HistoryTab(QWidget):
    COLS = ["Дата", "Название", "Платформа", "Статус", "Размер", "Файл"]

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win, self.core = win, win.core
        self.records: list[dict] = []
        lay = QVBoxLayout(self)
        self.table = make_table(self.COLS, 1)
        lay.addWidget(self.table, 1)
        lay.addLayout(hbox(
            button("Обновить", self.reload), button("Показать файл", self.show_file),
            button("Скачать снова", self.redownload), button("Удалить запись", self.delete),
            button("Очистить историю", self.clear), None))
        self.reload()

    def reload(self) -> None:
        import datetime
        self.records = self.core.store.list_history()
        self.table.setRowCount(len(self.records))
        for r, rec in enumerate(self.records):
            when = datetime.datetime.fromtimestamp(rec.get("finished_at") or 0).strftime("%Y-%m-%d %H:%M")
            status = "Готово" if rec.get("status") == "done" else "Ошибка"
            vals = [when, rec.get("title") or rec.get("url"), PLATFORMS.get(rec.get("platform"), rec.get("platform") or ""),
                    status, fmt_size(rec.get("size")), short_path(rec.get("path") or "")]
            for c, v in enumerate(vals):
                self.table.setItem(r, c, QTableWidgetItem(str(v)))

    def current(self) -> dict | None:
        rows = self.table.selectionModel().selectedRows()
        return self.records[rows[0].row()] if rows else None

    def show_file(self) -> None:
        rec = self.current()
        if rec and rec.get("path"):
            select_file(rec["path"])

    def redownload(self) -> None:
        rec = self.current()
        if rec:
            self.core.queue.add(rec["url"], self.core.options_for(rec["url"]))
            self.win.show_tab("queue")

    def delete(self) -> None:
        rec = self.current()
        if rec:
            self.core.store.delete_history(rec["id"])
            self.reload()

    def clear(self) -> None:
        if QMessageBox.question(self, "История", "Очистить всю историю? Скачанные файлы не удаляются.") == QMessageBox.Yes:
            self.core.store.clear_history()
            self.reload()


# ---------------------------------------------------------------------------------------------
class AuthTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win, self.core = win, win.core
        self.browsers = []
        lay = QVBoxLayout(self)
        note = QLabel("Авторизация нужна только для контента, к которому у вас есть законный доступ. Пароли не сохраняются, "
                      "cookies не попадают в журналы и отчёты. Если Chrome выдаёт ошибку DPAPI — используйте Firefox.")
        note.setWordWrap(True)
        note.setObjectName("hint")
        lay.addWidget(note)
        form = QFormLayout()
        self.platform = combo(PLATFORMS, "youtube")
        self.method = QComboBox()
        self.profile = QComboBox()
        self.cookie_file = QLineEdit()
        form.addRow("Платформа:", self.platform)
        form.addRow("Способ входа:", self.method)
        form.addRow("Профиль браузера:", self.profile)
        form.addRow("Файл cookies.txt:", hbox_widget(self.cookie_file, button("Выбрать…", self.pick_file)))
        lay.addLayout(form)
        self.result = QPlainTextEdit()
        self.result.setReadOnly(True)
        self.result.setMaximumHeight(150)
        lay.addLayout(hbox(
            button("Сохранить", self.save), button("Проверить авторизацию", self.check),
            button("Сбросить для платформы", self.reset), button("Сбросить для всех", lambda: self.reset(True)),
            button("Найти браузеры заново", self.detect), None))
        lay.addWidget(self.result)
        self.summary = QLabel("")
        self.summary.setObjectName("hint")
        self.summary.setWordWrap(True)
        lay.addWidget(self.summary)
        lay.addStretch(1)
        self.platform.currentIndexChanged.connect(self.load_current)
        self.method.currentIndexChanged.connect(self.fill_profiles)
        self.detect()

    def detect(self) -> None:
        self.browsers = detect_browsers()
        self.method.blockSignals(True)
        self.method.clear()
        self.method.addItem("Без авторизации", "none")
        for b in self.browsers:
            self.method.addItem(b.name, b.key)
        self.method.addItem("Файл cookies.txt (Netscape)", "file")
        self.method.blockSignals(False)
        found = ", ".join(b.name for b in self.browsers) or "не найдены"
        self.summary.setText(f"Найденные браузеры: {found}")
        self.load_current()

    def fill_profiles(self) -> None:
        self.profile.clear()
        key = self.method.currentData()
        for b in self.browsers:
            if b.key == key:
                for p in b.profiles:
                    self.profile.addItem(p.name, p.id)
        self.profile.setEnabled(self.profile.count() > 0)
        self.cookie_file.setEnabled(key == "file")

    def load_current(self) -> None:
        auth = self.core.settings.auth_for(self.platform.currentData())
        idx = self.method.findData(auth.get("method", "none"))
        self.method.setCurrentIndex(max(idx, 0))
        self.fill_profiles()
        pidx = self.profile.findData(auth.get("profile", ""))
        if pidx >= 0:
            self.profile.setCurrentIndex(pidx)
        self.cookie_file.setText(auth.get("cookie_file", ""))

    def current_auth(self) -> dict:
        return {"method": self.method.currentData() or "none", "profile": self.profile.currentData() or "",
                "cookie_file": self.cookie_file.text().strip().lstrip("‪‫‬‎‏")}

    def pick_file(self) -> None:
        name, _ = QFileDialog.getOpenFileName(self, "Выберите cookies.txt", "", "Text files (*.txt);;All files (*)")
        if name:
            self.cookie_file.setText(name)

    def save(self) -> bool:
        a = self.current_auth()
        if a["method"] == "file":
            info = validate_cookie_file(a["cookie_file"])
            if not info.ok:
                QMessageBox.warning(self, "cookies.txt", info.message)
                return False
        self.core.settings.set_auth(self.platform.currentData(), a["method"], a["profile"], a["cookie_file"])
        self.core.settings.save()
        self.result.setPlainText("Настройки авторизации сохранены.")
        return True

    def reset(self, all_platforms: bool = False) -> None:
        self.core.settings.reset_auth(None if all_platforms else self.platform.currentData())
        self.core.settings.save()
        self.load_current()
        self.result.setPlainText("Настройки авторизации сброшены.")

    def check(self) -> None:
        if not self.save():
            return
        platform, auth = self.platform.currentData(), self.current_auth()
        ytdlp = self.core.components.path("ytdlp")
        self.result.setPlainText("Проверяю… это может занять до минуты.")
        run_bg(self, lambda: check_auth(ytdlp, auth, platform), self.show_check,
               lambda msg: self.result.setPlainText("Ошибка проверки: " + msg))

    def show_check(self, res) -> None:
        state = {True: "вход выполнен", False: "вход НЕ обнаружен", None: "не удалось определить"}[res.logged_in]
        text = f"{'OK' if res.ok else 'Проблема'}: {res.message}\nCookies получено: {res.cookies_loaded}\nСостояние: {state}"
        if res.hint:
            text += f"\nПодсказка: {res.hint}"
        self.result.setPlainText(clean(text))


def hbox_widget(*widgets) -> QWidget:
    w = QWidget()
    w.setLayout(hbox(*widgets))
    w.layout().setContentsMargins(0, 0, 0, 0)
    return w


# ---------------------------------------------------------------------------------------------
class ComponentsTab(QWidget):
    COLS = ["Компонент", "Установлена", "Последняя", "Источник", "Статус", "Проверено", "Обновление", "Путь"]

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win, self.core = win, win.core
        self.cm = self.core.components
        self.busy = False
        self.keys: list[str] = []
        lay = QVBoxLayout(self)
        self.table = make_table(self.COLS, 7)
        lay.addWidget(self.table, 2)
        opts = QHBoxLayout()
        self.channel = combo({"stable": "Stable (по умолчанию)", "nightly": "Nightly (для yt-dlp)"}, self.core.settings.get("channel"))
        self.channel.currentIndexChanged.connect(self.channel_changed)
        self.auto = QCheckBox("Проверять обновления при запуске")
        self.auto.setChecked(bool(self.core.settings.get("auto_check_updates", True)))
        self.auto.toggled.connect(lambda v: (self.core.settings.set("auto_check_updates", v), self.core.settings.save()))
        opts.addWidget(QLabel("Канал yt-dlp:"))
        opts.addWidget(self.channel)
        opts.addWidget(self.auto)
        opts.addStretch(1)
        lay.addLayout(opts)
        lay.addLayout(hbox(
            button("Проверить все компоненты", self.check_all), button("Обновить все компоненты", self.update_all),
            button("Обновить выбранный", lambda: self.on_selected(self.update_one)), None))
        lay.addLayout(hbox(
            button("Переустановить", lambda: self.on_selected(lambda k: self.update_one(k, True))),
            button("Проверить работоспособность", lambda: self.on_selected(self.health)),
            button("Откатить обновление", lambda: self.on_selected(self.rollback)),
            button("Открыть папку", lambda: self.on_selected(self.open_folder)), None))
        self.progress = QProgressBar()
        self.message = QLabel("")
        lay.addWidget(self.message)
        lay.addWidget(self.progress)
        lay.addWidget(QLabel("История обновлений:"))
        self.history = QPlainTextEdit()
        self.history.setReadOnly(True)
        lay.addWidget(self.history, 1)
        self.win.bridge.comp_progress.connect(self.on_progress)
        self.refresh()

    def refresh(self) -> None:
        statuses = self.cm.statuses()
        self.keys = [s.key for s in statuses]
        self.table.setRowCount(len(statuses))
        for r, s in enumerate(statuses):
            health = ("работает" if s.healthy else (s.health or "не работает")) if s.path else "не установлен"
            vals = [s.title, s.installed or "—", s.latest or "—", s.source, health, s.checked_at or "—",
                    "есть обновление" if s.update_available else "актуально" if s.latest else "—", s.path or "—"]
            for c, v in enumerate(vals):
                item = QTableWidgetItem(str(v))
                if c == 6 and s.update_available:
                    item.setForeground(Qt.darkGreen)
                if c == 4 and s.path and not s.healthy:
                    item.setForeground(Qt.red)
                self.table.setItem(r, c, item)
        lines = [f"{h['time']}  {h['component']}: {h['action']} {'✓' if h['ok'] else '✗'} {h.get('from') or ''}→{h.get('to') or ''}  {h['message']}"
                 for h in self.cm.history()[:60]]
        self.history.setPlainText(clean("\n".join(lines)))

    def selected_key(self) -> str | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            QMessageBox.information(self, "Компоненты", "Выберите компонент в таблице.")
            return None
        return self.keys[rows[0].row()]

    def on_selected(self, fn) -> None:
        key = self.selected_key()
        if key:
            fn(key)

    def on_progress(self, text: str) -> None:
        self.message.setText(clean(text))

    def channel_changed(self) -> None:
        self.core.settings.set("channel", self.channel.currentData())
        self.core.settings.save()
        self.check_all()

    def run_op(self, fn, title: str) -> None:
        if self.busy:
            QMessageBox.information(self, "Компоненты", "Другая операция ещё выполняется.")
            return
        self.busy = True
        self.progress.setRange(0, 0)
        self.message.setText(title)
        run_bg(self, fn, lambda res: self.finish(res), lambda err: self.finish(err, True))

    def finish(self, result, failed: bool = False) -> None:
        self.busy = False
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        if failed:
            self.message.setText("Ошибка: " + str(result))
            QMessageBox.warning(self, "Компоненты", str(result))
        else:
            text = result if isinstance(result, str) else "Готово"
            self.message.setText(clean(text))
            self.win.bridge.log.emit(clean(text))
        self.refresh()

    def progress_cb(self):
        def cb(*args):
            text = " ".join(str(a) for a in args)
            self.win.bridge.comp_progress.emit(text)
        return cb

    def check_all(self) -> None:
        def work():
            errors = self.cm.check_latest()
            return "Проверка завершена" + (": " + "; ".join(f"{k}: {v}" for k, v in errors.items()) if errors else "")
        self.run_op(work, "Проверяю последние версии…")

    def update_all(self) -> None:
        cb = self.progress_cb()

        def work():
            res = self.cm.update_all(progress=cb)
            self.cm.check_latest()
            return "\n".join(f"{k}: {'OK' if ok else 'ошибка'} — {msg}" for k, ok, msg in res)
        self.run_op(work, "Обновляю компоненты…")

    def update_one(self, key: str, force: bool = False) -> None:
        cb = self.progress_cb()

        def work():
            msg = self.cm.update(key, force=force, progress=cb)
            self.cm.check_latest([key])
            return msg
        self.run_op(work, f"Обновляю: {SPECS[key].title}…")

    def health(self, key: str) -> None:
        def work():
            ok, msg = self.cm.health(key)
            return f"{SPECS[key].title}: {'работает' if ok else 'НЕ работает'} — {msg}"
        self.run_op(work, "Проверяю работоспособность…")

    def rollback(self, key: str) -> None:
        if QMessageBox.question(self, "Откат", f"Вернуть предыдущую версию компонента «{SPECS[key].title}»?") == QMessageBox.Yes:
            self.run_op(lambda: self.cm.rollback(key), "Откатываю…")

    def open_folder(self, key: str) -> None:
        path = self.cm.path(key)
        open_path(path.parent if path else self.cm.root)


# ---------------------------------------------------------------------------------------------
class DiagnosticsTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win, self.core = win, win.core
        self.report = None
        lay = QVBoxLayout(self)
        self.text = QPlainTextEdit()
        self.text.setReadOnly(True)
        self.text.setStyleSheet("font-family: Consolas, 'Courier New', monospace;")
        self.run_btn = button("Полная диагностика", self.run)
        lay.addLayout(hbox(self.run_btn, button("Экспорт TXT…", lambda: self.export("txt")), button("Экспорт JSON…", lambda: self.export("json")),
                           button("Копировать", lambda: QGuiApplication.clipboard().setText(self.text.toPlainText())), None))
        self.progress = QLabel("")
        lay.addWidget(self.progress)
        lay.addWidget(self.text, 1)

    def run(self) -> None:
        self.run_btn.setEnabled(False)
        self.text.setPlainText("Выполняется диагностика…")
        run_bg(self, lambda: diagnostics.run_full(self.core, progress=lambda s: self.win.bridge.comp_progress.emit(s)), self.done,
               lambda err: (self.run_btn.setEnabled(True), self.text.setPlainText("Ошибка диагностики: " + err)))

    def done(self, report) -> None:
        self.report = report
        self.run_btn.setEnabled(True)
        self.text.setPlainText(clean(report.to_text()))

    def export(self, kind: str) -> None:
        if not self.report:
            QMessageBox.information(self, "Диагностика", "Сначала выполните «Полную диагностику».")
            return
        name, _ = QFileDialog.getSaveFileName(self, "Сохранить отчёт", f"vds-diagnostics.{kind}", f"*.{kind}")
        if name:
            diagnostics.export(self.report, Path(name))
            QMessageBox.information(self, "Диагностика", "Отчёт сохранён. Cookies, токены и пароли в отчёт не попадают.")


# ---------------------------------------------------------------------------------------------
class SettingsTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.win, self.core = win, win.core
        s = self.core.settings
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 8)
        self.concurrency.setValue(int(s.get("concurrency", 2)))
        self.rate = QLineEdit(s.get("rate_limit", ""))
        self.rate.setPlaceholderText("например 2M или 500K; пусто — без ограничения")
        self.retries = QSpinBox()
        self.retries.setRange(0, 20)
        self.retries.setValue(int(s.get("retries", 5)))
        self.stall = QSpinBox()
        self.stall.setRange(30, 3600)
        self.stall.setSuffix(" с")
        self.stall.setValue(int(s.get("stall_timeout", 180)))
        form.addRow("Одновременных загрузок:", self.concurrency)
        form.addRow("Ограничение скорости:", self.rate)
        form.addRow("Повторы при сбое сети (yt-dlp):", self.retries)
        form.addRow("Тайм-аут без прогресса:", self.stall)
        lay.addLayout(form)
        self.probe = self.check("Проверять итоговый файл через FFprobe", "probe_after", True)
        self.dups = self.check("Предупреждать о повторном скачивании", "prevent_duplicates", True)
        self.rights = self.check("Требовать подтверждение права на скачивание", "confirm_rights", True)
        self.ejs = self.check("Разрешить yt-dlp загружать решатель EJS с GitHub (если встроенный не подходит)", "ejs_remote", False)
        self.system = self.check("Использовать yt-dlp/FFmpeg/Deno из PATH, если собственных нет", "allow_system_path", False)
        for c in (self.probe, self.dups, self.rights, self.ejs, self.system):
            lay.addWidget(c)
        box = QGroupBox("Имитация браузера (impersonate) по платформам")
        pf = QFormLayout(box)
        self.imp: dict[str, QLineEdit] = {}
        for key, name in PLATFORMS.items():
            edit = QLineEdit((s.get("platform_options", {}).get(key, {}) or {}).get("impersonate", ""))
            edit.setPlaceholderText("пусто — выключено; например chrome")
            self.imp[key] = edit
            pf.addRow(name + ":", edit)
        lay.addWidget(box)
        lay.addLayout(hbox(button("Сохранить настройки", self.save), button("Открыть папку данных", lambda: open_path(paths.data_dir())),
                           button("Открыть папку журналов", lambda: open_path(paths.log_dir())), None))
        info = QLabel(f"Данные: {paths.data_dir()}\nКомпоненты: {paths.components_dir()}")
        info.setObjectName("hint")
        info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(info)
        lay.addStretch(1)

    def check(self, text: str, key: str, default: bool) -> QCheckBox:
        c = QCheckBox(text)
        c.setChecked(bool(self.core.settings.get(key, default)))
        c.setProperty("key", key)
        return c

    def save(self) -> None:
        s = self.core.settings
        values = {"concurrency": self.concurrency.value(), "rate_limit": self.rate.text().strip(), "retries": self.retries.value(),
                  "stall_timeout": self.stall.value()}
        for c in (self.probe, self.dups, self.rights, self.ejs, self.system):
            values[c.property("key")] = c.isChecked()
        s.update(values)
        opts = dict(s.get("platform_options", {}))
        for key, edit in self.imp.items():
            opts[key] = {**(opts.get(key) or {}), "impersonate": edit.text().strip()}
        s.set("platform_options", opts)
        s.save()
        self.core.queue.set_parallel(self.concurrency.value())
        QMessageBox.information(self, "Настройки", "Настройки сохранены.")


# ---------------------------------------------------------------------------------------------
class LogTab(QWidget):
    def __init__(self, win: "MainWindow"):
        super().__init__()
        lay = QVBoxLayout(self)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(5000)
        lay.addWidget(self.view, 1)
        lay.addLayout(hbox(
            button("Копировать", lambda: QGuiApplication.clipboard().setText(self.view.toPlainText())),
            button("Очистить окно", self.view.clear), button("Открыть папку журналов", lambda: open_path(paths.log_dir())), None))
        win.bridge.log.connect(self.add)

    def add(self, line: str) -> None:
        import datetime
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.view.appendPlainText(f"[{stamp}] {clean(line)}")


# ---------------------------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self, core: AppCore):
        super().__init__()
        self.core = core
        self.bridge = Bridge()
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(1000, 780)
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.pages: dict[str, QWidget] = {}
        for key, title, cls in (
            ("download", "Загрузка видео", DownloadTab), ("queue", "Очередь", QueueTab), ("history", "История", HistoryTab),
            ("auth", "Авторизация", AuthTab), ("components", "Компоненты и обновления", ComponentsTab),
            ("diagnostics", "Диагностика", DiagnosticsTab), ("settings", "Настройки", SettingsTab), ("log", "Журнал событий", LogTab),
        ):
            page = cls(self)
            self.pages[key] = page
            self.tabs.addTab(page, title)
        self.bridge.job_event.connect(self.on_job_event)
        core.queue.subscribe(self._queue_callback)
        self.bridge.log.emit(f"Запуск {APP_NAME} {__version__}")
        if core.settings.migrated_from:
            self.bridge.log.emit(f"Настройки перенесены из предыдущей версии: {core.settings.migrated_from}")
        QTimer.singleShot(800, self.startup_checks)

    def _queue_callback(self, job, event: str) -> None:
        """Вызывается из рабочих потоков: событие вида «имя» или «имя:подробности»."""
        name, _, extra = event.partition(":")
        self.bridge.job_event.emit(job, name, extra)

    def show_tab(self, key: str) -> None:
        self.tabs.setCurrentWidget(self.pages[key])

    def on_job_event(self, job, event: str, extra: str) -> None:
        self.pages["queue"].on_event(job, event, extra)
        if event == "log" and extra:
            self.bridge.log.emit(f"{(job.title or job.url)[:60]}: {extra}")
        elif event == "status":
            self.bridge.log.emit(f"{(job.title or job.url)[:60]}: {row_status(job)}")
        if event in ("finished", "status") and job.status in (DONE, FAILED):
            self.pages["history"].reload()
            if job.status == FAILED and job.error:
                self.bridge.log.emit(f"ОШИБКА [{job.error.code}] {job.error.title}")

    def handle_action(self, kind: str, key: str) -> None:
        if kind == "update":
            self.show_tab("components")
            self.pages["components"].update_one(key)
        elif kind == "auth":
            self.show_tab("auth")

    def startup_checks(self) -> None:
        comp = self.pages["components"]
        if self.core.components.path("ytdlp") is None:
            if QMessageBox.question(self, "Первый запуск", "Движок загрузки (yt-dlp) ещё не установлен. Установить компоненты сейчас?") == QMessageBox.Yes:
                self.show_tab("components")
                comp.update_all()
            return
        if self.core.settings.get("auto_check_updates", True):
            run_bg(self, lambda: self.core.components.check_latest(), lambda _: comp.refresh(), lambda _: None)

    def closeEvent(self, event) -> None:  # noqa: N802 — имя задано Qt
        if self.core.queue.is_busy():
            answer = QMessageBox.question(self, "Выход", "Идут загрузки. Остановить их и выйти? Частично скачанные файлы сохранятся для продолжения.")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        self.core.shutdown()
        event.accept()


def main() -> int:
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyleSheet(STYLE)
    core = AppCore()
    window = MainWindow(core)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
