"""Deterministic HUD sync reconciliation helpers for v1 status payloads."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, MutableMapping


_STALE_STATUSES = {"pending", "queued"}
_KNOWN_SUMMARY_STATUSES = {
    "pending",
    "pending_approval",
    "approved",
    "rejected",
    "queued",
    "failed",
    "ready",
    "synced",
    "duplicate",
}


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_status(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def _item_sort_key(item: Mapping[str, Any]) -> tuple:
    queue_rank = item.get("queue_rank")
    try:
        queue_rank_value = int(queue_rank) if queue_rank is not None else 0
    except (TypeError, ValueError):
        queue_rank_value = 0

    return (
        queue_rank_value,
        str(item.get("created_at") or ""),
        str(item.get("internal_id") or ""),
    )


def _is_stale(item: Mapping[str, Any], normalized_status: str) -> bool:
    sync_hash = item.get("sync_hash")
    if sync_hash is None:
        return False

    sync_hash_text = str(sync_hash).strip()
    if not sync_hash_text:
        return False

    return (
        _coerce_int(item.get("retry_count")) > 2
        and normalized_status in _STALE_STATUSES
    )


def build_sync_status_payload(items: Iterable[Mapping[str, Any]]) -> Dict[str, Dict[str, int]]:
    """Build a deterministic summary of HUD sync status counts."""
    summary = {
        "stale_count": 0,
        "failed_count": 0,
        "total": 0,
        "pending": 0,
        "pending_approval": 0,
        "approved": 0,
        "failed": 0,
        "rejected": 0,
        "queued": 0,
        "ready": 0,
        "duplicate": 0,
        "synced": 0,
    }

    for item in items:
        if not isinstance(item, Mapping):
            continue

        normalized_status = _coerce_status(item.get("status"))
        summary["total"] += 1

        if normalized_status in _KNOWN_SUMMARY_STATUSES:
            summary[normalized_status] += 1

        if _is_stale(item, normalized_status):
            summary["stale_count"] += 1

        if normalized_status == "failed":
            summary["failed_count"] += 1

    return {"summary": summary}


def build_brief_sync_report(items: Iterable[Mapping[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Return sorted failed and stale item lists used by HUD status responses."""
    failed_items: List[Dict[str, Any]] = []
    pending_approval_items: List[Dict[str, Any]] = []
    stale_items: List[Dict[str, Any]] = []

    for item in items:
        if not isinstance(item, Mapping):
            continue

        normalized = dict(item)
        normalized_status = _coerce_status(item.get("status"))

        if normalized_status == "failed":
            failed_items.append(normalized)
        if normalized_status == "pending_approval":
            pending_approval_items.append(normalized)

        if _is_stale(item, normalized_status):
            normalized["stale"] = True
            stale_items.append(normalized)

    failed_items.sort(key=_item_sort_key)
    pending_approval_items.sort(key=_item_sort_key)
    stale_items.sort(key=_item_sort_key)

    return {
        "failed": failed_items,
        "pending_approval": pending_approval_items,
        "stale": stale_items,
    }


def mark_failed(item: MutableMapping[str, Any], reason: Any) -> MutableMapping[str, Any]:
    """
    Mark an in-memory HUD item payload as failed without writing to storage.
    """
    item["status"] = "failed"
    item["last_error"] = reason
    return item


__all__ = [
    "build_brief_sync_report",
    "build_sync_status_payload",
    "mark_failed",
]
