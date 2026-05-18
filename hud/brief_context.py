"""Default hud.brief classification context: mission, matrix, onboarding state."""

from __future__ import annotations

import re
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


def build_classification_context(
    store: HUDStore,
    user_id: str,
    soul_content: str,
    *,
    classification: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> Dict[str, Any]:
    extracted = hud_soul_md_extract_roles_and_goals(soul_content)
    matrix_guidance = build_decision_matrix_guidance_alias()
    return {
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
    }
