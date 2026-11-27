"""SQLite-хранилище истории скачиваний и очереди (переживает перезапуск)."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from . import paths

_SCHEMA = """
CREATE TABLE IF NOT EXISTS history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL, dedupe_key TEXT, title TEXT, path TEXT, size INTEGER,
    platform TEXT, status TEXT, error_code TEXT, finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_history_key ON history(dedupe_key);
CREATE TABLE IF NOT EXISTS queue (
    id TEXT PRIMARY KEY, url TEXT NOT NULL, platform TEXT, status TEXT,
    options TEXT, title TEXT, path TEXT, created REAL, attempts INTEGER DEFAULT 0
);
"""


class Store:
    def __init__(self, path: Path | None = None):
        self.path = path or (paths.data_dir() / "vds.sqlite3")
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(_SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- история
    def add_history(self, *, url: str, key: str, title: str, path: str, size: int, platform: str,
                    status: str, error_code: str = "") -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO history(url,dedupe_key,title,path,size,platform,status,error_code,finished_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (url, key, title, path, size, platform, status, error_code, time.time()),
            )
            self._db.commit()

    def list_history(self, limit: int = 1000) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM history ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def find_done(self, key: str) -> dict | None:
        """Успешно скачанная запись, файл которой ещё существует на диске."""
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM history WHERE dedupe_key=? AND status='done' ORDER BY id DESC", (key,)
            ).fetchall()
        for row in rows:
            if row["path"] and Path(row["path"]).exists():
                return dict(row)
        return None

    def delete_history(self, row_id: int) -> None:
        with self._lock:
            self._db.execute("DELETE FROM history WHERE id=?", (row_id,))
            self._db.commit()

    def clear_history(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM history")
            self._db.commit()

    # -- очередь
    def save_job(self, job: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO queue(id,url,platform,status,options,title,path,created,attempts) VALUES(?,?,?,?,?,?,?,?,?)",
                (job["id"], job["url"], job["platform"], job["status"], json.dumps(job["options"], ensure_ascii=False),
                 job.get("title", ""), job.get("path", ""), job.get("created", time.time()), job.get("attempts", 0)),
            )
            self._db.commit()

    def delete_job(self, job_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM queue WHERE id=?", (job_id,))
            self._db.commit()

    def load_jobs(self) -> list[dict]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM queue ORDER BY created").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["options"] = json.loads(d["options"] or "{}")
            except ValueError:
                d["options"] = {}
            result.append(d)
        return result
