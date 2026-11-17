from rq.job import Job

from app.workers import scheduler
from app.workers.queues import QUEUE_DEFAULT, enqueue_unique, get_queue


def _noop():
    return "ok"


def test_enqueue_unique_does_not_duplicate(redis):
    job1, created1 = enqueue_unique(
        QUEUE_DEFAULT, "tests.test_workers._noop", job_id="unique-1", connection=redis
    )
    job2, created2 = enqueue_unique(
        QUEUE_DEFAULT, "tests.test_workers._noop", job_id="unique-1", connection=redis
    )
    assert created1 and not created2
    assert job1.id == job2.id
    assert get_queue(QUEUE_DEFAULT, redis).count == 1


def test_enqueue_unique_replaces_finished_job(redis):
    job, _ = enqueue_unique(QUEUE_DEFAULT, "tests.test_workers._noop", job_id="unique-2", connection=redis)
    get_queue(QUEUE_DEFAULT, redis).remove(job)
    job.set_status("finished")
    _job, created = enqueue_unique(
        QUEUE_DEFAULT, "tests.test_workers._noop", job_id="unique-2", connection=redis
    )
    assert created
    assert Job.fetch("unique-2", connection=redis).get_status() == "queued"


def test_scheduler_tick_enqueues_once_per_interval(redis):
    jobs = (scheduler.PeriodicJob("demo", "tests.test_workers._noop", 60),)
    assert scheduler.tick(redis, jobs, now=1_000_000) == ["demo"]
    assert scheduler.tick(redis, jobs, now=1_000_010) == []  # Intervall läuft noch (Redis-Lock)
    assert redis.get(scheduler.HEARTBEAT_KEY) == b"1000010"
    assert get_queue(QUEUE_DEFAULT, redis).count == 1


def test_scheduler_registers_maintenance_jobs():
    names = {job.name for job in scheduler.PERIODIC_JOBS}
    assert {"storage_cleanup", "prune_activity"} <= names
    for job in scheduler.PERIODIC_JOBS:
        module, func = job.func.rsplit(".", 1)
        assert callable(getattr(__import__(module, fromlist=[func]), func))


def test_job_runs_in_worker(redis, database, settings):
    """Führt einen echten Wartungsjob über einen RQ-Worker im Burst-Modus aus."""
    from rq import SimpleWorker

    enqueue_unique(
        QUEUE_DEFAULT,
        "app.workers.jobs.maintenance.cleanup_storage_job",
        job_id="t-cleanup",
        connection=redis,
    )
    worker = SimpleWorker([get_queue(QUEUE_DEFAULT, redis)], connection=redis)
    worker.work(burst=True)
    job = Job.fetch("t-cleanup", connection=redis)
    assert job.get_status() == "finished", job.latest_result()
    assert job.return_value()["deleted_files"] == 0


def test_job_id_validation(redis):
    import pytest

    from app.workers.queues import make_job_id

    assert make_job_id("vod", 123, "a:b/c") == "vod-123-a_b_c"
    with pytest.raises(ValueError):
        enqueue_unique(QUEUE_DEFAULT, "tests.test_workers._noop", job_id="bad:id", connection=redis)
