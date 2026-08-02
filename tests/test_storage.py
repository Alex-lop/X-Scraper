from xscraper.models import CollectionRequest, JobStatus, Tweet
from xscraper.storage import Storage


def make_tweet(tweet_id="1"):
    return Tweet(
        tweet_id=tweet_id,
        text="hello",
        author_username="tester",
        url=f"https://x.com/tester/status/{tweet_id}",
        created_at="2026-06-02T14:29:55+00:00",
        like_count=7,
    )


def test_job_storage_deduplicates_and_checkpoints(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxTweets": 25}
    )
    job_id = storage.create_job(request)
    storage.set_running(job_id)
    assert storage.add_tweets(job_id, [make_tweet(), make_tweet()], "cursor-1") == 1
    job = storage.get_job(job_id)
    assert job["collected_count"] == 1
    assert job["cursor"] == "cursor-1"
    assert storage.get_job_tweets(job_id)[0]["like_count"] == 7


def test_recovery_and_resume(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "search", "sourceValue": "python", "maxTweets": 5}
    )
    job_id = storage.create_job(request)
    storage.set_running(job_id)
    assert storage.recover_jobs() == [job_id]
    assert storage.get_job(job_id)["status"] == JobStatus.QUEUED.value
    storage.fail_job(job_id, JobStatus.FAILED, "rate_limited", "later", True)
    assert storage.resume_job(job_id)
    assert storage.get_job(job_id)["status"] == JobStatus.QUEUED.value
