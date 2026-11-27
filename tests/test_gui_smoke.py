"""Смоук-тест интерфейса.

Если PySide6 установлен — окно создаётся по-настоящему (QT_QPA_PLATFORM=offscreen).
Если нет — используются заглушки Qt: тест проверяет только согласованность кода интерфейса с бэкендом
(имена методов, атрибутов, импорты), но НЕ корректность вызовов Qt. Реальный запуск окна — в Windows-сборке/вручную.
"""
import importlib
import os
import sys
import types

import pytest


class _Meta(type):
    def __getattr__(cls, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _D()


class _D(metaclass=_Meta):
    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _D()

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return _D()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return True

    def text(self):
        return ""

    def currentData(self):
        return "youtube"

    def currentText(self):
        return ""

    def toPlainText(self):
        return ""

    def isChecked(self):
        return False

    def value(self):
        return 1

    def count(self):
        return 0

    def findData(self, *a):
        return -1

    def __ge__(self, other):
        return False

    def __gt__(self, other):
        return False


def _install_stubs():
    for name in ("PySide6", "PySide6.QtCore", "PySide6.QtGui", "PySide6.QtWidgets"):
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda attr, _n=name: type(attr, (_D,), {})
        sys.modules[name] = mod


def _real_pyside() -> bool:
    try:
        import PySide6  # noqa: F401
        return not isinstance(PySide6, types.ModuleType) or hasattr(PySide6, "__version__")
    except ImportError:
        return False


@pytest.fixture
def gui_module():
    real = _real_pyside()
    saved = {k: v for k, v in sys.modules.items() if k.startswith("PySide6") or k == "vds.gui"}
    if real:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    else:
        for k in list(saved):
            sys.modules.pop(k, None)
        _install_stubs()
    sys.modules.pop("vds.gui", None)
    module = importlib.import_module("vds.gui")
    yield module, real
    sys.modules.pop("vds.gui", None)
    for k in [k for k in sys.modules if k.startswith("PySide6")]:
        sys.modules.pop(k, None)
    sys.modules.update(saved)


def test_gui_builds_all_pages(gui_module):
    module, real = gui_module
    from vds.core import AppCore

    if real:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication([])  # noqa: F841
    core = AppCore()
    try:
        win = module.MainWindow(core)
        assert set(win.pages) == {"download", "queue", "history", "auth", "components", "diagnostics", "settings", "log"}
        # события очереди: «имя» и «имя:подробности»
        job = core.queue.add("https://www.youtube.com/watch?v=abcdefghijk", core.options_for("https://www.youtube.com/watch?v=abcdefghijk"), start=False)
        win._queue_callback(job, "log:строка журнала")
        win.pages["queue"].rebuild()
        win.pages["history"].reload()
        win.pages["components"].refresh()
    finally:
        core.shutdown()
