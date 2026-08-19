import csv
import io

from xworkbench.api import create_app
from xworkbench.cli import _local_mcp_read, _seed_offline_demo
from xworkbench.config import Settings
from xworkbench.providers import ProviderRegistry
from xworkbench.read_service import ReadService
from xworkbench.storage import Storage


def test_offline_demo_proves_two_snapshot_product_loop(tmp_path):
    settings = Settings(tmp_path / "demo.db", tmp_path / "no-token")
    storage = Storage(settings.database_path)
    seeded = _seed_offline_demo(storage)
    reader = ReadService(storage)

    sources = reader.list_sources()["sources"]
    assert [(source["sourceId"], source["displayName"]) for source in sources] == [
        ("demo-project-glasswing", "DEMO — Project Glasswing (fictional topic)")
    ]
    snapshots = reader.list_snapshots(source_id=seeded["sourceId"])["snapshots"]
    assert len(snapshots) == 2
    assert [snapshot["sample"]["observedPosts"] for snapshot in snapshots] == [25, 25]
    assert snapshots[0]["sample"]["partial"] is True
    assert snapshots[0]["sample"]["truncated"] is True
    assert snapshots[1]["sample"]["partial"] is False

    comparison = reader.compare_snapshots(
        seeded["olderSnapshotId"], seeded["newerSnapshotId"], limit=25
    )
    assert comparison["counts"] == {
        "newlyObserved": 10,
        "reobserved": 15,
        "notObservedInNewerSample": 10,
    }
    assert comparison["reobserved"][0]["engagementDelta"]["like"] == 7
    assert comparison["partial"] is True
    assert "not a deletion claim" in comparison["absenceCaveat"]
    assert all(
        item["originalUrl"] is None and item["citationAvailable"] is False
        for category in ("newlyObserved", "notObservedInNewerSample")
        for item in comparison[category]
    )

    searched = reader.search_post_evidence(
        "moonflower", snapshot_ids=[seeded["newerSnapshotId"]], limit=25
    )
    assert len(searched["evidence"]) == 5
    assert all(item["evidenceId"] for item in searched["evidence"])
    assert all(item["untrustedExternalContent"] is True for item in searched["evidence"])

    app = create_app(
        settings,
        storage=storage,
        registry=ProviderRegistry([]),
        start_worker=False,
        collection_enabled=False,
    )
    try:
        client = app.test_client()
        snapshot_id = seeded["newerSnapshotId"]
        json_export = client.get(
            f"/api/jobs/{snapshot_id}/export?format=json"
        ).get_json()
        csv_export = client.get(
            f"/api/jobs/{snapshot_id}/export?format=csv"
        ).get_data(as_text=True)
        assert len(json_export["posts"]) == 25
        assert len(list(csv.DictReader(io.StringIO(csv_export)))) == 25
        assert all(post["url"].startswith("offline://") for post in json_export["posts"])
    finally:
        app.extensions["xworkbench_jobs"].shutdown()

    tools, mcp_comparison = _local_mcp_read(
        storage,
        "compare_snapshots",
        {
            "older_snapshot_id": seeded["olderSnapshotId"],
            "newer_snapshot_id": seeded["newerSnapshotId"],
            "limit": 25,
        },
    )
    assert set(tools) == {
        "list_x_snapshots",
        "get_x_snapshot",
        "get_x_posts",
        "search_x_snapshot",
        "get_latest_feed_snapshot",
        "list_sources",
        "list_snapshots",
        "get_latest_usable_snapshot",
        "search_post_evidence",
        "compare_snapshots",
        "get_topic_activity",
        "get_collection_health",
    }
    assert mcp_comparison["counts"] == comparison["counts"]
