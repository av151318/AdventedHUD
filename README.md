# AdventedHUD

Standalone HUD MCP service (v1.4.1). Exposes HTTP routes and JSON-RPC MCP on port **8200** by default.

Canonical docs: `docs/hud_foundation_spec.md`, `docs/hud_skill.md`, `docs/soul-template.md`.

## Run

```bash
cd /path/to/AdventedOS
export PYTHONPATH=AdventedHUD
export HUD_ADMIN_API_KEY=your-secret
export HUD_REQUIRE_POST_ONBOARDING_PUSH=0   # optional for local smoke
python -m hud.main
```

Environment:

| Variable | Default | Purpose |
|----------|---------|---------|
| `HUD_PORT` | `8200` | Listen port |
| `HUD_HOST` | `127.0.0.1` | Bind address |
| `HUD_DB_PATH` | `data/hud.db` | SQLite store |
| `HUD_ADMIN_API_KEY` | _(required for HUD routes)_ | `X-HUD-Admin-Key` header |
| `HUD_SOUL_MD_PATH` | `data/obsidian/AdventedHUD/soul.md` | Onboarding soul file |
| `HUD_SOUL_MD_TEMPLATE_PATH` | _(optional)_ | Worksheet template for onboarding read |
| `HUD_REQUIRE_POST_ONBOARDING_PUSH` | `1` | Set `0` to skip push-policy gate in dev |
| `HUD_RESOLVE_JWT_SUBJECT` | `0` | Set `1` to prefer JWT `sub` as user id |

## Health

```bash
curl -sS http://127.0.0.1:8200/health
```

## MCP (five-tool surface)

Methods: `hud.ingest`, `hud.brief`, `hud.project`, `hud.onboarding`, `hud.mcp`.

```bash
curl -sS -X POST http://127.0.0.1:8200/hud/mcp \
  -H 'Content-Type: application/json' \
  -H "X-HUD-Admin-Key: $HUD_ADMIN_API_KEY" \
  -d '{"jsonrpc":"2.0","method":"hud.brief"}'
```

Approve/reject item fate uses `hud.project` with `{"action":"approve"|"reject","item_id":"..."}`.

## HTTP parity

| Route | Method |
|-------|--------|
| `/health` | GET |
| `/hud/sync_status`, `/hud/status` | POST |
| `/hud/brief` | POST |
| `/hud/ingest` | POST |
| `/hud/project` | POST |
| `/hud/mcp` | POST |
| `/hud/onboarding/soul` | GET, POST |

## Onboarding ritual (v1.4.1)

1. `hud.onboarding` read (no markdown) returns worksheet `markdown`.
2. Write with `markdown` only → `ritual_controlled` + `data.mcp_meta.mode: strict_ritual` (efficiency + STRICT PROCEDURE text).
3. Re-call with `markdown` + atomic `roles`, `goals_by_role`, `primary_role_ref`, `primary_goal_ref` → `status: ok`; DB row with `roles_json` / `goals_json`. No COMPLETE signal — verify with default `hud.brief` (`onboarding_state: fully_onboarded`).
4. Optional: `external_push_without_approval` on write or alone sets push policy.
5. **Profile edit** (after onboarded): markdown-only write or `profile_edit: true` updates soul without strict ritual.

## Acceptance script

```bash
export HUD_ADMIN_API_KEY=your-secret
export HUD_REQUIRE_POST_ONBOARDING_PUSH=0
export HUD_BASE_URL=http://127.0.0.1:8200
bash scripts/hud_mcp_acceptance.sh
```

The script validates `decision_matrix` (Q1–Q4), `mcp_meta` on incomplete onboarding, and runs a minimal atomic onboarding before ingest/project.

## Adapters path limitation

`hud/adapters.py` resolves `data/` via `Path(__file__).resolve().parents[3] / "data"`. When AdventedHUD lives under the repo root, OAuth/token files under `data/` work as in the monolith. If you relocate only `AdventedHUD/`, mount `data/` or set `GOOGLE_OAUTH_*` env hints.

## Tests

```bash
cd AdventedOS
PYTHONPATH=AdventedHUD pytest AdventedHUD/tests -q
```

## Path note

`parents[3]` in adapters assumes package layout `AdventedHUD/hud/adapters.py` under repo root. A future release may introduce configurable data roots (`HUD_DATA_ROOT`) without changing the five-tool MCP contract.
