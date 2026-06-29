"""MCP meta-prompting: efficiency and strict ritual modes (HUD v1.4.1)."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from hud.onboarding_db import is_user_fully_onboarded
from hud.store import HUDStore

MCP_EFFICIENCY_INSTRUCTION = (
    "You are inside the MCP. Respond fast with minimal reasoning. Follow the exact tool contract and output only the required fields. Classify to the best of your ability first. Only ask the user a short, targeted question if you still have a missing required field or received an error after classification. Do not add extra explanation or conversation."
)

MCP_STRICT_RITUAL_SUFFIX = (
    "[MCP RITUAL MODE - STRICT PROCEDURE] Do not spend long thinking turns. "
    "Think fast. Be concise. No step-by-step. Structure all roles from Roles Matrix and "
    "goals from Goals Matrix in the soul_markdown below and re-call hud.onboarding with "
    "atomic fields. Output only the tool call."
)

MCP_BRIEF_DATE_INSTRUCTION = (
    "DATE RESOLUTION: This brief contains a 'dates' object with authoritative server anchors "
    "(today, next_monday, next_friday, end_of_this_week, etc.). Resolve any relative day phrase "
    "the user utters ('friday', 'this friday', 'next friday', 'end of week', etc.) strictly against "
    "the values in 'dates'. When the item is a todo or event, always emit the resolved ISO date in the 'due' field of the payload."
)

MCP_ONBOARDING_MODE = (
    "[MCP ONBOARDING MODE]\n"
    "You are in ONBOARDING mode. All action tools are blocked until onboarding is complete.\n\n"
    "Follow this exact decomposed sequence using ONLY the 4 narrow methods (hud.onboarding.read, hud.onboarding.write_soul, hud.onboarding.set_atomic, hud.onboarding.set_push — NO combined hud.onboarding calls, no rituals, no legacy shims for agent-driven onboarding):\n"
    "1. Call hud.brief (default, no scope) to load current state including dates, decision_matrix, mcp_meta, onboarding_state.\n"
    "2. Call hud.onboarding.read to get the rich outputs: markdown, current_atomic, atomic_template, atomic_schema, validation_status, onboarding_needed, soul_gate_issues, mcp_meta, etc.\n"
    "3. Survey the user faithful to the template + atomic_template. Use short targeted questions ONLY if still missing a required field or error after best-effort classification. Collect actual soul content the user provides. Never fabricate required fields.\n"
    "4. When profile is complete (validation_status.status == \"complete\" with zero issues in validation_status.issues and no relevant soul_gate_issues):\n"
    "   - If the push preference (external_push_without_approval) is not yet known, ask the user ONCE with a single short targeted question, then immediately begin the persistence sequence.\n"
    "   - Execute the three persistence calls (`write_soul` → `set_atomic` → `set_push`) as a TIGHT, LOW-LATENCY INTERNAL SEQUENCE with **NO user conversation, no extra messages, and no intervening user turns** between them. Output ONLY the next narrow tool call after each success response (the responses themselves carry updated mcp_meta).\n"
    "     a. hud.onboarding.write_soul with the full polished soul.md markdown.\n"
    "     b. hud.onboarding.set_atomic with the structured payload {roles: [...], goals_by_role: {...}} (refined directly from the atomic_template seen in the prior read + user's actual words; always cross-validate against the atomic_schema + validation_status from the read response; never invent primaries).\n"
    "     c. hud.onboarding.set_push with { \"external_push_without_approval\": boolean }.\n"
    "   The agent MUST treat the three calls as back-to-back internal steps (low roundtrip cost, minimal surface for interpretation error). This is the canonical close for reliable onboarding.\n"
    "   You may emit the three in one turn; backend reconciles; final hud.brief verifies.\n"
    "5. Verify completion by calling hud.brief — it must return onboarding_state: \"fully_onboarded\" (at which point mcp_meta switches to operational mode).\n\n"
    "Output ONLY the required tool call. No extra text or reasoning except a short targeted user question when the error guidance explicitly requires it.\n"
    "409 responses contain rich [ERROR CONTEXT] + guidance. JSON bodies contain atomic_template, atomic_schema, validation_status, mcp_meta, dates, decision_matrix etc. Use them to select the exact next narrow method. The agent succeeds by direct, low-entropy interpretation of these under the named mode."
)


MCP_INGEST_FIELD_CONTRACT = (
    "INGEST FIELD CONTRACT:\n"
    "- semantic_type = WHAT (event|todo|note); google_target = WHERE (obsidian|calendar|tasks).\n"
    "- google_target calendar (events): MUST send start AND end on hud.ingest using CalendarEventTime shape "
    "(object with dateTime+timeZone for timed events, or date for all-day). Resolve relative days using dates from hud.brief.\n"
    "- google_target tasks: send due (YYYY-MM-DD) when the todo has a schedule.\n"
    "- Google write success: inspect adapter_projection.external_dispatch (not adapter_projection alone).\n"
    "- requires_approval true queues even when push-without-approval is on."
)

MCP_OPERATIONAL_MODE = (
    "[MCP OPERATIONAL MODE]\n"
    "You are in normal operational mode inside the MCP.\n\n"
    "Rules while in this mode:\n"
    "- Respond fast with minimal reasoning.\n"
    "- Follow the exact tool contract and output only the required fields.\n"
    "- Classify to the best of your ability first.\n"
    "- Only ask the user a short, targeted question if you still have a missing required field or received an error after best-effort classification.\n"
    "- Do not add extra explanation or conversation.\n\n"
    "409 responses will tell you exactly what is missing. Use them to drive the next correct call."
)


def build_mcp_meta(*, strict_ritual: bool) -> Dict[str, str]:
    if strict_ritual:
        return {
            "mode": "strict_ritual",
            "instruction": f"{MCP_EFFICIENCY_INSTRUCTION}\n\n{MCP_STRICT_RITUAL_SUFFIX}",
        }
    return {"mode": "efficiency", "instruction": MCP_EFFICIENCY_INSTRUCTION}


def build_brief_date_meta(
    *, store: Optional[HUDStore] = None, user_id: Optional[str] = None, strict_ritual: bool = False
) -> Dict[str, str]:
    """MCP meta-prompting for hud.brief classification turns.

    Starts from the authoritative current named mode (ONBOARDING or OPERATIONAL)
    and appends the DATE RESOLUTION rule. Never emits the bare legacy efficiency
    string when a named mode should be the contract.
    """
    if strict_ritual or store is None or user_id is None:
        base = build_mcp_meta(strict_ritual=strict_ritual)
        base["mode"] = "brief_classification" if not strict_ritual else "strict_ritual"
    else:
        base = get_current_mcp_mode_meta(store, user_id)
        base = dict(base)  # copy
        base["mode"] = "brief_classification"

    date_rule = MCP_BRIEF_DATE_INSTRUCTION
    field_rule = MCP_INGEST_FIELD_CONTRACT
    base["instruction"] = f"{base.get('instruction', '')}\n\n{date_rule}\n\n{field_rule}".strip()
    return base


def build_onboarding_enforcement_meta() -> Dict[str, str]:
    """Strong enforcement meta for 409s while the user is not fully onboarded."""
    return {
        "mode": "onboarding",
        "instruction": MCP_ONBOARDING_MODE,
    }


def build_operational_meta() -> Dict[str, str]:
    """Meta attached once the user is fully onboarded to signal normal operation."""
    return {
        "mode": "operational",
        "instruction": MCP_OPERATIONAL_MODE,
    }


def get_current_mcp_mode_meta(store: HUDStore, user_id: str) -> Dict[str, str]:
    """Single source of truth for the current MCP behavioral contract.

    Every attachment point and every error 409 should go through this (or
    build_rich_error_meta which uses it) so the agent always sees the correct
    named mode tag with no leakage of the old efficiency string.
    """
    try:
        if is_user_fully_onboarded(store, user_id):
            return build_operational_meta()
    except Exception:
        pass
    return build_onboarding_enforcement_meta()


def build_rich_error_meta(
    *,
    current_mode: str,
    tool: str,
    violation: str,
    guidance: str,
) -> Dict[str, str]:
    """Rich diagnostic meta for 409 / validation error responses.

    The returned instruction is the authoritative mode contract + a precise
    [ERROR CONTEXT] block telling the agent:
      - which tool contract was violated
      - exactly what was missing / insufficient after classification
      - whether a short targeted user question is appropriate

    This is the mechanism that makes downstream ingest/project 409s useful
    instead of opaque "you are still onboarding".
    """
    if current_mode in ("onboarding", "onboarding_enforcement", "strict_ritual"):
        base = MCP_ONBOARDING_MODE
        mode_key = "onboarding"
    else:
        base = MCP_OPERATIONAL_MODE
        mode_key = "operational"

    diagnostic = (
        "\n\n[ERROR CONTEXT]\n"
        f"tool: {tool}\n"
        f"violation: {violation}\n"
        f"guidance: {guidance}\n"
        "Resolve the violation using the minimal correct tool call. "
        "Only ask the user a short targeted question when the guidance explicitly says to."
    )
    return {
        "mode": mode_key,
        "instruction": base + diagnostic,
    }


def should_attach_efficiency(store: HUDStore, user_id: str) -> bool:
    try:
        return not is_user_fully_onboarded(store, user_id)
    except Exception:
        return True


def attach_mcp_meta(
    data: Mapping[str, Any],
    *,
    store: HUDStore,
    user_id: str,
    strict_ritual: bool,
) -> Dict[str, Any]:
    out = dict(data)
    if strict_ritual:
        out["mcp_meta"] = build_mcp_meta(strict_ritual=True)
        return out
    # Always use the single source of truth for named mode
    out["mcp_meta"] = get_current_mcp_mode_meta(store, user_id)
    return out




def build_ingest_mcp_meta(
    store: HUDStore,
    user_id: str,
    *,
    google_target: Optional[str] = None,
    semantic_type: Optional[str] = None,
) -> Dict[str, str]:
    """MCP meta for hud.ingest responses — reinforces OpenAPI field contract."""
    base = get_current_mcp_mode_meta(store, user_id)
    base = dict(base)
    base["mode"] = "ingest"
    gt = str(google_target or "").strip().lower()
    st = str(semantic_type or "").strip().lower()
    extra = MCP_INGEST_FIELD_CONTRACT
    if gt == "calendar" or st == "event":
        extra += (
            "\nCALENDAR: You MUST include start and end on this ingest call. "
            "Example timed: start/end objects with dateTime and timeZone."
        )
    elif gt in ("tasks", "gtasks") or st == "todo":
        extra += "\nTASKS: Include due (YYYY-MM-DD) when the user gave a date."
    base["instruction"] = f"{base.get('instruction', '')}\n\n{extra}".strip()
    return base


def finalize_hud_data(
    data: Mapping[str, Any],
    *,
    store: HUDStore,
    user_id: str,
    strict_ritual: bool = False,
) -> Dict[str, Any]:
    result = attach_mcp_meta(data, store=store, user_id=user_id, strict_ritual=strict_ritual)
    result.pop("mcp_mode", None)
    result.pop("instruction", None)
    return result
