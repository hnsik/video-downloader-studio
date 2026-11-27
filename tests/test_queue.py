import threading
import time

from vds.engine import JobHandle, JobOptions, JobResult, Progress
from vds.errors import classify
from vds.jobqueue import CANCELED, DONE, FAILED, PAUSED, QUEUED, RUNNING, JobQueue
from vds.store import Store


class FakeEngine:
    """Имитация движка: управляемая длительность и результат."""

    def __init__(self):
        self.calls, self.fail_urls, self.gate = [], set(), threading.Event()
        self.hold = False

    def run(self, opts, handle, on_progress=None, on_log=None):
        self.calls.append(opts.url)
        if on_progress:
            on_progress(Progress("downloading", 50.0, 5, 10, 100.0, 1))
        while self.hold and not handle.stopped:
            time.sleep(0.02)
        if handle.cancel_requested:
            return JobResult(state="canceled")
        if handle.pause_requested:
            return JobResult(state="paused")
        if opts.url in self.fail_urls:
            return JobResult(state="failed", error=classify("HTTP Error 403: Forbidden"))
        return JobResult(ok=True, state="done", filepath=f"/tmp/{opts.url[-3:]}.mp4", title="T" + opts.url[-3:], size=10)


def wait(cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def opts(url="https://example.org/aaa"):
    return JobOptions(url=url, out_dir="/tmp")


def test_batch_runs_with_parallel_limit(tmp_path):
    eng = FakeEngine()
    eng.hold = True
    q = JobQueue(eng, Store(tmp_path / "s.db"), max_parallel=2)
    jobs = [q.add(f"https://example.org/v{i:02d}", opts()) for i in range(5)]
    assert wait(lambda: q.active_count() == 2)
    time.sleep(0.2)
    assert q.active_count() == 2 and sum(j.status == QUEUED for j in jobs) == 3
    eng.hold = False
    assert wait(lambda: all(j.status == DONE for j in jobs))
    pct, done, total = q.overall()
    assert (pct, done, total) == (100.0, 5, 5)


def test_failure_then_manual_retry(tmp_path):
    eng = FakeEngine()
    eng.fail_urls.add("https://example.org/bad")
    q = JobQueue(eng, Store(tmp_path / "s.db"))
    job = q.add("https://example.org/bad", opts())
    assert wait(lambda: job.status == FAILED)
    assert job.error.code == "HTTP_403" and len(eng.calls) == 1   # без автоповторов
    eng.fail_urls.clear()
    q.retry(job.id)
    assert wait(lambda: job.status == DONE) and len(eng.calls) == 2 and job.attempts == 2


def test_cancel_running_and_queued(tmp_path):
    eng = FakeEngine()
    eng.hold = True
    q = JobQueue(eng, Store(tmp_path / "s.db"), max_parallel=1)
    a, b = q.add("https://example.org/aaa", opts()), q.add("https://example.org/bbb", opts())
    assert wait(lambda: a.status == RUNNING)
    q.cancel(b.id)
    assert b.status == CANCELED
    q.cancel(a.id)
    assert wait(lambda: a.status == CANCELED)
    assert q.active_count() == 0


def test_pause_resume(tmp_path):
    eng = FakeEngine()
    eng.hold = True
    q = JobQueue(eng, Store(tmp_path / "s.db"), max_parallel=1)
    a = q.add("https://example.org/aaa", opts())
    assert wait(lambda: a.status == RUNNING)
    q.pause(a.id)
    assert wait(lambda: a.status == PAUSED)
    eng.hold = False
    q.resume(a.id)
    assert wait(lambda: a.status == DONE)


def test_history_and_duplicate_prevention(tmp_path):
    store = Store(tmp_path / "s.db")
    real = tmp_path / "video.mp4"
    real.write_bytes(b"x")
    eng = FakeEngine()
    q = JobQueue(eng, store)
    url = "https://www.youtube.com/watch?v=jNQXAC9IP4c"
    assert q.find_duplicate(url) is None
    store.add_history(url=url, key="youtube:jNQXAC9IP4c", title="Zoo", path=str(real), size=1, platform="youtube", status="done")
    dup = q.find_duplicate("https://youtu.be/jNQXAC9IP4c")
    assert dup and dup["where"] == "history"
    real.unlink()
    assert q.find_duplicate(url) is None      # файла нет — повторная загрузка допустима


def test_queue_persists_and_restores_as_paused(tmp_path):
    store = Store(tmp_path / "s.db")
    eng = FakeEngine()
    eng.hold = True
    q = JobQueue(eng, store, max_parallel=1)
    a, b = q.add("https://example.org/aaa", opts()), q.add("https://example.org/bbb", opts())
    assert wait(lambda: a.status == RUNNING)
    q.stop_all_for_exit()
    assert wait(lambda: a.status == PAUSED)
    q2 = JobQueue(FakeEngine(), Store(tmp_path / "s.db"))
    assert q2.restore() == 2
    states = {j.url: j.status for j in q2.jobs.values()}
    assert states["https://example.org/aaa"] == PAUSED and states["https://example.org/bbb"] == QUEUED


def test_busy_flag_for_component_updates(tmp_path):
    eng = FakeEngine()
    eng.hold = True
    q = JobQueue(eng, None)
    assert not q.is_busy()
    j = q.add("https://example.org/aaa", opts())
    assert wait(q.is_busy)
    q.cancel(j.id)
    assert wait(lambda: not q.is_busy())


def test_clear_finished_and_remove(tmp_path):
    q = JobQueue(FakeEngine(), Store(tmp_path / "s.db"))
    a = q.add("https://example.org/aaa", opts())
    assert wait(lambda: a.status == DONE)
    q.clear_finished()
    assert not q.jobs
