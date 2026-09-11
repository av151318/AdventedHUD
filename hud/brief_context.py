"""Default hud.brief classification context: mission, matrix, onboarding state."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from hud.onboarding import hud_soul_md_extract_roles_and_goals
from hud.onboarding_db import is_user_fully_onboarded, is_user_onboarding_atomic_complete
from hud.store import HUDStore

DECISION_MATRIX: Dict[str, Any] = {
    "name": "FranklinCovey Time Management Matrix",
    "axes": {
        "importance": "Contributes to mission, roles, goals, and values",
        "urgency": "Demands immediate attention (deadline, crisis, real consequences)",
    },
    "quadrants": [
        {
            "id": "Q1",
            "name": "Crisis / Important & Urgent",
            "urgency": "high",
            "importance": "high",
            "guidance": "Do immediately (or as soon as possible). Highest priority.",
            "priority_class_hint": "critical",
        },
        {
            "id": "Q2",
            "name": "Quality / Important but Not Urgent",
            "urgency": "low",
            "importance": "high",
            "guidance": "Schedule and protect dedicated time. Primary planning focus.",
            "priority_class_hint": "high",
        },
        {
            "id": "Q3",
            "name": "Deception / Not Important but Urgent",
            "urgency": "high",
            "importance": "low",
            "guidance": "Delegate, minimize, or batch. Question whether it truly belongs to the user.",
            "priority_class_hint": "medium",
        },
        {
            "id": "Q4",
            "name": "Waste / Not Important & Not Urgent",
            "urgency": "low",
            "importance": "low",
            "guidance": "Eliminate or strictly limit.",
            "priority_class_hint": "low",
        },
    ],
    "classification_flow": [
        "Determine importance against mission, roles, and goals.",
        "Determine urgency (deadline, crisis, external pressure).",
        "Default bias: favor Q2; reduce Q1 over time through better planning.",
    ],
    "semantic_type_hints": {
        "event": "Time-bound occurrence; often calendar when approved.",
        "todo": "Actionable item; often tasks when approved.",
        "note": "Unstructured capture; Obsidian only.",
    },
}


def build_decision_matrix_guidance_alias() -> Dict[str, Any]:
    return {
        "apply": "Use decision_matrix.quadrants and classification_flow verbatim.",
        "priority_class_values": ["critical", "high", "medium", "low", "normal"],
        "decision_matrix": DECISION_MATRIX,
    }


def extract_mission(content: str) -> Dict[str, Any]:
    text = str(content or "")
    mission_patterns = [
        re.compile(
            r"(?ms)^\s*##\s*My\s+Mission\s+Statement\s*\n+(.*?)(?=^\s*##\s|\Z)"
        ),
        re.compile(r"(?ms)^\s*##\s*Mission\s+Statement\s*\n+(.*?)(?=^\s*##\s|\Z)"),
        re.compile(r"(?ms)^\s*##\s*Mission\s*\n+(.*?)(?=^\s*##\s|\Z)"),
    ]
    for pattern in mission_patterns:
        match = pattern.search(text)
        if not match:
            continue
        body = _first_non_empty_paragraph(match.group(1))
        if body:
            return {"text": body, "source": "mission_statement"}

    vision_match = re.search(
        r"(?ms)^\s*##\s*Vision\s*\n+(.*?)(?=^\s*##\s|\Z)", text
    )
    if vision_match:
        body = _first_non_empty_paragraph(vision_match.group(1))
        if body:
            return {"text": body, "source": "vision_fallback"}

    return {"text": "", "source": None}


def _first_non_empty_paragraph(section: str) -> str:
    blocks = re.split(r"\n\s*\n", section.strip())
    for block in blocks:
        lines = [
            line.strip()
            for line in block.splitlines()
            if line.strip() and not line.strip().startswith("|")
        ]
        if not lines:
            continue
        paragraph = " ".join(lines).strip()
        if paragraph and not paragraph.startswith("#"):
            return paragraph
    return ""


def resolve_onboarding_state(store: HUDStore, user_id: str) -> str:
    try:
        if is_user_fully_onboarded(store, user_id):
            return "fully_onboarded"
        if is_user_onboarding_atomic_complete(store, user_id):
            return "awaiting_push_policy"
    except Exception:
        return "incomplete"
    return "incomplete"


def build_push_policy_status(store: HUDStore, user_id: str) -> Dict[str, Any]:
    try:
        policy_value = store.get_user_push_policy(user_id)
    except Exception:
        policy_value = None
    return {
        "set": policy_value is not None,
        "external_push_without_approval": bool(policy_value)
        if policy_value is not None
        else None,
    }


def _build_current_projection(store: HUDStore, user_id: str, days_ahead: int = 30) -> List[Dict[str, Any]]:
    """
    What the agent sees as "already committed" in the near term.
    This powers intelligent conflict detection and seamless projection when
    push-without-approval is enabled.
    Only returns items that have moved past classification into projection states.
    """
    try:
        items = store.list_items(limit=300)
    except Exception:
        return []

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)

    upcoming = []
    for item in items:
        payload = item.get("payload_json") if isinstance(item.get("payload_json"), Mapping) else {}
        due_str = (
            item.get("due")
            or item.get("scheduled_for")
            or item.get("date")
            or payload.get("due")
            or payload.get("scheduled_for")
            or payload.get("date")
        )
        if not due_str:
            continue
        try:
            due = datetime.fromisoformat(str(due_str).replace("Z", "+00:00"))
            if due > cutoff:
                continue

            status = item.get("status", "")
            # These are the states where something is actually going to external systems
            if status not in ("approved", "queued", "pending", "synced"):
                continue

            upcoming.append({
                "id": item.get("id") or item.get("internal_id"),
                "role": item.get("role_ref") or item.get("role") or payload.get("role_ref") or payload.get("role"),
                "title": item.get("title") or item.get("summary") or payload.get("title") or payload.get("summary"),
                "type": item.get("semantic_type") or payload.get("semantic_type") or "todo",
                "due": due_str,
                "status": status,
                "priority": item.get("priority_class"),
            })
        except Exception:
            continue

    upcoming.sort(key=lambda x: x.get("due", ""))
    return upcoming[:50]


def _build_google_context(hub: Optional["HUDAdapterHub"] = None) -> Dict[str, Any]:
    """
    Real Google surface data for the agent.
    When hub is provided, we try to get actual task lists and calendars from the adapters.
    This enables the agent to have awareness and use defaults intelligently.
    """
    if hub is None:
        return {
            "task_lists": [],
            "calendars": [],
            "default_task_list": "@default",
            "primary_calendar": "primary",
            "has_connected_google": False,
        }

    # Try to get real data from the adapters
    try:
        gtasks = hub.adapters.get("gtasks")
        gcal = hub.adapters.get("gcal")

        task_lists = []
        if gtasks and hasattr(gtasks, "list_task_lists"):
            res = gtasks.list_task_lists()
            task_lists = res.get("task_lists", []) if isinstance(res, dict) else []

        calendars = []
        if gcal and hasattr(gcal, "list_calendars"):
            res = gcal.list_calendars()
            calendars = res.get("calendars", []) if isinstance(res, dict) else []

        return {
            "task_lists": task_lists,
            "calendars": calendars,
            "default_task_list": "@default",
            "primary_calendar": "primary",
            "has_connected_google": bool(task_lists or calendars),
        }
    except Exception:
        return {
            "task_lists": [],
            "calendars": [],
            "default_task_list": "@default",
            "primary_calendar": "primary",
            "has_connected_google": False,
        }


def _compute_date_context() -> Dict[str, str]:
    """Server-side date awareness for the agent (mandatory for reliable projection).

    Never rely on the LLM to guess "next Friday" or similar.
    All dates are computed from the container's clock.
    """
    now = datetime.now(timezone.utc)
    today = now.date()
    today_iso = today.isoformat()

    def _next_weekday(target_weekday: int) -> str:
        # 0=Mon ... 6=Sun
        days_ahead = target_weekday - today.weekday()
        if days_ahead <= 0:
            days_ahead += 7
        return (today + timedelta(days=days_ahead)).isoformat()

    def _end_of_week(offset_weeks: int = 0) -> str:
        days_until_sunday = 6 - today.weekday()
        return (today + timedelta(days=days_until_sunday + (offset_weeks * 7))).isoformat()

    return {
        "today": today_iso,
        "today_iso": now.isoformat(),
        "next_monday": _next_weekday(0),
        "next_friday": _next_weekday(4),
        "end_of_this_week": _end_of_week(0),
        "end_of_next_week": _end_of_week(1),
    }


def _normalize_role_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _allowed_role_set(grants: Mapping[str, Any]) -> set[str]:
    return {
        _normalize_role_key(item)
        for item in (grants.get("allowed_role_refs") or [])
        if str(item).strip()
    }


def _item_role_ref(item: Mapping[str, Any]) -> str:
    payload = item.get("payload_json") if isinstance(item.get("payload_json"), Mapping) else {}
    return _normalize_role_key(
        item.get("role_ref")
        or item.get("role")
        or payload.get("role_ref")
        or payload.get("role")
    )


def grants_for_principal(principal: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(principal, Mapping) or principal.get("type") != "agent":
        return None
    grants = principal.get("grants")
    if isinstance(grants, Mapping):
        return dict(grants)
    return {
        "allowed_role_refs": list(principal.get("allowed_role_refs") or []),
        "calendar_id": principal.get("calendar_id"),
        "tasklist_id": principal.get("tasklist_id"),
    }


def filter_items_for_grants(
    items: List[Dict[str, Any]],
    grants: Optional[Mapping[str, Any]],
) -> List[Dict[str, Any]]:
    if not grants:
        return list(items)
    allowed = _allowed_role_set(grants)
    if not allowed:
        return []
    return [item for item in items if _item_role_ref(item) in allowed]


def filter_google_context_for_grants(
    google_context: Mapping[str, Any],
    grants: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    out = dict(google_context)
    if not grants:
        return out
    calendar_id = str(grants.get("calendar_id") or "primary").strip() or "primary"
    tasklist_id = str(grants.get("tasklist_id") or "@default").strip() or "@default"
    calendars = []
    for cal in out.get("calendars") or []:
        if not isinstance(cal, Mapping):
            continue
        cid = str(cal.get("id") or "").strip()
        if cid == calendar_id or (
            calendar_id == "primary" and (cid == "primary" or cal.get("primary") is True)
        ):
            calendars.append(dict(cal))
    task_lists = []
    for lst in out.get("task_lists") or []:
        if not isinstance(lst, Mapping):
            continue
        lid = str(lst.get("id") or "").strip()
        if lid == tasklist_id:
            task_lists.append(dict(lst))
    out["calendars"] = calendars
    out["task_lists"] = task_lists
    out["primary_calendar"] = calendar_id
    out["default_task_list"] = tasklist_id
    return out


def filter_classification_context_for_grants(
    context: Mapping[str, Any],
    grants: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    out = dict(context)
    if not grants:
        return out
    allowed = _allowed_role_set(grants)
    name_to_slug = dict(out.get("role_name_to_slug") or {})
    aliases: Dict[str, str] = {}
    for key, value in name_to_slug.items():
        aliases[_normalize_role_key(key)] = _normalize_role_key(value)
        aliases[_normalize_role_key(value)] = _normalize_role_key(value)
    for role in out.get("roles") or []:
        if not isinstance(role, Mapping):
            continue
        slug = _normalize_role_key(role.get("slug"))
        name = _normalize_role_key(role.get("name"))
        if slug:
            aliases[slug] = slug
        if name and slug:
            aliases[name] = slug

    def role_ok(key: Any) -> bool:
        normalized = _normalize_role_key(key)
        if normalized in allowed:
            return True
        mapped = aliases.get(normalized)
        return bool(mapped and mapped in allowed)

    out["roles"] = [
        role
        for role in (out.get("roles") or [])
        if isinstance(role, Mapping) and role_ok(role.get("slug"))
    ]
    filtered_goals: Dict[str, Any] = {}
    for key, value in (out.get("goals_by_role") or {}).items():
        if not role_ok(key):
            continue
        slug = aliases.get(_normalize_role_key(key), _normalize_role_key(key))
        if slug in allowed:
            filtered_goals[slug] = value
    out["goals_by_role"] = filtered_goals
    out["role_name_to_slug"] = {
        key: value
        for key, value in name_to_slug.items()
        if role_ok(value) or role_ok(key)
    }
    out["current_projection"] = filter_items_for_grants(
        list(out.get("current_projection") or []), grants
    )
    if "google_context" in out:
        out["google_context"] = filter_google_context_for_grants(
            out.get("google_context") or {}, grants
        )
    if "items" in out:
        out["items"] = filter_items_for_grants(list(out.get("items") or []), grants)
    return out


def build_classification_context(
    store: HUDStore,
    user_id: str,
    soul_content: str,
    *,
    classification: Mapping[str, Any],
    projection: Mapping[str, Any],
    hub: Optional["HUDAdapterHub"] = None,   # passed so we can populate real google_context
    grants: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    extracted = hud_soul_md_extract_roles_and_goals(soul_content)
    matrix_guidance = build_decision_matrix_guidance_alias()
    context = {
        "mode": "classification_context",
        "onboarding_state": resolve_onboarding_state(store, user_id),
        "mission": extract_mission(soul_content),
        "roles": extracted["roles"],
        "goals_by_role": extracted["goals_by_role"],
        "role_name_to_slug": extracted.get("role_name_to_slug", {}),
        "decision_matrix": DECISION_MATRIX,
        "decision_matrix_guidance": matrix_guidance,
        "push_policy": build_push_policy_status(store, user_id),
        "classification": dict(classification),
        "projection": dict(projection),
        "dates": _compute_date_context(),   # Server-computed date anchors (mandatory for the agent)
        "current_projection": _build_current_projection(store, user_id),  # What is already projected — lets agent detect conflicts intelligently
        "google_context": _build_google_context(hub=hub),  # Real lists/calendars when hub is provided
    }
    return filter_classification_context_for_grants(context, grants)
