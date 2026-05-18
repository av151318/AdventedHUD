"""HUD onboarding: soul.md paths, gate checks, read/write responses."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from aiohttp import web

from hud.contracts import (
    HUD_ERROR_HTTP_STATUS,
    HUD_ROUTE_ONBOARDING_SOUL,
    hud_error_payload,
    hud_success_payload,
)
from hud.contracts import HUD_PROJECTION_MODE_DRY_RUN, HUD_PROJECTION_MODE_LIVE
from hud.gates import (
    DEFAULT_SOUL_PATH,
    hud_actor,
    hud_normalize_identifier,
    hud_parse_explicit_bool,
    hud_push_policy_client_fields,
    hud_require_post_onboarding_push_policy,
    hud_resolve_user_id,
)
from hud.meta import finalize_hud_data
from hud.store import HUDStore

logger = logging.getLogger(__name__)

HUD_SOUL_MD_PLACEHOLDER_TOKENS: tuple[str, ...] = (
    "[your answer",
    "[your mission statement",
    "example-founder",
    "example-work",
    "example-personal",
    "example-quarterly-objective",
    "example-monthly-focus",
    "example-growth-experiment",
)
HUD_SOUL_MD_WRITE_MAX_BYTES = 524288


def hud_soul_md_path() -> Path:
    override = os.environ.get("HUD_SOUL_MD_PATH") or os.environ.get("SOUL_MD_PATH")
    if not override:
        override = str(DEFAULT_SOUL_PATH)
    return Path(override.strip()).expanduser()


def hud_soul_md_worksheet_read_path() -> Path:
    explicit = os.environ.get("HUD_SOUL_MD_TEMPLATE_PATH") or os.environ.get(
        "SOUL_MD_TEMPLATE_PATH"
    )
    canonical = hud_soul_md_path().expanduser().resolve()
    if explicit:
        candidate = Path(explicit.strip()).expanduser().resolve()
        if candidate.is_file():
            return candidate
        logger.warning(
            "HUD_SOUL_MD_TEMPLATE_PATH points to a missing file (%s); using worksheet fallback",
            candidate,
        )
    sibling = (canonical.parent / "soul.template.md").expanduser().resolve()
    if sibling.is_file() and sibling != canonical:
        return sibling
    return canonical


def hud_soul_md_part_table_populated(content: str, part_number: int) -> bool:
    part_pattern = re.compile(
        rf"(?ms)^\s*##\s*Part\s+{part_number}\b.*?(?=^\s*##\s*Part\s+\d+\b|\Z)"
    )
    part_match = part_pattern.search(content)
    if not part_match:
        return False
    section_text = part_match.group(0)
    table_rows = [
        line.strip() for line in section_text.splitlines() if line.strip().startswith("|")
    ]
    if not table_rows:
        return False
    separator_pattern = re.compile(r"^\|\s*:?-{3,}\s*(\|\s*:?-{3,}\s*)+\|?$")
    separator_index = None
    for idx, row in enumerate(table_rows):
        if separator_pattern.match(row):
            separator_index = idx
            break
    if separator_index is None:
        return False
    for row in table_rows[separator_index + 1 :]:
        if separator_pattern.match(row):
            continue
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if any(cells) and any(cell.strip() for cell in cells):
            return True
    return False


def hud_soul_md_gate_issues(content: str) -> List[str]:
    issues: List[str] = []
    lower_content = str(content).lower()
    for token in HUD_SOUL_MD_PLACEHOLDER_TOKENS:
        if token in lower_content:
            issues.append(f"placeholder_token:{token}")
    if not hud_soul_md_part_table_populated(content, 12):
        issues.append("part_12:no_populated_table_row")
    if not hud_soul_md_part_table_populated(content, 13):
        issues.append("part_13:no_populated_table_row")
    return issues


def hud_soul_md_has_placeholder_content(content: str) -> bool:
    return bool(hud_soul_md_gate_issues(content))


def hud_onboarding_context_status() -> Dict[str, Any]:
    path = hud_soul_md_path()
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {
            "required": True,
            "source": "soul_md",
            "reasons": ["missing_file"],
            "details": {},
        }
    except OSError as exc:
        return {
            "required": True,
            "source": "soul_md",
            "reasons": ["read_error"],
            "details": {"error": str(exc)},
        }
    if not content or not content.strip():
        return {
            "required": True,
            "source": "soul_md",
            "reasons": ["empty_file"],
            "details": {},
        }
    if hud_soul_md_has_placeholder_content(content):
        return {
            "required": True,
            "source": "soul_md",
            "reasons": ["placeholder_content"],
            "details": {"gate_issues": hud_soul_md_gate_issues(content)},
        }
    return {"required": False, "source": None, "reasons": [], "details": {}}


def hud_onboarding_context_complete() -> bool:
    return not hud_onboarding_context_status()["required"]


def hud_onboarding_params_has_markdown(payload: Mapping[str, Any]) -> bool:
    for key in ("markdown", "content", "body", "text"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return True
    return False


def hud_onboarding_params_is_push_only(payload: Mapping[str, Any]) -> bool:
    return (
        "external_push_without_approval" in payload
        and not hud_onboarding_params_has_markdown(payload)
    )


def hud_onboarding_params_has_atomic(payload: Mapping[str, Any]) -> bool:
    for key in ("roles", "atomic_roles", "goals_by_role", "atomic"):
        if key in payload and payload.get(key):
            return True
    return False


def hud_extract_default_requires_approval(payload: Optional[Mapping[str, Any]]) -> Optional[bool]:
    if not isinstance(payload, Mapping):
        return None
    explicit = payload.get("requires_approval")
    if "requires_approval" in payload:
        parsed = hud_parse_explicit_bool(explicit)
        if parsed is not None:
            return parsed
    for key in (
        "default_requires_approval",
        "requires_approval_default",
        "approval_requires",
        "approval_required",
        "default_approval",
        "onboarding_requires_approval",
    ):
        parsed = hud_parse_explicit_bool(payload.get(key))
        if parsed is not None:
            return parsed
    policy = payload.get("approval_policy")
    if isinstance(policy, str):
        normalized_policy = policy.strip().lower()
        if normalized_policy in {"required", "always", "strict", "approve_review_required"}:
            return True
        if normalized_policy in {"optional", "auto", "never", "disabled", "off"}:
            return False
    return None


def hud_apply_user_onboarding_context(
    payload: Mapping[str, Any],
    *,
    store: HUDStore,
    user_id: str,
) -> Dict[str, Any]:
    enriched = dict(payload)
    onboarding_status = hud_onboarding_context_status()
    onboarding_needed = onboarding_status["required"]
    try:
        context = store.get_user_onboarding_state(user_id) if user_id else None
    except Exception:
        logger.exception("Failed to load onboarding context for user_id=%s", user_id)
        context = None

    enriched["onboarding_needed"] = onboarding_needed
    enriched["onboarding_needed_reason"] = onboarding_status
    if not context or onboarding_needed:
        return enriched

    role_present = hud_normalize_identifier(
        payload.get("role_ref")
        or payload.get("role")
        or payload.get("roleId")
        or payload.get("role_id")
    )
    goal_present = hud_normalize_identifier(
        payload.get("goal_ref") or payload.get("goalId") or payload.get("goal_id")
    )
    if not role_present and context.get("role_ref"):
        enriched["role_ref"] = context.get("role_ref")
    if not goal_present and context.get("goal_ref"):
        enriched["goal_ref"] = context.get("goal_ref")
    if "requires_approval" not in payload and context.get("requires_approval") is not None:
        enriched["requires_approval"] = context.get("requires_approval")
    return enriched


def hud_store_user_onboarding_state(
    store: HUDStore,
    user_id: str,
    *,
    payload: Mapping[str, Any],
    classification: Mapping[str, Any],
) -> None:
    if not user_id:
        return
    role_ref = classification.get("role_ref")
    goal_ref = classification.get("goal_ref")
    requires_approval = hud_extract_default_requires_approval(payload)
    if role_ref is None and goal_ref is None and requires_approval is None:
        return
    store.set_user_onboarding_state(
        user_id,
        role_ref=role_ref if hud_normalize_identifier(role_ref) else None,
        goal_ref=goal_ref if hud_normalize_identifier(goal_ref) else None,
        requires_approval=requires_approval,
    )


def hud_onboarding_parse_atomic_refs(payload: Mapping[str, Any]) -> Dict[str, Optional[str]]:
    role_ref = hud_normalize_identifier(
        payload.get("primary_role_ref") or payload.get("role_ref")
    )
    goal_ref = hud_normalize_identifier(
        payload.get("primary_goal_ref") or payload.get("goal_ref")
    )
    atomic = payload.get("atomic")
    if isinstance(atomic, dict):
        role_ref = role_ref or hud_normalize_identifier(
            atomic.get("role_ref") or atomic.get("primary_role_ref")
        )
        goal_ref = goal_ref or hud_normalize_identifier(
            atomic.get("goal_ref") or atomic.get("primary_goal_ref")
        )
    roles = payload.get("roles") or payload.get("atomic_roles")
    if isinstance(roles, list) and roles:
        first = roles[0]
        if isinstance(first, dict):
            role_ref = role_ref or hud_normalize_identifier(
                first.get("slug") or first.get("role_ref") or first.get("name")
            )
        elif isinstance(first, str):
            role_ref = role_ref or hud_normalize_identifier(first)
    goals_by_role = payload.get("goals_by_role")
    if isinstance(goals_by_role, dict):
        for role_key, goals in goals_by_role.items():
            if not role_ref:
                role_ref = hud_normalize_identifier(role_key)
            if isinstance(goals, list) and goals:
                g0 = goals[0]
                if isinstance(g0, dict):
                    goal_ref = goal_ref or hud_normalize_identifier(
                        g0.get("goal") or g0.get("goal_ref")
                    )
                elif isinstance(g0, str):
                    goal_ref = goal_ref or hud_normalize_identifier(g0)
            if role_ref and goal_ref:
                break
    return {"role_ref": role_ref, "goal_ref": goal_ref}


def _apply_push_policy_from_payload(
    store: HUDStore,
    user_id: str,
    payload: Mapping[str, Any],
) -> Optional[web.Response]:
    explicit = hud_parse_explicit_bool(payload.get("external_push_without_approval"))
    if explicit is None:
        return None
    try:
        store.set_user_push_policy(user_id, external_push=explicit)
        if explicit:
            store.set_user_projection_mode(user_id, HUD_PROJECTION_MODE_LIVE)
            store.set_user_onboarding_state(user_id, requires_approval=False)
        else:
            store.set_user_projection_mode(user_id, HUD_PROJECTION_MODE_DRY_RUN)
    except ValueError as exc:
        return web.json_response(
            hud_error_payload(
                str(exc),
                "validation_error",
                "invalid_payload",
                route=HUD_ROUTE_ONBOARDING_SOUL,
                actor=None,
            ),
            status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
        )
    except Exception as exc:
        logger.exception("HUD push policy update failed user_id=%s", user_id)
        return web.json_response(
            hud_error_payload(
                "HUD store operation failed",
                "internal_error",
                "internal_error",
                route=HUD_ROUTE_ONBOARDING_SOUL,
                actor=None,
            ),
            status=500,
        )
    return None


def hud_coerce_soul_md_write_markdown(payload: Mapping[str, Any]) -> str:
    for key in ("markdown", "content", "body", "text"):
        val = payload.get(key)
        if isinstance(val, str) and val.strip():
            return val
    raise ValueError(
        "soul.md write requires a non-empty string in one of: markdown, content, body, text"
    )


def hud_write_soul_md_atomic(markdown: str) -> Path:
    raw = markdown.encode("utf-8")
    if len(raw) > HUD_SOUL_MD_WRITE_MAX_BYTES:
        raise ValueError(
            f"soul.md body exceeds maximum size ({HUD_SOUL_MD_WRITE_MAX_BYTES} bytes)"
        )
    path = hud_soul_md_path().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp.write_bytes(raw)
    try:
        os.replace(tmp, path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except TypeError:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise
    return path


def hud_extract_first_role_goal_from_soul(content: str) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {
        "role_ref": None,
        "role_description": None,
        "goal_ref": None,
    }
    if not content or not isinstance(content, str):
        return result

    def part_pattern(n: int) -> re.Pattern[str]:
        return re.compile(rf"(?ms)^\s*##\s*Part\s+{n}\b.*?(?=^\s*##\s*Part\s+\d+\b|\Z)")

    separator_pattern = re.compile(r"^\|\s*:?-{3,}\s*(\|\s*:?-{3,}\s*)+\|?$")
    part12_match = part_pattern(12).search(content)
    role_name: Optional[str] = None
    if part12_match:
        table_rows = [
            line.strip()
            for line in part12_match.group(0).splitlines()
            if line.strip().startswith("|")
        ]
        sep_index = None
        for idx, row in enumerate(table_rows):
            if separator_pattern.match(row):
                sep_index = idx
                break
        if sep_index is not None:
            for row in table_rows[sep_index + 1 :]:
                if separator_pattern.match(row):
                    continue
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                if len(cells) >= 1 and any(c.strip() for c in cells):
                    result["role_ref"] = cells[0] or None
                    if len(cells) >= 2:
                        role_name = cells[1] or None
                    if len(cells) >= 3:
                        result["role_description"] = cells[2] or None
                    break

    part13_match = part_pattern(13).search(content)
    if part13_match:
        table_rows = [
            line.strip()
            for line in part13_match.group(0).splitlines()
            if line.strip().startswith("|")
        ]
        sep_index = None
        for idx, row in enumerate(table_rows):
            if separator_pattern.match(row):
                sep_index = idx
                break
        goal_ref: Optional[str] = None
        if sep_index is not None:
            for row in table_rows[sep_index + 1 :]:
                if separator_pattern.match(row):
                    continue
                cells = [cell.strip() for cell in row.strip("|").split("|")]
                if len(cells) >= 2 and any(c.strip() for c in cells):
                    row_role = cells[0] or ""
                    row_goal = cells[1] or None
                    if role_name and row_role.lower() == role_name.lower():
                        goal_ref = row_goal
                        break
                    if goal_ref is None:
                        goal_ref = row_goal
        result["goal_ref"] = goal_ref
    return result


def hud_soul_md_extract_part_section(content: str, part_number: int) -> Optional[str]:
    part_pattern = re.compile(
        rf"(?ms)^\s*##\s*Part\s+{part_number}\b.*?(?=^\s*##\s*Part\s+\d+\b|\Z)"
    )
    part_match = part_pattern.search(content)
    return part_match.group(0) if part_match else None


def hud_soul_md_parse_markdown_table(section_text: str) -> List[List[str]]:
    if not section_text:
        return []
    table_rows = [
        line.strip() for line in section_text.splitlines() if line.strip().startswith("|")
    ]
    if not table_rows:
        return []
    separator_pattern = re.compile(r"^\|\s*:?-{3,}\s*(\|\s*:?-{3,}\s*)+\|?$")
    rows: List[List[str]] = []
    for row in table_rows:
        if separator_pattern.match(row):
            continue
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if any(c.strip() for c in cells):
            rows.append(cells)
    return rows


def hud_soul_md_extract_roles_and_goals(content: str) -> Dict[str, Any]:
    roles: List[Dict[str, str]] = []
    goals_by_role: Dict[str, List[Dict[str, str]]] = {}
    role_name_to_slug: Dict[str, str] = {}

    part12 = hud_soul_md_extract_part_section(content, 12)
    if part12:
        for row in hud_soul_md_parse_markdown_table(part12):
            if len(row) >= 3 and row[0] and not row[0].lower().startswith("role"):
                slug = row[0].strip()
                name = row[1].strip()
                desc = row[2].strip() if len(row) > 2 else ""
                if slug and name:
                    roles.append({"slug": slug, "name": name, "description": desc})
                    role_name_to_slug[name] = slug
                    role_name_to_slug[slug] = slug

    part13 = hud_soul_md_extract_part_section(content, 13)
    if part13:
        for row in hud_soul_md_parse_markdown_table(part13):
            if len(row) >= 2 and row[0] and not row[0].lower().startswith("role"):
                role_key = row[0].strip()
                goal = row[1].strip()
                done = row[2].strip() if len(row) > 2 else ""
                if role_key and goal:
                    if role_key not in goals_by_role:
                        goals_by_role[role_key] = []
                    goals_by_role[role_key].append(
                        {"goal": goal, "done_definition": done}
                    )
                    if role_key in role_name_to_slug:
                        sl = role_name_to_slug[role_key]
                        if sl != role_key and sl not in goals_by_role:
                            goals_by_role[sl] = goals_by_role[role_key][:]

    return {
        "roles": roles,
        "goals_by_role": goals_by_role,
        "role_name_to_slug": {k: v for k, v in role_name_to_slug.items() if k != v},
    }


def hud_onboarding_soul_read_response(
    *,
    store: HUDStore,
    actor: Optional[str],
    route: str,
    route_meta: Optional[Dict[str, Any]] = None,
    request: Optional[web.Request] = None,
) -> web.Response:
    worksheet_path = hud_soul_md_worksheet_read_path()
    canonical_path = hud_soul_md_path().expanduser().resolve()
    try:
        raw = worksheet_path.read_bytes()
        exists = True
    except FileNotFoundError:
        raw = b""
        exists = False
    except OSError as exc:
        return web.json_response(
            hud_error_payload(
                f"Failed to read soul.md: {exc}",
                "internal_error",
                "internal_error",
                route=route,
                actor=actor,
            ),
            status=500,
        )
    if len(raw) > HUD_SOUL_MD_WRITE_MAX_BYTES:
        return web.json_response(
            hud_error_payload(
                f"soul.md exceeds maximum size ({HUD_SOUL_MD_WRITE_MAX_BYTES} bytes)",
                "validation_error",
                "invalid_payload",
                route=route,
                actor=actor,
            ),
            status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
        )
    try:
        markdown = raw.decode("utf-8") if raw else ""
    except UnicodeDecodeError:
        return web.json_response(
            hud_error_payload(
                "soul.md is not valid UTF-8",
                "validation_error",
                "invalid_payload",
                route=route,
                actor=actor,
            ),
            status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
        )
    ctx = hud_onboarding_context_status()
    read_source = "worksheet" if worksheet_path != canonical_path else "canonical"
    canon_gate_issues: List[str] = []
    try:
        if canonical_path.is_file():
            canon_gate_issues = hud_soul_md_gate_issues(
                canonical_path.read_text(encoding="utf-8")
            )
    except (OSError, UnicodeDecodeError):
        canon_gate_issues = []
    onboarding_required = bool(ctx.get("required"))
    data: Dict[str, Any] = {
        "path": str(worksheet_path),
        "canonical_path": str(canonical_path),
        "read_source": read_source,
        "exists": exists,
        "byte_length": len(raw),
        "markdown": markdown,
        "onboarding_needed": onboarding_required,
        "onboarding_complete": not onboarding_required,
        "onboarding_gate_issues": canon_gate_issues,
        "onboarding_needed_reason": ctx,
    }
    uid = hud_resolve_user_id(request) if request is not None else "localuser"
    if request is not None and not onboarding_required:
        data.update(hud_push_policy_client_fields(store, uid))
    data = finalize_hud_data(data, store=store, user_id=uid)
    return web.json_response(
        hud_success_payload(
            route,
            status="ok",
            actor=actor,
            route_meta=route_meta,
            data=data,
        ),
        status=200,
    )


def hud_onboarding_soul_build_response(
    *,
    store: HUDStore,
    actor: Optional[str],
    payload: Mapping[str, Any],
    route: str,
    route_meta: Optional[Dict[str, Any]] = None,
    request: Optional[web.Request] = None,
) -> web.Response:
    try:
        markdown = hud_coerce_soul_md_write_markdown(payload)
        written_path = hud_write_soul_md_atomic(markdown)
    except ValueError as exc:
        return web.json_response(
            hud_error_payload(
                str(exc),
                "validation_error",
                "invalid_payload",
                route=route,
                actor=actor,
            ),
            status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
        )
    except OSError as exc:
        return web.json_response(
            hud_error_payload(
                f"Failed to write soul.md: {exc}",
                "internal_error",
                "internal_error",
                route=route,
                actor=actor,
            ),
            status=500,
        )
    ctx = hud_onboarding_context_status()
    try:
        written_body = written_path.read_text(encoding="utf-8")
    except OSError:
        written_body = ""
    gate_issues = hud_soul_md_gate_issues(written_body)

    if request is not None:
        uid = hud_resolve_user_id(request, payload=payload)
        push_error = _apply_push_policy_from_payload(store, uid, payload)
        if push_error is not None:
            return push_error

    uid = hud_resolve_user_id(request, payload=payload) if request is not None else "localuser"
    has_atomic = hud_onboarding_params_has_atomic(payload)
    if not has_atomic:
        data = finalize_hud_data(
            {
                "atomic_format": {
                    "roles": [{"slug": "...", "name": "...", "description": "..."}],
                    "goals_by_role": {
                        "role_slug": [{"goal": "...", "done_definition": "..."}]
                    },
                },
                "soul_markdown": written_body,
                "onboarding_gate_issues": gate_issues,
                "next_action": "provide_atomic_rewrite",
            },
            store=store,
            user_id=uid,
            strict_ritual=True,
        )
        return web.json_response(
            hud_success_payload(
                route, status="ritual_controlled", actor=actor, route_meta=route_meta, data=data
            ),
            status=200,
        )

    populated_from_agent = False
    if not gate_issues and request is not None:
        uid = hud_resolve_user_id(request, payload=payload)
        atomic_refs = hud_onboarding_parse_atomic_refs(payload)
        role_ref = atomic_refs.get("role_ref")
        goal_ref = atomic_refs.get("goal_ref")
        if not role_ref or not goal_ref:
            extracted = hud_extract_first_role_goal_from_soul(written_body)
            role_ref = role_ref or extracted.get("role_ref")
            goal_ref = goal_ref or extracted.get("goal_ref")
        if uid and role_ref:
            try:
                store.set_user_onboarding_state(
                    uid,
                    role_ref=role_ref,
                    goal_ref=goal_ref,
                    requires_approval=hud_extract_default_requires_approval(payload),
                )
                populated_from_agent = bool(
                    atomic_refs.get("role_ref") and atomic_refs.get("goal_ref")
                )
            except Exception:
                logger.exception("HUD failure populating onboarding state DB after soul write")

    ctx = hud_onboarding_context_status()
    onboarding_required = bool(ctx.get("required"))

    data: Dict[str, Any] = {
        "path": str(written_path),
        "onboarding_needed": onboarding_required,
        "onboarding_complete": not onboarding_required,
        "onboarding_gate_issues": gate_issues,
        "onboarding_needed_reason": ctx,
    }
    if populated_from_agent:
        data["populated_from_agent"] = True
        if not gate_issues and not onboarding_required:
            data["next_action"] = "verify_with_brief"
    if request is not None and not onboarding_required:
        data.update(hud_push_policy_client_fields(store, uid))
    data = finalize_hud_data(data, store=store, user_id=uid, strict_ritual=False)
    return web.json_response(
        hud_success_payload(route, status="ok", actor=actor, route_meta=route_meta, data=data),
        status=200,
    )


def hud_onboarding_dispatch_response(
    *,
    store: HUDStore,
    actor: Optional[str],
    payload: Mapping[str, Any],
    route: str,
    route_meta: Optional[Dict[str, Any]] = None,
    request: Optional[web.Request] = None,
) -> web.Response:
    if hud_onboarding_params_is_push_only(payload):
        if request is None:
            return web.json_response(
                hud_error_payload(
                    "external_push_without_approval requires a resolved user_id",
                    "validation_error",
                    "invalid_payload",
                    route=route,
                    actor=actor,
                ),
                status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
            )
        uid = hud_resolve_user_id(request, payload=payload)
        explicit = hud_parse_explicit_bool(payload.get("external_push_without_approval"))
        if explicit is None:
            return web.json_response(
                hud_error_payload(
                    "external_push_without_approval must be an explicit boolean",
                    "validation_error",
                    "invalid_payload",
                    route=route,
                    actor=actor,
                ),
                status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
            )
        push_error = _apply_push_policy_from_payload(store, uid, payload)
        if push_error is not None:
            return push_error
        push_data = finalize_hud_data(
            {"external_push_without_approval": explicit},
            store=store,
            user_id=uid,
        )
        return web.json_response(
            hud_success_payload(
                route,
                status="ok",
                actor=actor,
                route_meta=route_meta,
                data=push_data,
            ),
            status=200,
        )
    if hud_onboarding_params_has_markdown(payload):
        return hud_onboarding_soul_build_response(
            store=store,
            actor=actor,
            payload=payload,
            route=route,
            route_meta=route_meta,
            request=request,
        )
    return hud_onboarding_soul_read_response(
        store=store,
        actor=actor,
        route=route,
        route_meta=route_meta,
        request=request,
    )


async def handle_onboarding_soul_read(request: web.Request) -> web.Response:
    from hud.gates import require_hud_admin

    actor = hud_actor(request)
    admin_error = await require_hud_admin(request)
    if admin_error is not None:
        return admin_error
    store: HUDStore = request.app["hud_store"]
    return hud_onboarding_soul_read_response(
        store=store,
        actor=actor,
        route=HUD_ROUTE_ONBOARDING_SOUL,
        request=request,
    )


async def handle_onboarding_soul_write(request: web.Request) -> web.Response:
    from hud.contracts import require_json
    from hud.gates import require_hud_admin

    actor = hud_actor(request)
    admin_error = await require_hud_admin(request)
    if admin_error is not None:
        return admin_error
    try:
        payload = require_json(await request.text())
    except ValueError as exc:
        return web.json_response(
            hud_error_payload(
                str(exc),
                "validation_error",
                "invalid_payload",
                route=HUD_ROUTE_ONBOARDING_SOUL,
                actor=actor,
            ),
            status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
        )
    store: HUDStore = request.app["hud_store"]
    return hud_onboarding_soul_build_response(
        store=store,
        actor=actor,
        payload=payload,
        route=HUD_ROUTE_ONBOARDING_SOUL,
        request=request,
    )
