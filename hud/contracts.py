"""Contracts and validation helpers for HUD endpoints."""

import json
import logging
import re
from enum import Enum
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


class HudIntent(str, Enum):
    INGEST = "ingest"
    BRIEF = "brief"
    PROJECT = "project"
    MCP = "mcp"
    ONBOARDING = "onboarding"


HUD_INTENT_INGEST = HudIntent.INGEST.value
HUD_INTENT_BRIEF = HudIntent.BRIEF.value
HUD_INTENT_PROJECT = HudIntent.PROJECT.value
HUD_INTENT_MCP = HudIntent.MCP.value
HUD_INTENT_ONBOARDING = HudIntent.ONBOARDING.value

HUD_ROUTE_INGEST = "/hud/ingest"
HUD_ROUTE_BRIEF = "/hud/brief"
HUD_ROUTE_PROJECT = "/hud/project"
HUD_ROUTE_SYNC_STATUS = "/hud/sync_status"
HUD_ROUTE_STATUS_COMPAT = "/hud/status"
HUD_ROUTE_MCP = "/hud/mcp"
HUD_ROUTE_ONBOARDING_SOUL = "/hud/onboarding/soul"

HUD_ROUTE_PUSH_POLICY = "/hud/push_policy"

HUD_SCOPE_TODAY = "today"
HUD_SCOPE_WEEK = "week"
HUD_SCOPE_PREFIX = "goal:"

HUD_PROJECTION_MODE_DRY_RUN = "dry_run"
HUD_PROJECTION_MODE_LIVE = "live"
# Plan prose used "direct"; runtime canonical is `live` (accepted as alias in parse/normalize).
HUD_PROJECTION_MODE_DIRECT_ALIASES = frozenset({"direct"})
HUD_VALID_PROJECTION_MODES = frozenset({HUD_PROJECTION_MODE_DRY_RUN, HUD_PROJECTION_MODE_LIVE})
HUD_DEFAULT_PROJECTION_MODE = HUD_PROJECTION_MODE_DRY_RUN

# v1.4: hud.onboarding accepts atomic roles/goals or returns [MCP RITUAL MODE - STRICT PROCEDURE] rejection
HUD_MCP_METHODS = {
    "hud.ingest": HUD_INTENT_INGEST,
    "hud.brief": HUD_INTENT_BRIEF,
    "hud.project": HUD_INTENT_PROJECT,
    "hud.onboarding": HUD_INTENT_ONBOARDING,
    "hud.mcp": HUD_INTENT_MCP,
}

# v1.3: hud.approve / hud.reject removed from MCP surface (no longer in HUD_MCP_METHODS).
# Use hud.project with {"action": "approve" | "reject" | "project", "item_id": "...", ...} for item fate.
# hud.project is the single consolidated tool for projection, approval, rejection decisions.

HUD_ERROR_HTTP_STATUS = {
    "validation_error": 400,
    "invalid_payload": 400,
    "invalid_item_id": 404,
    "invalid_scope": 400,
    "method_not_found": 404,
    "not_implemented": 501,
    "onboarding_required": 409,
    "push_policy_required": 409,
}


_HUD_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_HUD_GOAL_SCOPE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def require_json(request_body: Any) -> Dict[str, Any]:
    """Coerce and normalize a request body to a HUD payload dictionary."""
    try:
        return coerce_hud_request_payload(request_body)
    except ValueError as exc:
        logger.warning("Malformed HUD JSON payload: %s", exc)
        raise


def coerce_hud_request_payload(raw_json: Any) -> Dict[str, Any]:
    """Return a normalized HUD payload dict, or raise ValueError with reason."""
    if raw_json is None:
        raise ValueError("HUD payload is missing")

    if isinstance(raw_json, (bytes, bytearray)):
        try:
            raw_json = raw_json.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("HUD payload must be UTF-8 encoded JSON") from exc

    if isinstance(raw_json, str):
        body = raw_json.strip()
        if not body:
            raise ValueError("HUD payload is empty")
        try:
            raw_json = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError("HUD payload is not valid JSON") from exc

    if not isinstance(raw_json, dict):
        raise ValueError("HUD payload must be a JSON object")

    normalized: Dict[str, Any] = {}
    for key, value in raw_json.items():
        if not isinstance(key, str):
            logger.warning("HUD payload key is not a string: %r", type(key).__name__)
            raise ValueError("HUD payload keys must be strings")
        normalized[key] = value
    return normalized


def validate_hud_id(item_id: Any) -> str:
    """Validate a HUD item identifier from route path params."""
    if not isinstance(item_id, str):
        logger.warning("Invalid HUD item_id type: %r", type(item_id).__name__)
        raise ValueError("item_id must be a non-empty string")

    normalized = item_id.strip()
    if not normalized:
        logger.warning("HUD item_id is missing")
        raise ValueError("item_id is required")

    if not _HUD_ITEM_ID_RE.fullmatch(normalized):
        logger.warning("Invalid HUD item_id format: %r", normalized)
        raise ValueError("item_id contains invalid characters")

    return normalized


def parse_hud_scope(scope: Optional[str]) -> str:
    """Parse accepted HUD scope values."""
    if scope is None:
        return HUD_SCOPE_TODAY

    if not isinstance(scope, str):
        logger.warning("Invalid HUD scope type: %r", type(scope).__name__)
        raise ValueError("scope must be a string")

    normalized = scope.strip().lower()
    if normalized in {HUD_SCOPE_TODAY, HUD_SCOPE_WEEK}:
        return normalized

    if normalized.startswith(HUD_SCOPE_PREFIX):
        slug = normalized[len(HUD_SCOPE_PREFIX) :]
        if not slug:
            logger.warning("HUD scope goal form is missing slug: %r", scope)
            raise ValueError("scope goal form requires non-empty slug")
        if not _HUD_GOAL_SCOPE_RE.fullmatch(slug):
            logger.warning("HUD scope slug failed validation: %r", scope)
            raise ValueError("scope goal slug is invalid")
        return f"{HUD_SCOPE_PREFIX}{slug}"

    logger.warning("Unsupported HUD scope value: %r", scope)
    raise ValueError("scope must be 'today', 'week', or 'goal:<slug>'")


def parse_projection_mode(value: Any) -> str:
    """Parse and validate a projection mode value."""
    if isinstance(value, str):
        normalized = value.strip().lower()
    else:
        if value is None:
            raise ValueError("projection_mode is required when provided")
        normalized = str(value).strip().lower()
    if not normalized:
        raise ValueError("projection_mode is required when provided")
    if normalized in HUD_PROJECTION_MODE_DIRECT_ALIASES:
        normalized = HUD_PROJECTION_MODE_LIVE
    if normalized not in HUD_VALID_PROJECTION_MODES:
        raise ValueError("projection_mode must be 'dry_run', 'live', or 'direct' (alias for live)")
    return normalized


def normalize_projection_mode(value: Any) -> Optional[str]:
    """Return a normalized projection mode or None when missing."""
    if value is None:
        return None
    if isinstance(value, str):
        normalized = value.strip().lower()
    else:
        normalized = str(value).strip().lower()
    if not normalized:
        return None
    if normalized in HUD_PROJECTION_MODE_DIRECT_ALIASES:
        normalized = HUD_PROJECTION_MODE_LIVE
    if normalized not in HUD_VALID_PROJECTION_MODES:
        return None
    return normalized


def hud_success_payload(
    route: str,
    *,
    data: Optional[dict] = None,
    status: str = "ok",
    actor: Optional[str] = None,
    route_meta: Optional[dict] = None,
    output: Optional[list] = None,
    choices: Optional[list] = None,
) -> Dict[str, Any]:
    """Build a success-like OAI-compatible payload."""
    payload: Dict[str, Any] = {
        "service": "hud",
        "route": route,
        "status": status,
        "data": data if data is not None else {},
        "output": output if output is not None else [],
        "choices": choices if choices is not None else [],
    }
    if actor is not None:
        payload["actor"] = actor
    if route_meta is not None:
        payload["route_meta"] = route_meta
    return payload


def hud_error_payload(
    message: str,
    type: str,
    code: str,
    *,
    service: str = "hud",
    route: Optional[str] = None,
    actor: Optional[str] = None,
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build an OAI-like error payload."""
    payload: Dict[str, Any] = {
        "error": {
            "message": message,
            "type": type,
            "code": code,
        },
        "service": service,
        "status": "error",
        "output": [],
        "choices": [],
    }
    if route is not None:
        payload["route"] = route
    if actor is not None:
        payload["actor"] = actor
    if data is not None:
        payload["data"] = data
    return payload
