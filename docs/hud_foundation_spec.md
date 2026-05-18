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
   The agent calls `hud.brief` with no parameters.  
   The MCP returns your current atomic model:
   - Your Roles
   - Your Goals by Role
   - Decision matrix / priority guidance

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
   Using `hud.project` with an explicit `action` (`project` | `approve` | `reject`).

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
| `hud.project`         | Reviews/decides fate (`project` / `approve` / `reject`) and projects    | For review or explicit approval |
| `hud.onboarding`      | Reads or writes soul.md with atomic roles & goals                       | Initial setup and later edits |
| `hud.mcp`             | JSON-RPC entry point for all tools                                      | Primary surface for agents |

---

## 7. Projection Philosophy (Obsidian First)

- **Source of Truth**: Obsidian vault (`data/obsidian/AdventedHUD/`)
- **Default Mode**: `dry_run`
- **Live Projection**: Only on explicit approval + user preference

---

## 8. Meta-Prompting & Efficiency

The MCP uses explicit meta-prompting to keep the agent in a high-efficiency, low-reasoning state while interacting with the system, especially under OWUI’s sequential tool-calling constraints.

---

## 9. Onboarding & the Soul Template

The HUD provides a clean `soul-template.md`. During onboarding the agent helps the user copy it to create their own unique `soul.md`. The system must never overwrite an existing `soul.md`.

---

## 10. Data Ownership & Independence

All personal context belongs in Obsidian. The standalone AdventedHUD project is the authoritative implementation. The older monolith proxy is legacy compatibility only.

---

*This document reflects the agent-driven, Obsidian-first model with a deterministic priority matrix for classification.*
