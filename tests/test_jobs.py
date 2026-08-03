import pytest

from xscraper.jobs import JobService
from xscraper.models import CollectionRequest, CollectionSummary, JobStatus, Tweet
from xscraper.storage import Storage


def make_tweet():
    return Tweet(
        tweet_id="1",
        text="same post",
        author_username="tester",
        url="https://x.com/tester/status/1",
        created_at="2026-06-02T14:29:55+00:00",
    )


class DuplicateResumeProvider:
    def session_status(self):
        return {"status": "valid", "valid": True, "message": "ready"}

    def collect(self, request, *, cursor, cursor_context, on_batch, should_cancel):
        accepted = on_batch(
            [make_tweet()],
            "next",
            {
                "provider": "fake",
                "version": 1,
                "operation": "fake",
                "requestFingerprint": "fake",
                "sort": "live",
            },
            1,
        )
        assert accepted == 0
        return CollectionSummary(completion_reason="target_reached")


def test_resume_does_not_report_target_reached_for_duplicate_rows(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "search", "sourceValue": "python", "maxTweets": 2}
    )
    job_id = storage.create_job(request)
    storage.add_tweets(job_id, [make_tweet()], "saved")
    storage.fail_job(job_id, JobStatus.FAILED, "temporary", "retry", True)
    assert storage.resume_job(job_id)

    service = JobService(storage, DuplicateResumeProvider(), start_worker=False)
    service.run_once(job_id)

    job = storage.get_job(job_id)
    assert job["status"] == JobStatus.PARTIAL.value
    assert job["completion_reason"] == "target_not_reached"
    assert job["collected_count"] == 1
    assert job["target_count"] == 2


def test_only_one_worker_process_lock_can_use_a_database(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    first = JobService(storage, DuplicateResumeProvider())
    try:
        with pytest.raises(RuntimeError, match="one server process"):
            JobService(storage, DuplicateResumeProvider())
    finally:
        first.shutdown()
    assert not (tmp_path / "test.db.worker.lock").exists()
