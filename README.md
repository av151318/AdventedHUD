# AdventedHUD

**Agent-Driven Personal Planner — MCP Service (v1.4.1)**

AdventedHUD is an MCP service that turns an LLM agent into a **personal planner**. It provides agents with atomic context (your roles, goals, mission, and a FranklinCovey decision matrix) and routes classified items to Obsidian as the source of truth, with optional projection to Google Calendar and Google Tasks.

Exposes HTTP routes and JSON-RPC MCP on port **8200** by default.

---

## Table of Contents

- [Architecture](#architecture)
- [Quick Start](#quick-start)
- [Environment Reference](#environment-reference)
- [MCP Tool Surface](#mcp-tool-surface)
- [HTTP Route Parity](#http-route-parity)
- [Onboarding Ritual](#onboarding-ritual-v141)
- [Agent Harness Compatibility](#agent-harness-compatibility)
- [AdventedOS Context](#adventedos-context)
- [Project Structure](#project-structure)
- [Docs](#docs)
- [Tests](#tests)
- [License](#license)

---

## Architecture

AdventedHUD was built to slot into a **three-layer** architecture, with **AdventedOS** as the reference deployment. The diagram below shows the full flow and where each component lives.

```
+------------------------------------------------------------------+
|                        Agent / LLM                               |
|  (Claude, GPT, or any MCP-capable model that can call            |
|   tools sequentially based on mcp_meta instructions)             |
+---------------------------+--------------------------------------+
                           |  JSON-RPC MCP (hud.brief, hud.ingest,
                           |  hud.project, hud.onboarding, hud.mcp)
                           v
+------------------------------------------------------------------+
|              Open WebUI (OWUI) -- Optional UI                     |
|  Tool server layer. Discovers tools from HUD's /openapi.json     |
|  Manages conversation state, user auth to the proxy.             |
+---------------------------+--------------------------------------+
                           |  HTTP (routed through proxy)
                           v
+------------------------------------------------------------------+
|           AdventedOS Proxy  (port 52415)                         |
|  Reference auth gateway -- handles:                              |
|    * MCP tool discovery (proxies /openapi.json from HUD)         |
|    * API key injection & credential management                   |
|    * Route orchestration (HUD, model inference, etc.)            |
|  Not required to run HUD -- any reverse proxy or direct          |
|  HTTP access works.                                              |
+---------------------------+--------------------------------------+
                           |  HTTP / JSON-RPC  (X-HUD-Admin-Key)
                           v
+------------------------------------------------------------------+
|                 AdventedHUD  (port 8200)                         |
|                                                                  |
|  +----------+  +----------+  +----------+  +---------------+     |
|  |  Brief   |  |  Ingest  |  | Project  |  | Onboarding   |     |
|  | (context)|  | (create) |  |(approve/ |  | (soul +      |     |
|  |          |  |          |  |  reject/ |  |  atomic)     |     |
|  |          |  |          |  | retract/ |  |              |     |
|  |          |  |          |  |  update) |  |              |     |
|  +----+-----+  +----+-----+  +----+-----+  +-------+------+     |
|       |             |             |                 |            |
|       +-------------+-------------+-----------------+            |
|                     |             |                              |
|                     v             v                              |
|              +------------------------------+                    |
|              |  Ingest/Project Pipeline     |                    |
|              |  (classification gate,       |                    |
|              |   projection dispatch)       |                    |
|              +---------------+--------------+                    |
+-------------------------------+----------------------------------+
                                |
             +------------------+------------------+
             v                  v                   v
+--------------------+  +--------------+  +--------------+
|   Obsidian         |  |   Google     |  |   Google     |
|   Vault            |  |   Calendar   |  |   Tasks      |
|  (source of        |  |  (projected) |  |  (projected) |
|   truth)           |  |              |  |              |
+--------------------+  +--------------+  +--------------+
```

### Flow Summary

1. **Agent classifies** user input using context from `hud.brief` (roles, goals, decision matrix)
2. **Agent calls** `hud.ingest` with classified item (role_ref, goal_ref, priority_class, semantic_type, google_target)
3. **HUD persists** to Obsidian first -- that's the source of truth
4. **On approval**, HUD projects to Google Calendar/Tasks via OAuth
5. The **AdventedOS proxy** sits between OWUI and HUD when deployed in reference mode -- handling auth, key injection, and route orchestration

---

## Quick Start

### Requirements

- Docker (recommended) **or** Python 3.10+
- A Google Cloud project with Calendar and Tasks APIs enabled (for projection -- optional)
- An Obsidian vault (optional -- HUD creates one)

### Install & Run (Docker)

```bash
# Clone the repo
git clone https://github.com/av151318/AdventedHUD.git
cd AdventedHUD

# Build the image
docker build -t adventedhud .

# Run with defaults (port 8200)
docker run -d -p 8200:8200 \
  -e HUD_ADMIN_API_KEY=your-secret-key \
  --name adventedhud \
  adventedhud
```

```bash
# Verify it's alive
curl -sS http://127.0.0.1:8200/health
```

To pass additional environment variables, append `-e VAR=value` flags to the `docker run` command. For persistent data, mount a volume:

```bash
docker run -d -p 8200:8200 \
  -e HUD_ADMIN_API_KEY=your-secret-key \
  -e HUD_ALLOW_WRITES=true \
  -v /path/to/data:/app/data \
  --name adventedhud \
  adventedhud
```

### Install & Run (native)

```bash
# Clone the repo
git clone https://github.com/av151318/AdventedHUD.git
cd AdventedHUD
pip install -r requirements.txt

export HUD_ADMIN_API_KEY=your-secret-key
python -m hud.main
```

### Wire Up an Agent

HUD's primary interface is JSON-RPC MCP. An agent calls five methods:

```bash
curl -sS -X POST http://127.0.0.1:8200/hud/mcp \
  -H 'Content-Type: application/json' \
  -H "X-HUD-Admin-Key: $HUD_ADMIN_API_KEY" \
  -d '{"jsonrpc":"2.0","method":"hud.brief"}'
```

To expose HUD's tools to **Open WebUI** or another OpenAPI-compatible tool server, point it at:

```
http://your-host:8200/openapi.json
```

> **For AdventedOS reference deployment**: the proxy at port 52415 handles this discovery automatically and injects the admin key. See [AdventedOS Context](#adventedos-context).

### Google OAuth Setup (for Calendar/Tasks Projection)

1. Create a Google Cloud Platform project
2. Enable **Google Calendar API** and **Google Tasks API**
3. Create OAuth 2.0 credentials (Desktop application type)
4. Download as `gOAuth1.json` and place in your `data/` directory, or use env vars:

```bash
export GOOGLE_OAUTH_CREDENTIAL_ID=your-client-id
export GOOGLE_OAUTH_CREDENTIAL_SECRET=your-client-secret
```

5. For token encryption, set:

```bash
export GOOGLE_TOKEN_ENCRYPTION_KEY=your-secure-random-key
```

**Default projection mode is `dry_run`** -- no data touches Google until you explicitly set it to `live` and approve items.

---

## Environment Reference

| Variable | Default | Purpose |
|----------|---------|---------|
| `HUD_PORT` | `8200` | Listen port |
| `HUD_HOST` | `127.0.0.1` | Bind address |
| `HUD_DB_PATH` | `data/hud.db` | SQLite store |
| `HUD_ADMIN_API_KEY` | _(required)_ | `X-HUD-Admin-Key` header value |
| `HUD_DATA_DIR` | `data/` (relative to repo root) | Root for OAuth credentials, Obsidian vault, DB |
| `HUD_SOUL_MD_PATH` | `data/obsidian/AdventedHUD/soul.md` | Onboarding soul file |
| `HUD_SOUL_MD_TEMPLATE_PATH` | _(optional)_ | Worksheet template for onboarding read |
| `HUD_REQUIRE_POST_ONBOARDING_PUSH` | `1` | Set `0` to skip push-policy gate in dev |
| `HUD_RESOLVE_JWT_SUBJECT` | `0` | Set `1` to prefer JWT `sub` as user id |
| `HUD_ALLOW_WRITES` | `false` | Enable file/DB writes (startup safety) |
| `HUD_ALLOW_GOOGLE_WRITES` | `false` | Enable Google Calendar/Tasks projection |
| `HUD_LOG_LEVEL` | `INFO` | Log verbosity |
| `GOOGLE_OAUTH_CREDENTIAL_ID` | _(env hint)_ | OAuth client ID |
| `GOOGLE_OAUTH_CREDENTIAL_FILE` | _(env hint)_ | Path to OAuth JSON file |
| `GOOGLE_OAUTH_CREDENTIAL_PATH` | _(env hint)_ | Dir to search for gOAuth*.json |
| `GOOGLE_OAUTH_CREDENTIALS` | _(env hint)_ | Inline OAuth JSON string |
| `GOOGLE_TOKEN_ENCRYPTION_KEY` | _(default placeholder)_ | Key for encrypting stored OAuth tokens |
| `GOOGLE_TOKEN_FILE` | _(env hint)_ | Path to token JSON file |

### Credential Resolution Order

HUD resolves Google OAuth credentials in this priority:

1. **Inline payload** -- `oauth_credentials` field passed in the request
2. **Environment hints** -- `GOOGLE_OAUTH_CREDENTIAL_ID`, `GOOGLE_OAUTH_CREDENTIAL_FILE`, etc.
3. **File discovery** -- scans `data/` for `gOAuth1.json`, `gOAuth2.json`, and `gOAuth*.json` files

Tokens are resolved similarly: inline -> env var -> file scan for `*.token.json` in `data/`.

---

## MCP Tool Surface

HUD exposes a five-tool JSON-RPC surface through a single endpoint (`/hud/mcp`), plus decomposed first-class HTTP paths for direct access.

### Core Tools

| Method | Purpose | Agent Use |
|--------|---------|-----------|
| `hud.brief` (default) | Returns roles, goals, decision matrix (Q1-Q4) for classification | First call every turn -- get context before classifying |
| `hud.brief` (scoped) | Returns human-readable briefing for a time period or goal | When user asks "what's on my plate?" |
| `hud.ingest` | Persists a classified item to Obsidian (with optional projection) | After classification -- create a todo/event/note |
| `hud.project` | Reviews item fate: `approve`, `reject`, `retract`, `update`, or `project` | For item review, approval decisions, and correcting/removing projected items |
| `hud.onboarding` | Reads/writes soul.md and atomic roles/goals | Initial setup, profile edits |

### Decomposed Onboarding Paths

For agents that prefer typed OpenAPI operations over JSON-RPC:

| Operation | HTTP Path | Method | Description |
|-----------|-----------|--------|-------------|
| `hud.onboarding.read` | `/hud/onboarding/read` | GET | Read soul.md + atomic template + validation status |
| `hud.onboarding.write_soul` | `/hud/onboarding/write_soul` | POST | Write soul.md markdown (step 1/3) |
| `hud.onboarding.set_atomic` | `/hud/onboarding/set_atomic` | POST | Persist atomic roles/goals (step 2/3) |
| `hud.onboarding.set_push` | `/hud/onboarding/set_push` | POST | Set external push policy (step 3/3) |

### OpenAPI Discovery

HUD serves a full OpenAPI 3.1 spec at `/openapi.json` for tool server auto-discovery. Compatible with OWUI, custom tool servers, and any OpenAPI client.

---

## HTTP Route Parity

All MCP methods have direct HTTP path equivalents:

| Route | Method | Maps To |
|-------|--------|---------|
| `/health` | GET | Service health check |
| `/hud/brief` | POST | `hud.brief` |
| `/hud/ingest` | POST | `hud.ingest` |
| `/hud/project` | POST | `hud.project` |
| `/hud/mcp` | POST | All tools (JSON-RPC) |
| `/hud/onboarding/soul` | GET, POST | Legacy combined onboarding |
| `/hud/onboarding/read` | GET | `hud.onboarding.read` |
| `/hud/onboarding/write_soul` | POST | `hud.onboarding.write_soul` |
| `/hud/onboarding/set_atomic` | POST | `hud.onboarding.set_atomic` |
| `/hud/onboarding/set_push` | POST | `hud.onboarding.set_push` |
| `/hud/sync_status` | POST | Sync status check |
| `/hud/status` | POST | Sync status (compat alias) |
| `/openapi.json` | GET | OpenAPI 3.1 discovery spec |

---

## Onboarding Ritual (v1.4.1)

HUD uses a multi-step onboarding to build the user's personal context (roles, goals, mission) and push policy preferences.

1. **`hud.brief` (no scope)** -- Returns classification context with `onboarding_state: incomplete`
2. **`hud.onboarding.read`** -- Returns the current soul.md template markdown and the exact `atomic_template` shape
3. **Write soul.md** -- `hud.onboarding.write_soul` with filled markdown (Roles Matrix + Goals Matrix tables)
4. **Set atomic** -- `hud.onboarding.set_atomic` with structured `roles`, `goals_by_role`, `primary_role_ref`, `primary_goal_ref`
5. **Set push policy** -- `hud.onboarding.set_push` with user's `external_push_without_approval` preference
6. **Verify** -- `hud.brief` (no scope) confirms `onboarding_state: fully_onboarded`

The onboarding uses **meta-prompting** -- tool responses include `mcp_meta.mode` that tells the agent what to do next (`efficiency`, `strict_ritual`) and reduces tool-calling overhead.

---

## Agent Harness Compatibility

> **Important note on tool calling patterns**

AdventedHUD was designed and tested against **Open WebUI's sequential tool-calling model**, where the agent calls one tool at a time and the system enforces a strict turn-by-turn flow.

The `mcp_meta` instruction system -- which tells agents "call this next" -- is optimized for this sequential pattern. Key behaviors to expect:

| Agent Harness | Compatibility | Notes |
|---------------|---------------|-------|
| **Open WebUI** | Full | Reference platform. Sequential calls, mcp_meta instructions, onboarding ritual -- all tested |
| **Claude Code / Claude Desktop** | Varies | MCP JSON-RPC works. Parallel tool calling may skip mcp_meta sequencing. Onboarding ritual instructions may behave differently |
| **Custom agent harness** | Varies | Direct HTTP to HUD works. If your harness calls tools in parallel or ignores mcp_meta, you may need to orchestrate the onboarding ritual steps yourself |
| **OpenAI Compatible** | Partial | The OpenAPI spec at `/openapi.json` works for tool discovery. Sequential constraint behavior depends on your client |

**Key callout**: The onboarding ritual relies on `mcp_meta.next_action` to guide the agent through the 6-step sequence. If your agent harness does not read and follow `mcp_meta` instructions between tool calls, you will need to orchestrate the onboarding steps externally.

---

## AdventedOS Context

AdventedHUD was built as part of **AdventedOS** -- a personal server infrastructure that bundles:

- **AdventedOS Proxy** -- An HTTP gateway (port 52415) that routes requests between OWUI, HUD, model inference endpoints, and other services. Handles MCP tool discovery, API key injection, and cross-service orchestration.
- **Open WebUI (OWUI)** -- Chat interface for interacting with LLMs. Discovers HUD's tools via the OpenAPI spec and manages conversation state.

### Running HUD in the AdventedOS Stack

In the reference deployment, the proxy lives at the monorepo root next to `AdventedHUD/`:

```
AdventedOS/
+-- AdventedHUD/        # this repo
+-- proxy/              # AdventedOS proxy (separate service)
+-- data/               # Shared data (OAuth credentials, Obsidian vault, DB)
+-- docker-compose.yml  # OWUI container config
```

The proxy automatically:
- Serves HUD's `/openapi.json` for OWUI tool discovery
- Injects the `X-HUD-Admin-Key` header on behalf of the user
- Routes `/hud/*` traffic to the HUD service on port 8200

**You do not need AdventedOS to run AdventedHUD.** The service is fully standalone -- point any HTTP client or MCP-capable agent directly at port 8200.

---

## Project Structure

```
AdventedHUD/
+-- main.py                     # Entry point
+-- hud/
|   +-- main.py                 # Server bootstrap (aiohttp)
|   +-- server.py               # Route definitions + OpenAPI spec
|   +-- mcp.py                  # JSON-RPC dispatch for all tools
|   +-- adapters.py             # Google OAuth, Calendar, Tasks projection
|   +-- brief_context.py        # Decision matrix + context builder
|   +-- contracts.py            # Shared models, error payloads, constants
|   +-- gates.py                # Admin auth, user resolution, onboarding gating
|   +-- handlers.py             # /health, /sync_status
|   +-- ingest_project.py       # Ingest + approve/reject/retract/update pipeline
|   +-- meta.py                 # Meta-prompting / efficiency modes
|   +-- onboarding.py           # Soul read/write with validation
|   +-- onboarding_db.py        # DB-level onboarding state management
|   +-- store.py                # SQLite store
|   +-- sync.py                 # Sync utilities
|   +-- workers.py              # Background workers
+-- tests/
|   +-- test_phase1_smoke.py
|   +-- test_brief_v141.py
|   +-- test_meta.py
|   +-- test_onboarding_db.py
|   +-- test_store.py
|   +-- test_soul_template_gates.py
|   +-- test_google_projection_lifecycle.py
+-- docs/
|   +-- hud_foundation_spec.md   # Foundation specification (authoritative)
|   +-- hud_skill.md             # Agent MCP contract (for LLM agents)
|   +-- soul-template.md         # Soul worksheet template
+-- requirements.txt             # aiohttp only
+-- README.md                    # you are here
```

---

## Docs

| Document | Purpose |
|----------|---------|
| [`docs/hud_foundation_spec.md`](docs/hud_foundation_spec.md) | Foundation specification. Describes intent, philosophy, atomic data model, decision matrix, projection rules, and onboarding gating. Authoritative design document. |
| [`docs/hud_skill.md`](docs/hud_skill.md) | Agent MCP contract. The exact instructions an LLM agent follows to interact with HUD. Includes onboarding ritual sequence, classification rules, and meta-prompting behavior. |
| [`docs/soul-template.md`](docs/soul-template.md) | User-facing soul worksheet template. The markdown file users fill with their mission, roles, goals, values, and priorities. |

---

## Tests

```bash
# From the repo root
pip install pytest
pytest tests -q

# All tests use monkeypatch + tmp_path -- no external dependencies, no live services
```

Test coverage:
- `test_phase1_smoke.py` -- End-to-end routing and basic MCP dispatch
- `test_brief_v141.py` -- Brief context with decision matrix
- `test_meta.py` -- Meta-prompting mode transitions
- `test_onboarding_db.py` -- DB-level onboarding state management
- `test_store.py` -- SQLite store operations
- `test_soul_template_gates.py` -- Soul template validation gating
- `test_google_projection_lifecycle.py` -- Google projection: deferred on ingest, dispatched on approve, auto-approve for Pattern B

---

## License

Apache 2.0

---

*Built as part of the AdventedOS personal infrastructure ecosystem.*
