"""Очередь загрузок: параллельность, пауза, отмена, повтор, история, защита от дублей."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .engine import Engine, JobHandle, JobOptions, JobResult, Progress
from .errors import ErrorInfo
from .platforms import dedupe_key, detect_platform
from .store import Store

QUEUED, RUNNING, PAUSED, DONE, FAILED, CANCELED = "queued", "running", "paused", "done", "failed", "canceled"
STATUS_RU = {QUEUED: "В очереди", RUNNING: "Загрузка", PAUSED: "Пауза", DONE: "Готово", FAILED: "Ошибка", CANCELED: "Отменено"}
ACTIVE = {RUNNING}


@dataclass
class Job:
    id: str
    url: str
    platform: str
    options: JobOptions
    status: str = QUEUED
    title: str = ""
    path: str = ""
    created: float = field(default_factory=time.time)
    attempts: int = 0
    percent: float = 0.0
    speed: float = 0.0
    eta: int = -1
    downloaded: int = 0
    total: int = 0
    size: int = 0
    error: ErrorInfo | None = None
    notes: list[str] = field(default_factory=list)
    handle: JobHandle = field(default_factory=JobHandle, repr=False)


Listener = Callable[[Job, str], None]  # (job, event): added|progress|status|log|removed|finished


class JobQueue:
    def __init__(self, engine: Engine, store: Store | None = None, max_parallel: int = 2):
        self.engine = engine
        self.store = store
        self.max_parallel = max(1, int(max_parallel))
        self.jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self._paused_all = False

    # -- подписка
    def subscribe(self, cb: Listener) -> None:
        self._listeners.append(cb)

    def _emit(self, job: Job, event: str, extra: str = "") -> None:
        for cb in list(self._listeners):
            try:
                cb(job, event if not extra else f"{event}:{extra}")
            except Exception:  # noqa: BLE001 — слушатель не должен ломать очередь
                pass

    # -- восстановление
    def restore(self) -> int:
        if not self.store:
            return 0
        n = 0
        for row in self.store.load_jobs():
            if row["status"] in (DONE, CANCELED):
                self.store.delete_job(row["id"])
                continue
            opts = JobOptions.from_dict(row["options"]) if row["options"] else None
            if not opts:
                continue
            status = PAUSED if row["status"] in (RUNNING, PAUSED) else row["status"]
            job = Job(row["id"], row["url"], row["platform"] or detect_platform(row["url"]), opts, status, row.get("title", ""),
                      row.get("path", ""), row.get("created", time.time()), row.get("attempts", 0))
            self.jobs[job.id] = job
            self._order.append(job.id)
            n += 1
        return n

    # -- операции
    def find_duplicate(self, url: str) -> dict | None:
        """Уже скачанное видео (по истории и существующему файлу) либо уже стоящее в очереди."""
        key = dedupe_key(url)
        with self._lock:
            for job in self.jobs.values():
                if dedupe_key(job.url) == key and job.status in (QUEUED, RUNNING, PAUSED):
                    return {"title": job.title or job.url, "path": "", "where": "queue"}
        if self.store:
            found = self.store.find_done(key)
            if found:
                found["where"] = "history"
                return found
        return None

    def add(self, url: str, options: JobOptions, start: bool = True) -> Job:
        options.url = url
        job = Job(uuid.uuid4().hex[:10], url, detect_platform(url), options)
        job.options.platform = job.platform
        with self._lock:
            self.jobs[job.id] = job
            self._order.append(job.id)
        self._persist(job)
        self._emit(job, "added")
        if start:
            self._pump()
        return job

    def _persist(self, job: Job) -> None:
        if self.store:
            self.store.save_job({"id": job.id, "url": job.url, "platform": job.platform, "status": job.status,
                                 "options": job.options.to_dict(), "title": job.title, "path": job.path,
                                 "created": job.created, "attempts": job.attempts})

    def _pump(self) -> None:
        with self._lock:
            if self._paused_all:
                return
            running = sum(1 for j in self.jobs.values() if j.status == RUNNING)
            for jid in self._order:
                if running >= self.max_parallel:
                    break
                job = self.jobs.get(jid)
                if job and job.status == QUEUED:
                    job.status = RUNNING
                    job.handle = JobHandle()
                    job.attempts += 1
                    job.error = None
                    running += 1
                    threading.Thread(target=self._run, args=(job,), daemon=True, name=f"job-{job.id}").start()
                    self._emit(job, "status")

    def _run(self, job: Job) -> None:
        def on_progress(p: Progress) -> None:
            job.percent, job.speed, job.eta = p.percent, p.speed, p.eta
            job.downloaded, job.total = p.downloaded, p.total
            self._emit(job, "progress")

        def on_log(line: str) -> None:
            self._emit(job, "log", line)

        try:
            result: JobResult = self.engine.run(job.options, job.handle, on_progress, on_log)
        except Exception as exc:  # noqa: BLE001
            from .errors import classify_exception
            result = JobResult(state="failed", error=classify_exception(exc))
        with self._lock:
            job.notes = result.notes
            if result.state == "done":
                job.status, job.percent = DONE, 100.0
                job.path, job.title, job.size = result.filepath, result.title, result.size
            elif result.state == "paused":
                job.status = PAUSED
            elif result.state == "canceled":
                job.status = CANCELED
            else:
                job.status, job.error = FAILED, result.error
        if job.status == DONE and self.store:
            self.store.add_history(url=job.url, key=dedupe_key(job.url), title=job.title, path=job.path, size=job.size,
                                   platform=job.platform, status="done")
        elif job.status == FAILED and self.store and job.error:
            self.store.add_history(url=job.url, key=dedupe_key(job.url), title=job.title or job.url, path="", size=0,
                                   platform=job.platform, status="failed", error_code=job.error.code)
        self._persist(job)
        self._emit(job, "status")
        self._emit(job, "finished")
        self._pump()

    def cancel(self, job_id: str, delete_partial: bool = False) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        if job.status == RUNNING:
            job.handle.stop(pause=False)
        elif job.status in (QUEUED, PAUSED, FAILED):
            job.status = CANCELED
            self._persist(job)
            self._emit(job, "status")

    def pause(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        if job.status == RUNNING:
            job.handle.stop(pause=True)
        elif job.status == QUEUED:
            job.status = PAUSED
            self._persist(job)
            self._emit(job, "status")

    def resume(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if job and job.status == PAUSED:
            job.status = QUEUED
            self._persist(job)
            self._emit(job, "status")
            self._pump()

    def retry(self, job_id: str) -> None:
        """Ручной повтор неудачной/отменённой загрузки (автоматических бесконечных повторов нет)."""
        job = self.jobs.get(job_id)
        if job and job.status in (FAILED, CANCELED):
            job.status, job.error, job.percent = QUEUED, None, 0.0
            self._persist(job)
            self._emit(job, "status")
            self._pump()

    def remove(self, job_id: str) -> None:
        job = self.jobs.get(job_id)
        if not job:
            return
        if job.status == RUNNING:
            job.handle.stop(pause=False)
        with self._lock:
            self.jobs.pop(job_id, None)
            if job_id in self._order:
                self._order.remove(job_id)
        if self.store:
            self.store.delete_job(job_id)
        self._emit(job, "removed")

    def clear_finished(self) -> None:
        for job in [j for j in self.jobs.values() if j.status in (DONE, CANCELED)]:
            self.remove(job.id)

    def pause_all(self) -> None:
        with self._lock:
            self._paused_all = True
        for job in list(self.jobs.values()):
            self.pause(job.id)

    def resume_all(self) -> None:
        with self._lock:
            self._paused_all = False
        for job in list(self.jobs.values()):
            if job.status == PAUSED:
                job.status = QUEUED
                self._persist(job)
                self._emit(job, "status")
        self._pump()

    def set_parallel(self, n: int) -> None:
        self.max_parallel = max(1, min(8, int(n)))
        self._pump()

    def stop_all_for_exit(self) -> None:
        for job in list(self.jobs.values()):
            if job.status == RUNNING:
                job.handle.stop(pause=True)

    # -- сводка
    def active_count(self) -> int:
        return sum(1 for j in self.jobs.values() if j.status == RUNNING)

    def is_busy(self) -> bool:
        """Используется менеджером компонентов: заменять файлы во время загрузки нельзя."""
        return self.active_count() > 0

    def overall(self) -> tuple[float, int, int]:
        with self._lock:
            jobs = [j for j in self.jobs.values() if j.status != CANCELED]
        if not jobs:
            return 0.0, 0, 0
        total = sum(100.0 if j.status == DONE else j.percent for j in jobs) / len(jobs)
        return total, sum(1 for j in jobs if j.status == DONE), len(jobs)
