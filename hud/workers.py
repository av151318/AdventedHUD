"""Deterministic v1 HUD worker shell for downstream orchestration."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, Mapping, Optional, Sequence

from hud.contracts import (
    HUD_DEFAULT_PROJECTION_MODE,
    normalize_projection_mode,
    HUD_SCOPE_PREFIX,
    HUD_SCOPE_TODAY,
    HUD_SCOPE_WEEK,
)


class HUDIntent(str, Enum):
    """Subset of supported HUD intents for local worker routing."""

    INGEST = "ingest"
    BRIEF = "brief"
    STATUS = "status"
    SYNC_STATUS = "sync_status"
    CLASSIFY = "classify"
    PROJECT = "project"
    APPROVE = "approve"
    REJECT = "reject"
    MCP = "mcp"


HUD_INTENT_INGEST = HUDIntent.INGEST.value
HUD_INTENT_BRIEF = HUDIntent.BRIEF.value
HUD_INTENT_STATUS = HUDIntent.STATUS.value
HUD_INTENT_SYNC_STATUS = HUDIntent.SYNC_STATUS.value
HUD_INTENT_CLASSIFY = HUDIntent.CLASSIFY.value
HUD_INTENT_PROJECT = HUDIntent.PROJECT.value
HUD_INTENT_APPROVE = HUDIntent.APPROVE.value
HUD_INTENT_REJECT = HUDIntent.REJECT.value
HUD_INTENT_MCP = HUDIntent.MCP.value


class HUDWorkerError(ValueError):
    """Typed worker error for unsupported or invalid HUD inputs."""

    def __init__(self, message: str, *, code: str, intent: Optional[str] = None):
        super().__init__(message)
        self.code = code
        self.intent = intent


def _as_text(value: Any, default: str = "") -> str:
    """Return a trimmed string representation for deterministic comparisons."""
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _normalize_id(value: Any) -> str:
    """Normalize identifiers used by worker orchestration."""
    normalized = _as_text(value)
    return normalized.lower()


def _normalize_scope(value: Any) -> str:
    """Normalize HUD scope values for deterministic route logic."""
    return _as_text(value).lower()


def _normalize_payload(payload: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Convert route payload into a deterministic dictionary."""
    if payload is None:
        return {}
    if isinstance(payload, Mapping):
        return dict(payload)
    return {"value": payload}


def _normalize_status(value: Any, *, default: str = "pending") -> str:
    """Normalize item status for local projection decisions."""
    normalized = _as_text(value, default=default).lower()
    return normalized


def _first_non_empty(*values: Any) -> str:
    for value in values:
        text = _as_text(value)
        if text:
            return text
    return ""


def _normalize_payload_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = _as_text(value)
    if not text:
        return None
    normalized = text.lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False
    return None


class HUDWorkers:
    """Pure-Python v1 shell for downstream HUD orchestration."""

    _KNOWN_INTENTS = {member.value for member in HUDIntent}
    _INTENT_STATUS_TRANSITIONS = {
        HUD_INTENT_INGEST: {
            "pending_approval": "pending_approval",
            "pending": "queued",
            "queued": "queued",
            "approved": "approved",
            "rejected": "rejected",
            "failed": "failed",
            "duplicate": "duplicate",
        },
        HUD_INTENT_BRIEF: {},
        HUD_INTENT_STATUS: {},
        HUD_INTENT_SYNC_STATUS: {},
        HUD_INTENT_CLASSIFY: {},
        HUD_INTENT_PROJECT: {},
        HUD_INTENT_APPROVE: {
            "pending": "approved",
            "queued": "approved",
            "failed": "approved",
            "pending_approval": "approved",
            "approved": "approved",
            "rejected": "approved",
            "duplicate": "duplicate",
        },
        HUD_INTENT_REJECT: {
            "pending": "rejected",
            "queued": "rejected",
            "failed": "rejected",
            "pending_approval": "rejected",
            "approved": "rejected",
            "rejected": "rejected",
            "duplicate": "duplicate",
        },
        HUD_INTENT_MCP: {},
    }
    _INTENT_ACTION = {
        HUD_INTENT_INGEST: "ingest_payload",
        HUD_INTENT_BRIEF: "generate_brief",
        HUD_INTENT_STATUS: "query_status",
        HUD_INTENT_SYNC_STATUS: "query_status",
        HUD_INTENT_CLASSIFY: "classify",
        HUD_INTENT_PROJECT: "project",
        HUD_INTENT_APPROVE: "approve_item",
        HUD_INTENT_REJECT: "reject_item",
        HUD_INTENT_MCP: "route_mcp",
    }
    _INTENT_SEMANTIC_TYPE = {
        HUD_INTENT_INGEST: "ingest",
        HUD_INTENT_BRIEF: "briefing",
        HUD_INTENT_STATUS: "state_query",
        HUD_INTENT_SYNC_STATUS: "state_query",
        HUD_INTENT_CLASSIFY: "intent_projection",
        HUD_INTENT_PROJECT: "intent_projection",
        HUD_INTENT_APPROVE: "policy_change",
        HUD_INTENT_REJECT: "policy_change",
        HUD_INTENT_MCP: "transport_routing",
    }
    _INTENT_PRIORITY = {
        HUD_INTENT_INGEST: "high",
        HUD_INTENT_BRIEF: "medium",
        HUD_INTENT_STATUS: "normal",
        HUD_INTENT_SYNC_STATUS: "normal",
        HUD_INTENT_CLASSIFY: "normal",
        HUD_INTENT_PROJECT: "normal",
        HUD_INTENT_APPROVE: "critical",
        HUD_INTENT_REJECT: "critical",
        HUD_INTENT_MCP: "critical",
    }
    _PROJECTABLE_INTENTS = {
        HUD_INTENT_INGEST,
        HUD_INTENT_CLASSIFY,
        HUD_INTENT_PROJECT,
    }
    _NEEDS_ADAPTER_TARGET = {
        HUD_INTENT_INGEST,
        HUD_INTENT_CLASSIFY,
        HUD_INTENT_PROJECT,
    }
    _PROJECTABLE_STATUS_VALUES = frozenset({"pending", "queued", "approved", "ready"})

    @staticmethod
    def _extract_context_value(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
        for key in keys:
            value = _as_text(payload.get(key))
            if value:
                return value
        return ""

    @staticmethod
    def _resolve_default_requires_approval(payload: Mapping[str, Any]) -> Optional[bool]:
        for key in (
            "default_requires_approval",
            "requires_approval_default",
            "approval_requires",
            "approval_required",
            "default_approval",
            "onboarding_requires_approval",
        ):
            normalized = _normalize_payload_bool(payload.get(key))
            if normalized is not None:
                return bool(normalized)

        policy = _as_text(payload.get("approval_policy"))
        if policy:
            normalized_policy = policy.lower().strip()
            if normalized_policy in {"required", "always", "strict", "approve_review_required"}:
                return True
            if normalized_policy in {"optional", "auto", "never", "disabled", "off"}:
                return False

        return None

    @staticmethod
    def _role_goal_present(payload: Mapping[str, Any]) -> bool:
        return bool(
            _as_text(
                payload.get("role_ref")
                or payload.get("goal_ref")
                or payload.get("role")
                or payload.get("role_id")
                or payload.get("roleId")
                or payload.get("goal")
                or payload.get("goal_id")
                or payload.get("goalId")
                or payload.get("onboarding_role_ref")
                or payload.get("onboarding_goal_ref")
                or payload.get("mission_role_ref")
                or payload.get("mission_goal_ref")
                or payload.get("role_context_ref")
                or payload.get("goal_context_ref")
                or payload.get("default_role_ref")
                or payload.get("default_goal_ref")
            )
        )
    _ADAPTER_TARGET_ALIASES = {
        "obsidian": "obsidian",
        "google_calendar": "gcal",
        "gcal": "gcal",
        "calendar": "gcal",
        "google_tasks": "gtasks",
        "gtasks": "gtasks",
        "tasks": "gtasks",
    }
    _GOOGLE_TO_ADAPTER_TARGET = {
        "calendar": "gcal",
        "gcal": "gcal",
        "google_calendar": "gcal",
        "tasks": "gtasks",
        "gtasks": "gtasks",
        "google_tasks": "gtasks",
    }
    _GOOGLE_TARGET_HINT_KEYS: Sequence[str] = (
        "google_target",
        "google_target_id",
        "calendar_id",
        "tasklist_id",
        "task_id",
        "event_id",
    )
    _ADAPTER_METHOD_HINT_KEYS: Sequence[str] = (
        "adapter_method",
        "method",
        "projection_method",
        "write_method",
    )
    _GOOGLE_HINT_KEYS = {
        "calendar": (
            "calendar_id",
            "calendar",
            "calendar_ref",
            "event_id",
            "event",
            "google_id",
        ),
        "tasks": (
            "task_id",
            "tasklist_id",
            "task",
            "google_task_id",
        ),
    }

    def _normalize_intent(self, intent: Any) -> str:
        if hasattr(intent, "value"):
            intent = getattr(intent, "value")
        normalized = _as_text(intent).lower()
        if not normalized:
            raise HUDWorkerError(
                "intent is required",
                code="missing_intent",
                intent=None,
            )
        if normalized not in self._KNOWN_INTENTS:
            raise HUDWorkerError(
                f"unknown intent: {normalized}",
                code="unknown_intent",
                intent=normalized,
            )
        return normalized

    def _resolve_goal_ref(self, payload: Mapping[str, Any]) -> str:
        goal_ref = _as_text(
            payload.get("goal_ref") or payload.get("goalId") or payload.get("goal_id")
        )
        if not goal_ref:
            goal_ref = self._extract_context_value(
                payload,
                (
                    "onboarding_goal_ref",
                    "mission_goal_ref",
                    "goal_context_ref",
                    "default_goal_ref",
                ),
            )
        if goal_ref:
            return goal_ref
        scope = _normalize_scope(payload.get("scope"))
        if scope.startswith(f"{HUD_SCOPE_PREFIX}"):
            candidate = scope[len(HUD_SCOPE_PREFIX) :].strip()
            return _normalize_id(candidate) if candidate else ""
        return ""

    def _normalize_priority(self, payload: Mapping[str, Any], *, intent: str) -> str:
        priority = _as_text(
            payload.get("priority_class") or payload.get("priority") or "normal"
        ).lower()
        if priority in {"critical", "high", "medium", "low", "normal"}:
            return priority
        return self._INTENT_PRIORITY.get(intent, "normal")

    def _normalize_role(self, payload: Mapping[str, Any]) -> str:
        role = _as_text(
            payload.get("role_ref")
            or payload.get("role")
            or payload.get("roleId")
            or payload.get("role_id")
        )
        if not role:
            role = self._extract_context_value(
                payload,
                (
                    "onboarding_role_ref",
                    "mission_role_ref",
                    "role_context_ref",
                    "default_role_ref",
                ),
            )
        return _normalize_id(role) if role else ""

    @staticmethod
    def _normalize_hint_value(payload: Mapping[str, Any], keys: Sequence[str]) -> str:
        for key in keys:
            text = _as_text(payload.get(key))
            if text:
                return text
        return ""

    @staticmethod
    def _coerce_google_target_for_payload(text: str) -> str:
        if not text:
            return ""
        normalized = _as_text(text).replace("-", "_").lower()
        # semantic_type values are NOT valid google_target (event|todo|note vs obsidian|calendar|tasks)
        if normalized in {"event", "todo", "note", "task", "tasks_list"}:
            return ""
        if normalized in {"calendar", "google_calendar", "gcal"}:
            return "calendar"
        if normalized in {"tasks", "google_tasks", "gtasks"}:
            return "tasks"
        if normalized in {"obsidian", "base", "role", "goal"}:
            return "obsidian"
        if normalized == "google":
            return ""
        return ""

    def _resolve_google_target(self, payload: Mapping[str, Any]) -> str:
        explicit = self._coerce_google_target_for_payload(_as_text(payload.get("google_target")))
        if explicit:
            return explicit
        if self._normalize_hint_value(payload, self._GOOGLE_HINT_KEYS["calendar"]):
            return "calendar"
        if self._normalize_hint_value(payload, self._GOOGLE_HINT_KEYS["tasks"]):
            return "tasks"

        # No baked-in default. The agent (after reading the rich default hud.brief
        # containing current_projection, google_context, dates, push_policy, and
        # decision_matrix_guidance) decides google_target and passes it explicitly.
        # Per foundation spec: agent classifies, MCP persists and projects.
        # Obsidian is primary; Google is secondary only when the agent chooses it.
        return "obsidian"

    def _resolve_adapter_target(self, payload: Mapping[str, Any], *, intent: str, google_target: str) -> str:
        explicit_target = _as_text(payload.get("adapter_target"))
        normalized_explicit_target = explicit_target.lower().replace("-", "_").strip() if explicit_target else ""
        if normalized_explicit_target:
            mapped = self._ADAPTER_TARGET_ALIASES.get(normalized_explicit_target)
            if mapped is not None:
                return mapped
        mapped = self._GOOGLE_TO_ADAPTER_TARGET.get(google_target)
        if mapped is not None:
            return mapped
        if intent == HUD_INTENT_MCP and normalized_explicit_target:
            return normalized_explicit_target
        return "obsidian"

    def _resolve_adapter_method(self, payload: Mapping[str, Any], *, projected: bool, intent: str) -> str:
        explicit = _as_text(self._normalize_hint_value(payload, self._ADAPTER_METHOD_HINT_KEYS))
        if explicit:
            return explicit.lower().replace("-", "_")
        if not projected:
            return "sync_state"
        if intent in self._NEEDS_ADAPTER_TARGET:
            return "upsert_item"
        return "sync_state"

    def _resolve_requires_approval(self, payload: Mapping[str, Any], *, projected: bool, intent: str) -> bool:
        explicit = _normalize_payload_bool(payload.get("requires_approval"))
        if explicit is not None:
            return bool(explicit)
        from_context = self._resolve_default_requires_approval(payload)
        if from_context is not None:
            return bool(from_context)
        if not projected:
            return False
        if intent == HUD_INTENT_INGEST:
            return False
        return intent in self._NEEDS_ADAPTER_TARGET

    def _projection_metadata(self, payload: Mapping[str, Any], *, intent: str) -> Dict[str, Any]:
        google_target = self._resolve_google_target(payload)
        projected = self._projected(payload, intent=intent)
        projection_mode = normalize_projection_mode(payload.get("projection_mode"))
        if projection_mode is None:
            projection_mode = HUD_DEFAULT_PROJECTION_MODE
        return {
            "google_target": google_target,
            "adapter_target": self._resolve_adapter_target(
                payload,
                intent=intent,
                google_target=google_target,
            ),
            "mode": projection_mode,
            "adapter_method": self._resolve_adapter_method(payload, projected=projected, intent=intent),
            "requires_approval": self._resolve_requires_approval(payload, projected=projected, intent=intent),
        }

    def _normalised_projection_payload(self, payload: Mapping[str, Any]) -> Dict[str, str]:
        return {
            "goal_ref": self._resolve_goal_ref(payload),
            "role_ref": self._normalize_role(payload),
        }

    def _normalize_action(self, payload: Mapping[str, Any], *, intent: str) -> str:
        action = _as_text(payload.get("action"))
        if action:
            return action
        return self._INTENT_ACTION.get(intent, "noop")

    def _normalize_semantic_type(self, payload: Mapping[str, Any], *, intent: str) -> str:
        semantic_type = _as_text(payload.get("semantic_type"))
        if semantic_type:
            return semantic_type.lower()
        return self._INTENT_SEMANTIC_TYPE.get(intent, "unknown")

    def _scope_allows_projection(self, scope: str) -> bool:
        if not scope:
            return False
        if scope in {HUD_SCOPE_TODAY, HUD_SCOPE_WEEK}:
            return True
        if scope.startswith(HUD_SCOPE_PREFIX):
            suffix = scope[len(HUD_SCOPE_PREFIX) :].strip()
            return bool(suffix)
        return False

    def _status_allows_projection(self, status: str) -> bool:
        return status in self._PROJECTABLE_STATUS_VALUES

    def _projected(self, payload: Mapping[str, Any], *, intent: str) -> bool:
        if intent not in self._PROJECTABLE_INTENTS:
            return False
        scope = _normalize_scope(payload.get("scope"))
        status = _normalize_status(payload.get("status"))
        return self._scope_allows_projection(scope) and self._status_allows_projection(status)

    def _status_transition(self, intent: str, status: Optional[str]) -> Optional[str]:
        intent_matrix = self._INTENT_STATUS_TRANSITIONS.get(intent, {})
        normalized_status = _normalize_status(status)
        if normalized_status in intent_matrix:
            return intent_matrix.get(normalized_status)
        if "*" in intent_matrix:
            return intent_matrix.get("*")
        return None

    def route_intent(self, intent: Any, payload: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """Route an intent to a deterministic local placeholder or error result."""
        normalized_payload = _normalize_payload(payload)
        try:
            normalized_intent = self._normalize_intent(intent)
        except HUDWorkerError as exc:
            return {
                "status": "error",
                "error": {
                    "code": exc.code,
                    "message": str(exc),
                    "intent": exc.intent,
                },
                "result": {
                    "action": "blocked",
                    "intent": _as_text(intent, default=None),
                    "projection": {"projected": False, "status": "pending"},
                    "payload": normalized_payload,
                },
            }

        if "intent" not in normalized_payload:
            normalized_payload = {**normalized_payload, "intent": normalized_intent}

        classification = self.classify(normalized_payload)
        projection = self.project(normalized_payload)

        return {
            "status": "ok",
            "intent": normalized_intent,
            "result": {
                "mode": "matrix",
                "action": classification["action"],
                "classification": classification,
                "projection": projection,
                "payload": normalized_payload,
            },
        }

    def classify(self, payload: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """Classify payload into deterministic worker attributes."""
        normalized_payload = _normalize_payload(payload)
        normalized_intent = self._normalize_intent(
            normalized_payload.get("intent", HUD_INTENT_INGEST)
        )
        transition_to = self._status_transition(normalized_intent, normalized_payload.get("status"))
        projection = self._projection_metadata(normalized_payload, intent=normalized_intent)
        has_context = self._role_goal_present(normalized_payload)
        return {
            "intent": normalized_intent,
            "priority_class": self._normalize_priority(
                normalized_payload, intent=normalized_intent
            ),
            "semantic_type": self._normalize_semantic_type(
                normalized_payload, intent=normalized_intent
            ),
            "role_ref": self._normalize_role(normalized_payload),
            "goal_ref": self._resolve_goal_ref(normalized_payload),
            "action": self._normalize_action(normalized_payload, intent=normalized_intent),
            "google_target": projection["google_target"],
            "adapter_target": projection["adapter_target"],
            "adapter_method": projection["adapter_method"],
            "requires_approval": projection["requires_approval"],
            "projection_mode": projection["mode"],
            "status_transition": {
                "from": _normalize_status(normalized_payload.get("status"), default="pending"),
                "to": transition_to,
                "allowed": transition_to is not None,
                "intent": normalized_intent,
            },
            "onboarding_needed": not has_context,
        }

    def project(self, item: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
        """Return a deterministic projection placeholder outcome."""
        normalized_item = _normalize_payload(item)
        normalized_intent = self._normalize_intent(
            normalized_item.get("intent", HUD_INTENT_INGEST)
        )
        scope = _normalize_scope(normalized_item.get("scope"))
        status = _normalize_status(normalized_item.get("status"))

        projection = self._projection_metadata(normalized_item, intent=normalized_intent)
        projected = self._projected(normalized_item, intent=normalized_intent)
        normalized_refs = self._normalised_projection_payload(normalized_item)

        return {
            "projected": projected,
            "status": "projected" if projected else "pending",
            "mode": projection["mode"],
            "scope": scope or "unknown",
            "item_status": status,
            "google_target": projection["google_target"],
            "adapter_target": projection["adapter_target"],
            "adapter_method": projection["adapter_method"],
            "requires_approval": projection["requires_approval"],
            "role_ref": normalized_refs["role_ref"],
            "goal_ref": normalized_refs["goal_ref"],
            "onboarding_needed": not self._role_goal_present(normalized_item),
        }


__all__ = [
    "HUDIntent",
    "HUD_INTENT_INGEST",
    "HUD_INTENT_BRIEF",
    "HUD_INTENT_STATUS",
    "HUD_INTENT_SYNC_STATUS",
    "HUD_INTENT_CLASSIFY",
    "HUD_INTENT_PROJECT",
    "HUD_INTENT_APPROVE",
    "HUD_INTENT_REJECT",
    "HUD_INTENT_MCP",
    "HUDWorkerError",
    "HUDWorkers",
]
