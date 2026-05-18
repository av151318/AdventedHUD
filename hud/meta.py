"""MCP meta-prompting: efficiency and strict ritual modes (HUD v1.4.1)."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from hud.store import HUDStore

MCP_EFFICIENCY_INSTRUCTION = (
    "You are inside the MCP — think fast, use minimal reasoning, follow the defined format."
)

MCP_STRICT_RITUAL_SUFFIX = (
    "[MCP RITUAL MODE - STRICT PROCEDURE] Do not spend long thinking turns. "
    "Think fast. Be concise. No step-by-step. Structure all roles from Part 12 and "
    "goals from Part 13 in the soul_markdown below and re-call hud.onboarding with "
    "atomic fields. Output only the tool call."
)


def build_mcp_meta(*, strict_ritual: bool) -> Dict[str, str]:
    if strict_ritual:
        return {
            "mode": "strict_ritual",
            "instruction": f"{MCP_EFFICIENCY_INSTRUCTION}\n\n{MCP_STRICT_RITUAL_SUFFIX}",
        }
    return {"mode": "efficiency", "instruction": MCP_EFFICIENCY_INSTRUCTION}


def should_attach_efficiency(store: HUDStore, user_id: str) -> bool:
    try:
        state = store.get_user_onboarding_state(user_id)
    except Exception:
        return True
    if not state:
        return True
    role_ref = state.get("role_ref")
    goal_ref = state.get("goal_ref")
    if not role_ref or not goal_ref:
        return True
    return False


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
    if should_attach_efficiency(store, user_id):
        out["mcp_meta"] = build_mcp_meta(strict_ritual=False)
    return out


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
