from hud.store import HUDStore


def test_insert_list_get(tmp_path):
    store = HUDStore(str(tmp_path / "hud.db"))

    item = store.upsert_item(
        {
            "internal_id": "item-1",
            "actor": "alice",
            "intent": "ingest",
            "scope": "today",
            "status": "pending",
            "payload_json": {"hello": "world"},
        }
    )

    assert item["internal_id"] == "item-1"
    assert item["status"] == "pending"
    assert store.get_item("item-1") is not None

    listed = store.list_items(status="pending", limit=10)
    assert len(listed) == 1
    assert listed[0]["internal_id"] == "item-1"


def test_update_status_fields(tmp_path):
    store = HUDStore(str(tmp_path / "hud.db"))

    store.upsert_item(
        {
            "internal_id": "item-2",
            "actor": "alice",
            "intent": "ingest",
            "scope": "week",
            "status": "pending",
            "payload_json": {"hello": "world"},
        }
    )

    assert store.update_status(
        "item-2",
        status="approved",
        last_error="ok",
        retry_count=2,
        actor="bob",
        reviewed_by="ops",
        google_id="g-id",
    )

    updated = store.get_item("item-2")
    assert updated is not None
    assert updated["status"] == "approved"
    assert updated["actor"] == "bob"
    assert updated["reviewed_by"] == "ops"
    assert updated["google_id"] == "g-id"
    assert updated["retry_count"] == 2
    assert updated["last_error"] == "ok"

    assert store.set_sync_hash("item-2", "sync-1")
    synced = store.get_item("item-2")
    assert synced is not None
    assert synced["sync_hash"] == "sync-1"
