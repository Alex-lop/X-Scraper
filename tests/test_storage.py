import json

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


def test_job_observations_and_enrichments_are_immutable(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    plain_request = CollectionRequest.from_dict(
        {"sourceType": "search", "sourceValue": "python", "maxTweets": 5}
    )
    sentiment_request = CollectionRequest.from_dict(
        {
            "sourceType": "search",
            "sourceValue": "python",
            "maxTweets": 5,
            "analyzeSentiment": True,
        }
    )
    first_job = storage.create_job(plain_request)
    second_job = storage.create_job(sentiment_request)
    storage.add_tweets(first_job, [make_tweet()], "first")
    newer = make_tweet()
    newer.like_count = 99
    storage.add_tweets(
        second_job,
        [newer],
        "second",
        enrichments={"1": ("positive", 0.8, "vader", "3.3.2")},
    )

    first = storage.get_job_tweets(first_job)[0]
    second = storage.get_job_tweets(second_job)[0]
    assert first["like_count"] == 7
    assert first["sentiment_label"] is None
    assert second["like_count"] == 99
    assert second["sentiment_label"] == "positive"


def test_claim_and_cancel_transitions_are_atomic(tmp_path):
    storage = Storage(tmp_path / "test.db")
    storage.initialize()
    request = CollectionRequest.from_dict(
        {"sourceType": "profile", "sourceValue": "tester", "maxTweets": 5}
    )
    queued = storage.create_job(request)
    assert storage.request_cancel(queued)
    assert storage.claim_job(queued, "worker") is None
    assert storage.get_job(queued)["status"] == JobStatus.CANCELLED.value

    running = storage.create_job(request)
    assert storage.claim_job(running, "worker") is not None
    assert storage.claim_job(running, "other-worker") is None
    assert storage.request_cancel(running)
    final_status = storage.finish_job(
        running, [], completion_reason="target_reached"
    )
    assert final_status == JobStatus.CANCELLED.value
    assert storage.get_job(running)["status"] == JobStatus.CANCELLED.value

    restarting = storage.create_job(request)
    assert storage.claim_job(restarting, "worker") is not None
    assert storage.request_cancel(restarting)
    recovered = storage.recover_jobs()
    assert restarting not in recovered
    assert storage.get_job(restarting)["status"] == JobStatus.CANCELLED.value


def test_migrates_an_empty_version_one_database(tmp_path):
    storage = Storage(tmp_path / "test.db")
    with storage.connect() as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        storage._migration_1(connection)
        storage._set_schema_version(connection, 1)

    storage.initialize()
    with storage.connect() as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()[0]
        observation_table = connection.execute(
            "SELECT name FROM sqlite_master WHERE name = 'tweet_observations'"
        ).fetchone()
    assert version == "2"
    assert observation_table is not None
    assert (tmp_path / "test.db.pre-v1-to-v2.bak").exists()


def test_migrates_version_one_rows_into_job_snapshots(tmp_path):
    storage = Storage(tmp_path / "test.db")
    request = CollectionRequest.from_dict(
        {
            "sourceType": "profile",
            "sourceValue": "tester",
            "maxTweets": 1,
            "analyzeSentiment": True,
        }
    )
    with storage.connect() as connection:
        connection.execute(
            "CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        storage._migration_1(connection)
        storage._set_schema_version(connection, 1)
        connection.execute(
            """
            INSERT INTO jobs (
                id, request_json, source_type, source_value, status, target_count,
                collected_count, cursor, created_at, updated_at
            ) VALUES ('legacy', ?, 'profile', 'tester', 'succeeded', 1, 1,
                      'legacy-cursor', ?, ?)
            """,
            (
                json.dumps(request.to_dict()),
                "2026-06-02T00:00:00+00:00",
                "2026-06-02T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO tweets (
                tweet_id, text, author_username, url, scraped_at, like_count
            ) VALUES ('1', 'legacy post', 'tester', 'https://x.com/tester/status/1', ?, 7)
            """,
            ("2026-06-02T00:00:00+00:00",),
        )
        connection.execute("INSERT INTO job_tweets VALUES ('legacy', '1', 0)")
        connection.execute(
            """
            INSERT INTO tweet_enrichments VALUES (
                '1', 'positive', 0.8, 'vader', '3.3.2', '2026-06-02T00:00:00+00:00'
            )
            """
        )

    storage.initialize()
    row = storage.get_job_tweets("legacy")[0]
    assert row["text"] == "legacy post"
    assert row["like_count"] == 7
    assert row["sentiment_label"] == "positive"
    migrated_job = storage.get_job("legacy")
    assert migrated_job["request_fingerprint"] == request.fingerprint()
    assert migrated_job["cursor_context"]["operation"] == "UserTweets"
