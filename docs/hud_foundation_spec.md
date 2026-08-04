# HUD Foundation Specification

**Version:** 1.4.1 (Agent-Driven Personal Planner)  
**Status:** Authoritative for the standalone AdventedHUD project  
**Last Updated:** 2026-05-17

---

## 1. Intent and Philosophy

HUD exists to turn a language model into a **personal planner agent** that helps you live according to your own mission and roles.

The **source of truth** is local — inside your Obsidian vault. Roles, goals, personal notes, and context live in Obsidian first. Projection to Google Calendar or Google Tasks is always a secondary, optional action.

The MCP is deliberately designed to support two natural usage patterns:

1. **Queue + Review Flow** (recommended default for most people)  
   You dump context (meetings, todos, thoughts, events, etc.). These items are captured into Obsidian and queued for review. Later, when you ask for a briefing or planning session, the agent surfaces what’s pending and you decide — in bulk or individually — what to keep, what to project, and what to discard.

2. **Direct Push Flow**  
   When you have “push without approval” enabled, you can quickly push a todo or event directly to Google. However, you should always retain the ability to explicitly say “queue this” or “add for review” even when that mode is on.

This design gives you **natural language control** over how much friction and review you want on any given day.

---

## 2. Canonical Atomic Data Model

HUD works with a small set of first-class atomic entities:

| Entity     | Description                                      | Primary Home          | Can Project To          |
|------------|--------------------------------------------------|-----------------------|-------------------------|
| Role       | A recurring area of responsibility or identity   | Obsidian (soul.md)    | —                       |
| Goal       | Long-term aspiration tied to a Role              | Obsidian (soul.md)    | —                       |
| Event      | Time-bound occurrence                            | Obsidian              | Google Calendar         |
| Todo       | Actionable item (with optional due date)         | Obsidian              | Google Tasks            |
| Note       | Unstructured capture or reflection               | Obsidian              | —                       |

These are the only entities the agent and MCP are allowed to create, classify, or project.

---

## 3. Architecture — Agent as Personal Planner

The HUD MCP is **not** an orchestrator. It is a **context and projection service** that enables a model to act as your personal planner.

### Primary Flow (Agent-Driven)

1. **Context Capture**  
   You give the agent natural language (chat, voice note, quick dump).

2. **Classification Context**  
   The agent calls `hud.brief` with no parameters (default mode).  
   The MCP returns a complete classification payload:
   - `mission` (from **My Mission Statement**, Vision fallback if empty)
   - `roles` and `goals_by_role` (from soul.md Roles Matrix / Goals Matrix tables)
   - `decision_matrix` (full FranklinCovey Q1–Q4 structure)
   - `decision_matrix_guidance` (how to apply the matrix)
   - `onboarding_state` (`incomplete` | `awaiting_push_policy` | `fully_onboarded`)
   - `push_policy`
   - `data.mcp_meta` while onboarding is incomplete (see §8)

3. **Agent Classification**  
   The agent uses this context to classify your input into one or more atomic items, deciding:
   - `role_ref`
   - `goal_ref` (optional)
   - `priority_class`
   - `semantic_type` (event / todo / note)
   - `google_target` (obsidian | calendar | tasks)

4. **Ingestion / Projection**  
   The agent calls `hud.ingest` or `hud.project` with the fully classified payload.

5. **Fate Decision** (optional)  
   Using `hud.project` with an explicit `action` (`project` | `approve` | `reject` | `retract` | `update`).

6. **Persistence & Projection**  
   Everything is first written to Obsidian. Projection to Google only happens when the item is approved and projection mode allows it.

**Core Rule**: The agent classifies. The MCP persists and projects.

---

## 4. Priority / Decision Matrix (FranklinCovey Time Management Matrix)

This MCP uses the classic **FranklinCovey Time Management Matrix** (also known as the Covey Quadrant or adapted Eisenhower Matrix) as the deterministic logic for priority classification.

The matrix classifies tasks using two axes:

- **Importance**: Tasks that contribute to the user’s long-term goals, values, roles, and personal mission (derived from the user’s provided atomic context: Mission, Roles, Goals).
- **Urgency**: Tasks that demand immediate attention (deadlines, crises, pressing problems).

### The Four Quadrants

| Quadrant | Name                        | Urgency | Importance | Definition & Characteristics                                      | Agent Action / Classification Guidance |
|----------|-----------------------------|---------|------------|-------------------------------------------------------------------|----------------------------------------|
| **Q1**   | Crisis / Important & Urgent | High    | High       | Both critical to goals/roles **and** time-sensitive. Crises, deadlines, emergencies that align with core roles or goals. | **Do immediately** (or as soon as possible). Highest priority. |
| **Q2**   | Quality / Important but Not Urgent | Low | High     | Significantly advance long-term goals, roles, relationships, health, planning, prevention, and mission. Highest-leverage activities with no external pressure. | **Schedule and protect** dedicated time. Primary focus of planning effort. |
| **Q3**   | Deception / Not Important but Urgent | High | Low      | Feel urgent (interruptions, some emails, requests) but do **not** advance the user’s own roles/goals/mission. Often other people’s priorities. | **Delegate, minimize, or batch**. Question whether it truly belongs to the user. |
| **Q4**   | Waste / Not Important & Not Urgent | Low | Low      | Trivial or time-wasting activities that neither advance goals nor have deadlines (mindless scrolling, excessive distractions, busywork). | **Eliminate or strictly limit**. |

### Key Decision Rules

1. **Importance is contextual** — Determined by the user’s passed-in personal context (Mission, Roles, Goals, values). A task is Important only if it clearly supports one or more defined roles or moves the user toward their stated goals.

2. **Urgency is mostly objective** — Clear deadline within the planning horizon, crisis/breakdown that must be fixed now, or external pressure with real negative consequences if delayed.

3. **Classification Flow**:
   - First determine Importance (does this advance roles or goals?).
   - Then determine Urgency (does it require immediate action or have a tight deadline?).
   - Default bias: Spend more time in **Q2** and reduce Q1 over time through better planning.

When `hud.brief` (default) is called, the MCP returns the user’s Roles, Goals, and this Decision Matrix so the agent has a complete, deterministic payload for classification.

---

## 5. Primary Usage Patterns

### Pattern A — Queue + Review (Most Common)
- You dump lots of context throughout the day/week.
- Items are captured into Obsidian and queued.
- When you’re ready, you ask for a briefing or planning session.
- The agent shows you what’s pending.
- You review, decide what to keep, what to project, and what to drop.

### Pattern B — Direct Push
- “Push without approval” is enabled.
- Quick direct projection is possible, but you can always explicitly say “queue this” or “add for review”.

---

## 6. Core Tools and Their Intent

| Tool                  | Purpose                                                                 | When Agent Uses It |
|-----------------------|-------------------------------------------------------------------------|--------------------|
| `hud.brief` (default) | Returns roles, goals, and decision matrix for classification            | Before every classification |
| `hud.brief` (scoped)  | Returns human-readable briefing for a time period or goal               | When user asks for a plan or review |
| `hud.ingest`          | Accepts classified item and persists (with optional projection)         | After classification |
| `hud.project`         | Reviews/decides fate (`project` / `approve` / `reject` / `retract` / `update`) and projects    | For review, explicit approval, or correcting/removing a projected item |
| `hud.onboarding`      | Reads or writes soul.md; persists atomic state to DB on write           | Initial setup, profile edits |
| `hud.mcp`             | JSON-RPC entry point for all tools                                      | Primary surface for agents |

### Post-projection fate (retract / update)

Projection **permission** plus **edit** means the agent can also scrap or correct an
item *after* it has been projected live. `hud.project` therefore supports two
additional actions on already-projected (`approved` / `synced`) items:

- **`retract`** — deletes the live projection: `DELETE` the Google Calendar event
  (`gcal.delete`) or Google Task (`gtasks.delete`) using the stored external id,
  and archives/removes the Obsidian note (`obsidian.delete`) using the stored path.
  The item then moves to the terminal `retracted` status. A Google 404
  (already gone) is treated as success; a failed external delete is reported as an
  error and the item is **not** marked retracted.
- **`update`** — patches the projected item in place: the Google object is `PATCH`ed
  (never re-created, so no duplicates) using the stored external id, and the
  Obsidian note is overwritten. The item stays `approved` / `synced`.

Durable identity is written back after every successful live projection:
`google_id` / `external_id`, `calendar_id` / `tasklist_id`, and the Obsidian
`relative_path`. These are what make later `retract` / `update` able to target the
exact external object. `reject` on an already-projected item returns a clear
`use_retract` error instead of a silent noop — `reject` only gates the queue for
non-projected items.

Correction playbook:
- Wrong date / wrong content on a live item → `retract` then re-`ingest`, **or**
  `update` when the same external object should be corrected in place.
- Never "reject then re-ingest" alone for a live item — that leaves the stale
  Google/Obsidian artifact behind.

---

## 7. Projection Philosophy (Obsidian First)

- **Source of Truth**: Obsidian vault (`data/obsidian/AdventedHUD/`)
- **Default Mode**: `dry_run`
- **Live Projection**: Only on explicit approval + user preference

---

## 8. Meta-Prompting & Efficiency

The MCP uses explicit meta-prompting to keep the agent in a high-efficiency, low-reasoning state while interacting with the system, especially under OWUI’s sequential tool-calling constraints.

### Response shape

Successful tool responses include agent data under `data`. When meta-prompting applies:

```json
{
  "data": {
    "mcp_meta": {
      "mode": "efficiency",
      "instruction": "You are inside the MCP — think fast, use minimal reasoning, follow the defined format."
    }
  }
}
```

| `mcp_meta.mode` | When | `instruction` contents |
|-----------------|------|-------------------------|
| `efficiency` | User not `fully_onboarded` (most tools) | General efficiency line only |
| `strict_ritual` | Onboarding write missing valid atomic payload | Efficiency line **plus** `[MCP RITUAL MODE - STRICT PROCEDURE]` paragraph |
| _(absent)_ | User `fully_onboarded` and not in strict ritual | Normal operation |

### Rules

1. **No success / completion signals** — There is no `COMPLETE`, `ritual_complete`, or release string. The agent leaves strict mode when `mcp_meta.mode` is not `strict_ritual` on the next successful onboarding step, then verifies via default `hud.brief`.
2. **Dual-purpose responses** — `data` may include both agent-control fields (`mcp_meta`, `next_action`) and user-facing content (`markdown`, items).
3. **Default `hud.brief` is always available** — Even while onboarding is incomplete, so the agent can classify using mission, roles, goals, and the decision matrix (with `efficiency` meta attached).

### Tool coverage

| Tool | Meta while incomplete |
|------|------------------------|
| `hud.brief` (default) | `efficiency` |
| `hud.brief` (scoped) | `efficiency` |
| `hud.ingest`, `hud.project`, `hud.mcp` | `efficiency` (when gated responses include `data`) |
| `hud.onboarding` (read) | `efficiency` |
| `hud.onboarding` (write, no atomic) | `strict_ritual` |
| `hud.onboarding` (profile edit) | none when atomic DB already complete |

---

## 9. Onboarding, Soul Template & DB Gating

### Soul template

`AdventedHUD/docs/soul-template.md` is the user-facing worksheet. Populated **Roles Matrix** (roles table) and **Goals Matrix** (goals table) sections are required for soul file validation during writes.

### DB-only gating (authoritative)

Tool gating uses `hud_user_onboarding_states`, not file presence alone.

| State | Condition |
|-------|-----------|
| **Atomic incomplete** | No row, or missing/invalid `roles_json` + `goals_json`, or failed validation of `roles[]`, `goals_by_role{}`, `primary_role_ref`, `primary_goal_ref` |
| **Awaiting push** | Atomic complete but `HUD_REQUIRE_POST_ONBOARDING_PUSH=1` and no push policy row |
| **Fully onboarded** | Valid atomic payload persisted **and** push policy set (when required) |

`role_ref` and `goal_ref` columns are **derived** from the atomic write, not the definition of completeness.

### Onboarding write paths

1. **Ritual** — Markdown write without atomic fields → `status: ritual_controlled`, `mcp_meta.mode: strict_ritual`.
2. **Atomic complete** — Write with `roles`, `goals_by_role`, `primary_role_ref`, `primary_goal_ref` → persists JSON to DB, `status: ok`, no strict meta.
3. **Profile edit** — After atomic complete, markdown-only write (or `profile_edit: true`) updates soul.md **without** strict ritual. File gate issues may appear as `soul_validation_warnings` only.

### Push policy

Set via `hud.onboarding` with `external_push_without_approval` (alone or bundled with markdown write).

---

## 10. Data Ownership & Independence

All personal context belongs in Obsidian. The standalone AdventedHUD project is the authoritative implementation. The older monolith proxy is legacy compatibility only.

---

*This document reflects the agent-driven, Obsidian-first model with a deterministic priority matrix for classification.*
