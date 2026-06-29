"""DB-primary onboarding completeness (HUD v1.4.1)."""

from __future__ import annotations

import os
from typing import Any, Dict, List, Mapping, Optional

from hud.store import HUDStore


def _normalize_identifier(value: Any) -> Optional[str]:
    if value is None:
        return None
    candidate = str(value).strip()
    return candidate if candidate else None


def _require_post_onboarding_push_policy() -> bool:
    raw = (os.environ.get("HUD_REQUIRE_POST_ONBOARDING_PUSH") or "1").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def validate_atomic_payload(
    roles: Any,
    goals_by_role: Any,
) -> List[str]:
    issues: List[str] = []
    if not isinstance(roles, list) or not roles:
        issues.append("roles_empty")
        return issues

    role_slugs: List[str] = []
    for idx, role in enumerate(roles):
        if not isinstance(role, dict):
            issues.append(f"roles[{idx}]:not_object")
            continue
        slug = _normalize_identifier(role.get("slug") or role.get("role_ref"))
        name = str(role.get("name") or "").strip()
        if not slug:
            issues.append(f"roles[{idx}]:missing_slug")
        elif slug in role_slugs:
            issues.append(f"roles[{idx}]:duplicate_slug")
        else:
            role_slugs.append(slug)
        if not name:
            issues.append(f"roles[{idx}]:missing_name")

    if not isinstance(goals_by_role, dict) or not goals_by_role:
        issues.append("goals_by_role_empty")
    else:
        for role_key, goals in goals_by_role.items():
            rk = _normalize_identifier(role_key)
            if rk and rk not in role_slugs and role_key not in role_slugs:
                issues.append(f"goals_by_role:unknown_role:{role_key}")
            if not isinstance(goals, list) or not goals:
                issues.append(f"goals_by_role:{role_key}:empty")
                continue
            for gidx, goal in enumerate(goals):
                if isinstance(goal, dict):
                    if not str(goal.get("goal") or "").strip():
                        issues.append(f"goals_by_role:{role_key}[{gidx}]:missing_goal")
                elif not str(goal).strip():
                    issues.append(f"goals_by_role:{role_key}[{gidx}]:missing_goal")

    return issues


def is_user_onboarding_atomic_complete(store: HUDStore, user_id: str) -> bool:
    atomic = store.get_user_onboarding_atomic(user_id)
    if not atomic:
        return False
    issues = validate_atomic_payload(
        atomic.get("roles"),
        atomic.get("goals_by_role"),
    )
    return not issues


def is_user_fully_onboarded(store: HUDStore, user_id: str) -> bool:
    if not is_user_onboarding_atomic_complete(store, user_id):
        return False
    if _require_post_onboarding_push_policy():
        try:
            if store.get_user_push_policy(user_id) is None:
                return False
        except Exception:
            return False
    return True


def hud_onboarding_db_status(store: HUDStore, user_id: str) -> Dict[str, Any]:
    if is_user_fully_onboarded(store, user_id):
        return {"required": False, "source": "db", "reasons": [], "details": {}}
    if is_user_onboarding_atomic_complete(store, user_id):
        return {
            "required": True,
            "source": "db",
            "reasons": ["awaiting_push_policy"],
            "details": {},
        }
    atomic = store.get_user_onboarding_atomic(user_id)
    if atomic:
        issues = validate_atomic_payload(
            atomic.get("roles"),
            atomic.get("goals_by_role"),
        )
        return {
            "required": True,
            "source": "db",
            "reasons": ["atomic_invalid"],
            "details": {"validation_issues": issues},
        }
    return {
        "required": True,
        "source": "db",
        "reasons": ["atomic_missing"],
        "details": {},
    }


def hud_onboarding_context_complete(store: HUDStore, user_id: str) -> bool:
    return is_user_fully_onboarded(store, user_id)


def _parse_atomic_primary_refs(
    payload: Mapping[str, Any], *, roles: Any, goals_by_role: Any
) -> Dict[str, Optional[str]]:
    primary_role_ref = _normalize_identifier(
        payload.get("primary_role_ref") or payload.get("role_ref")
    )
    primary_goal_ref = _normalize_identifier(
        payload.get("primary_goal_ref") or payload.get("goal_ref")
    )
    atomic = payload.get("atomic")
    if isinstance(atomic, dict):
        primary_role_ref = primary_role_ref or _normalize_identifier(
            atomic.get("primary_role_ref") or atomic.get("role_ref")
        )
        primary_goal_ref = primary_goal_ref or _normalize_identifier(
            atomic.get("primary_goal_ref") or atomic.get("goal_ref")
        )
    if not primary_role_ref and isinstance(roles, list) and roles:
        first = roles[0]
        if isinstance(first, dict):
            primary_role_ref = _normalize_identifier(
                first.get("slug") or first.get("role_ref") or first.get("name")
            )
    if not primary_goal_ref and isinstance(goals_by_role, dict):
        for role_key, goals in goals_by_role.items():
            if not primary_role_ref:
                primary_role_ref = _normalize_identifier(role_key)
            if isinstance(goals, list) and goals:
                g0 = goals[0]
                if isinstance(g0, dict):
                    primary_goal_ref = _normalize_identifier(
                        g0.get("goal") or g0.get("goal_ref")
                    )
                elif isinstance(g0, str):
                    primary_goal_ref = _normalize_identifier(g0)
            if primary_role_ref and primary_goal_ref:
                break
    return {
        "primary_role_ref": primary_role_ref,
        "primary_goal_ref": primary_goal_ref,
    }


def parse_atomic_from_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    roles = payload.get("roles") or payload.get("atomic_roles")
    goals_by_role = payload.get("goals_by_role")
    atomic = payload.get("atomic")
    if isinstance(atomic, dict):
        roles = roles or atomic.get("roles")
        goals_by_role = goals_by_role or atomic.get("goals_by_role")
    roles = roles if isinstance(roles, list) else []
    goals_by_role = goals_by_role if isinstance(goals_by_role, dict) else {}
    refs = _parse_atomic_primary_refs(
        payload, roles=roles, goals_by_role=goals_by_role
    )
    return {
        "roles": roles,
        "goals_by_role": goals_by_role,
        "primary_role_ref": refs["primary_role_ref"],
        "primary_goal_ref": refs["primary_goal_ref"],
    }
