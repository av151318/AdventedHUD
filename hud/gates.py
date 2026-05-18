"""HUD request gates: admin auth, user id, onboarding, push policy."""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from aiohttp import web

from hud.contracts import (
    HUD_ERROR_HTTP_STATUS,
    HUD_PROJECTION_MODE_DRY_RUN,
    HUD_PROJECTION_MODE_LIVE,
    HUD_ROUTE_MCP,
    hud_error_payload,
    normalize_projection_mode,
    parse_projection_mode,
)
from hud.meta import finalize_hud_data
from hud.store import HUDStore

logger = logging.getLogger(__name__)

HUD_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOUL_PATH = HUD_PACKAGE_ROOT.parent / "data" / "obsidian" / "AdventedHUD" / "soul.md"


def hud_actor(request: web.Request) -> Optional[str]:
    actor = request.headers.get("X-HUD-Actor") or request.headers.get("x-hud-actor")
    return actor.strip() if actor and actor.strip() else None


def hud_normalize_identifier(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def hud_resolve_user_id(
    request: web.Request,
    payload: Optional[Mapping[str, Any]] = None,
) -> str:
    if os.environ.get("HUD_RESOLVE_JWT_SUBJECT", "").strip().lower() in {"1", "true", "yes"}:
        auth = request.headers.get("Authorization") or request.headers.get("authorization")
        if isinstance(auth, str) and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
            sub = hud_jwt_subject_unverified(token)
            if sub:
                return sub
    candidates = (
        request.headers.get("X-HUD-User-Id"),
        request.headers.get("x-hud-user-id"),
        request.query.get("user_id"),
    )
    if payload is not None:
        candidates += (
            payload.get("user_id"),
            payload.get("userId"),
            payload.get("actor"),
        )
    actor = hud_actor(request)
    if actor is not None:
        candidates += (actor,)
    for candidate in candidates:
        text = hud_normalize_identifier(candidate)
        if text:
            return text
    return "localuser"


def hud_jwt_subject_unverified(token: str) -> Optional[str]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    body = parts[1]
    pad = "=" * ((4 - len(body) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(body + pad)
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    sub = payload.get("sub")
    if isinstance(sub, str) and sub.strip():
        return sub.strip()
    return None


def hud_admin_key() -> Optional[str]:
    admin_key = os.environ.get("HUD_ADMIN_API_KEY", "").strip()
    return admin_key or None


async def require_hud_admin(request: web.Request) -> Optional[web.Response]:
    actor = hud_actor(request)
    required_key = hud_admin_key()
    if not required_key:
        return web.json_response(
            hud_error_payload(
                "HUD admin key is not configured",
                "authentication_error",
                "missing_hud_admin_key",
                service="HUD",
                route=str(request.path),
                actor=actor,
            ),
            status=401,
        )
    provided_key = request.headers.get("X-HUD-Admin-Key") or request.headers.get(
        "x-hud-admin-key"
    )
    if provided_key == required_key:
        return None
    return web.json_response(
        hud_error_payload(
            "Invalid or missing HUD admin key",
            "authentication_error",
            "invalid_hud_admin_key",
            route=str(request.path),
            actor=actor,
        ),
        status=401,
    )


def hud_agent_safe_onboarding_errors() -> bool:
    return os.environ.get("HUD_AGENT_SAFE_ONBOARDING_ERRORS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def hud_require_post_onboarding_push_policy() -> bool:
    raw = (os.environ.get("HUD_REQUIRE_POST_ONBOARDING_PUSH") or "1").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def hud_maybe_redact_onboarding_error_data(data: Mapping[str, Any]) -> Dict[str, Any]:
    if not hud_agent_safe_onboarding_errors():
        return dict(data)
    return {
        "onboarding_needed": bool(data.get("onboarding_needed", True)),
        "onboarding_complete": bool(data.get("onboarding_complete", False)),
        "next_action": str(data.get("next_action") or "onboard"),
    }


def hud_maybe_redact_push_policy_error_data(data: Mapping[str, Any]) -> Dict[str, Any]:
    if not hud_agent_safe_onboarding_errors():
        return dict(data)
    return {
        "post_onboarding_push_required": bool(
            data.get("post_onboarding_push_required", True)
        ),
        "next_action": "choose_push_policy",
    }


def hud_parse_explicit_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def hud_effective_projection_mode(
    request: web.Request,
    *,
    store: HUDStore,
    user_id: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    requested = None
    if payload is not None:
        requested = normalize_projection_mode(payload.get("projection_mode"))
    if requested is None:
        requested = normalize_projection_mode(request.query.get("projection_mode"))
    from_store = store.get_user_projection_mode(user_id)
    effective = requested or from_store or HUD_PROJECTION_MODE_DRY_RUN
    if effective not in {HUD_PROJECTION_MODE_DRY_RUN, HUD_PROJECTION_MODE_LIVE}:
        raise ValueError("projection_mode must be 'dry_run', 'live', or 'direct' (alias for live)")
    return {
        "effective_projection_mode": effective,
        "projection_mode_requested": requested,
        "projection_mode_from_store": from_store,
    }


def hud_projection_mode_client_fields(bundle: Mapping[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "effective_projection_mode": bundle.get("effective_projection_mode"),
        "projection_mode_requested": bundle.get("projection_mode_requested"),
        "projection_mode_from_store": bundle.get("projection_mode_from_store"),
    }
    pw = bundle.get("persist_warning")
    if pw:
        fields["persist_warning"] = pw
    return {k: v for k, v in fields.items() if v is not None or k == "effective_projection_mode"}


def hud_push_policy_client_fields(store: HUDStore, user_id: str) -> Dict[str, Any]:
    if not hud_require_post_onboarding_push_policy():
        return {}
    try:
        policy = store.get_user_push_policy(user_id)
    except Exception:
        logger.exception("HUD get_user_push_policy failed user_id=%s", user_id)
        policy = None
    if policy is not None:
        return {"post_onboarding_push_policy_required": False}
    return {
        "post_onboarding_push_policy_required": True,
        "next_action": "choose_push_policy",
    }


def hud_onboarding_gate_response_if_blocked(
    *,
    store: HUDStore,
    route: str,
    actor: Optional[str],
    user_id: str,
    onboarding_context_status,
) -> Optional[web.Response]:
    from hud.onboarding import hud_soul_md_gate_issues, hud_soul_md_path

    ctx = onboarding_context_status()
    if not ctx.get("required"):
        return None
    canon = hud_soul_md_path().expanduser().resolve()
    gate_issues: list[str] = []
    try:
        if canon.is_file():
            gate_issues = hud_soul_md_gate_issues(canon.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        gate_issues = []
    try:
        db_state = store.get_user_onboarding_state("default") or {}
        if not db_state.get("role_ref"):
            conn = store._connect()
            row = conn.execute(
                "SELECT 1 FROM hud_user_onboarding_states WHERE role_ref IS NOT NULL LIMIT 1"
            ).fetchone()
            conn.close()
            if not row:
                gate_issues = gate_issues + ["atomic_db_state_missing"]
    except Exception:
        gate_issues = gate_issues + ["atomic_db_state_check_failed"]

    full_data: Dict[str, Any] = {
        "onboarding_needed": True,
        "onboarding_complete": False,
        "onboarding_gate_issues": gate_issues,
        "canonical_path": str(canon),
        "onboarding_needed_reason": ctx,
        "next_action": "onboard",
    }
    error_data = finalize_hud_data(
        hud_maybe_redact_onboarding_error_data(full_data),
        store=store,
        user_id=user_id,
    )
    body = hud_error_payload(
        "Complete personal context onboarding (soul.md) before this HUD operation.",
        "onboarding_required",
        "must_complete_onboarding",
        route=route,
        actor=actor,
        data=error_data,
    )
    return web.json_response(body, status=HUD_ERROR_HTTP_STATUS["onboarding_required"])


def hud_push_policy_gate_response_if_blocked(
    *,
    store: HUDStore,
    route: str,
    actor: Optional[str],
    user_id: str,
    onboarding_context_complete,
) -> Optional[web.Response]:
    if not hud_require_post_onboarding_push_policy():
        return None
    if not onboarding_context_complete():
        return None
    try:
        policy = store.get_user_push_policy(user_id)
    except Exception:
        logger.exception("HUD get_user_push_policy failed user_id=%s", user_id)
        policy = None
    if policy is not None:
        return None
    full_data: Dict[str, Any] = {
        "post_onboarding_push_required": True,
        "next_action": "choose_push_policy",
        "onboarding_needed": False,
        "onboarding_complete": True,
    }
    error_data = finalize_hud_data(
        hud_maybe_redact_push_policy_error_data(full_data),
        store=store,
        user_id=user_id,
    )
    body = hud_error_payload(
        "Mission profile is ready; confirm whether HUD may push to external calendar and tasks "
        "without per-item approval.",
        "policy_required",
        "must_complete_push_policy",
        route=route,
        actor=actor,
        data=error_data,
    )
    return web.json_response(body, status=HUD_ERROR_HTTP_STATUS["push_policy_required"])


def parse_projection_mode_from_payload(payload: Mapping[str, Any]) -> str:
    mode = payload.get("projection_mode")
    if mode is None:
        return HUD_PROJECTION_MODE_DRY_RUN
    return parse_projection_mode(mode)
