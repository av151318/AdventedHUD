"""Standalone aiohttp application for AdventedHUD."""

from __future__ import annotations

import os
from pathlib import Path

from aiohttp import web

from hud.adapters import HUDAdapterHub
from hud.contracts import (
    HUD_ROUTE_AGENT_KEYS,
    HUD_ROUTE_BRIEF,
    HUD_ROUTE_INGEST,
    HUD_ROUTE_MCP,
    HUD_ROUTE_ONBOARDING_SOUL,
    HUD_ROUTE_ONBOARDING_READ,
    HUD_ROUTE_ONBOARDING_WRITE_SOUL,
    HUD_ROUTE_ONBOARDING_SET_ATOMIC,
    HUD_ROUTE_ONBOARDING_SET_PUSH,
    HUD_ROUTE_PROJECT,
    HUD_ROUTE_STATUS_COMPAT,
    HUD_ROUTE_SYNC_STATUS,
)
from hud.handlers import handle_brief, handle_health, handle_provision_agent_key, handle_sync_status
from hud.ingest_project import handle_hud_ingest, handle_hud_project
from hud.mcp import handle_mcp
from hud.onboarding import handle_onboarding_soul_read, handle_onboarding_soul_write, handle_onboarding_read, handle_onboarding_write_soul, handle_onboarding_set_atomic, handle_onboarding_set_push
from hud.store import HUDStore
from hud.workers import HUDWorkers


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return default


def create_app() -> web.Application:
    db_path = os.environ.get("HUD_DB_PATH", "data/hud.db")
    app = web.Application()
    store = HUDStore(str(Path(db_path).expanduser()))
    workers = HUDWorkers()
    hud_allow_writes = _env_bool("HUD_ALLOW_WRITES", False)
    hud_allow_google_writes = _env_bool("HUD_ALLOW_GOOGLE_WRITES", hud_allow_writes)
    hub = HUDAdapterHub(
        allow_writes=hud_allow_writes,
        allow_google_writes=hud_allow_google_writes,
    )
    app["hud_store"] = store
    app["hud_workers"] = workers
    app["hud_adapter_hub"] = hub

    app.router.add_get("/health", handle_health)
    app.router.add_post(HUD_ROUTE_SYNC_STATUS, handle_sync_status)
    app.router.add_post(HUD_ROUTE_STATUS_COMPAT, handle_sync_status)
    app.router.add_post(HUD_ROUTE_BRIEF, handle_brief)
    app.router.add_post(HUD_ROUTE_INGEST, handle_hud_ingest)
    app.router.add_post(HUD_ROUTE_PROJECT, handle_hud_project)
    app.router.add_post(HUD_ROUTE_AGENT_KEYS, handle_provision_agent_key)
    app.router.add_post(HUD_ROUTE_MCP, handle_mcp)
    app.router.add_get(HUD_ROUTE_ONBOARDING_SOUL, handle_onboarding_soul_read)
    app.router.add_post(HUD_ROUTE_ONBOARDING_SOUL, handle_onboarding_soul_write)

    # First-class narrow decomposed onboarding methods (direct paths for OpenAPI discovery; 1 MCP still canonical via /hud/mcp)
    app.router.add_get(HUD_ROUTE_ONBOARDING_READ, handle_onboarding_read)
    app.router.add_post(HUD_ROUTE_ONBOARDING_WRITE_SOUL, handle_onboarding_write_soul)
    app.router.add_post(HUD_ROUTE_ONBOARDING_SET_ATOMIC, handle_onboarding_set_atomic)
    app.router.add_post(HUD_ROUTE_ONBOARDING_SET_PUSH, handle_onboarding_set_push)

    # OAI API MCP discovery for OWUI / OpenAPI tool servers
    app.router.add_get("/openapi.json", handle_openapi)
    return app

OPENAPI_SPEC = {
    "openapi": "3.1.0",
    "info": {
        "title": "AdventedHUD - OAI API MCP",
        "version": "1.4.1",
        "description": "Human-first planning MCP surface. Routes natural language into atomic roles/goals/plans with Google projection and Obsidian persistence."
    },
    "servers": [{"url": "/"}],
    "paths": {
        "/hud/brief": {
            "post": {
                "summary": "Get classification context (roles, goals, decision matrix)",
                "operationId": "hud_brief",
                "requestBody": {"required": False, "content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"200": {"description": "Brief with roles/goals/matrix"}}
            }
        },
        "/hud/ingest": {
            "post": {
                "summary": "After hud_brief for context, call this with classification fields (role_ref, goal_ref, priority_class, semantic_type, google_target) and item content. THE creation tool — persists to Obsidian and projects to your target.",
                "operationId": "hud_ingest",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "properties": {
                                    "title": {"type": "string", "description": "Item title"},
                                    "content": {"type": "string", "description": "Item content or body"},
                                    "role_ref": {"type": "string", "description": "Role this item belongs to. From hud_brief response roles."},
                                    "goal_ref": {"type": "string", "description": "Optional goal slug this item advances. From hud_brief response goals_by_role."},
                                    "priority_class": {"type": "string", "enum": ["critical", "high", "medium", "low"], "description": "Priority from decision matrix classification."},
                                    "semantic_type": {"type": "string", "enum": ["event", "todo", "note"], "description": "What kind of item this is. Determines projection behavior."},
                                    "google_target": {"type": "string", "enum": ["obsidian", "calendar", "tasks"], "description": "Where to project. obsidian=local only, calendar=Google Calendar, tasks=Google Tasks."},
                                    "scope": {"type": "string", "description": "Projection scope (today, week, or goal:<slug>)"},
                                    "intent": {"type": "string", "description": "Processing intent; affects classification"},
                                    "projection_mode": {"type": "string", "enum": ["live", "dry_run"], "description": "Override projection mode (defaults to stored user preference or dry_run)."},
                                    "requires_approval": {"type": "boolean", "description": "true = queue for review (Pattern A). false = allow immediate projection when push policy allows (Pattern B)."},
                                    "text": {"type": "string", "description": "Alias for content/body."},
                                    "due": {"type": "string", "description": "Todo due date (YYYY-MM-DD or RFC3339). Required shape for google_target tasks when scheduling matters."},
                                    "time_zone": {"type": "string", "description": "IANA timezone (e.g. America/Los_Angeles). Used for calendar start/end when dateTime strings omit offset."},
                                    "start": {"$ref": "#/components/schemas/CalendarEventTime", "description": "REQUIRED when google_target is calendar. Event start (Google Calendar API shape)."},
                                    "end": {"$ref": "#/components/schemas/CalendarEventTime", "description": "REQUIRED when google_target is calendar. Event end (Google Calendar API shape)."},
                                }
                            }
                        }
                    }
                },
                "responses": {"200": {"description": "Ingest result with classification and projection; check adapter_projection.external_dispatch for Google calendar/tasks write"}}
            }
        },
        "/hud/project": {
            "post": {
                "summary": "Project / approve / reject / retract / update structured items",
                "operationId": "hud_project",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object", "properties": {"item_id": {"type": "string", "description": "HUD item identifier from hud.ingest or hud.brief"}, "action": {"type": "string", "enum": ["project", "approve", "reject", "retract", "update"], "description": "approve=approve pending item, reject=deny a non-projected item, retract=delete a projected item's live Google/Obsidian projection, update=patch a projected item in place, project=generic upsert"}, "fate": {"type": "string", "enum": ["project", "approve", "reject", "retract", "update"], "description": "Alias for action"}, "projection_mode": {"type": "string", "enum": ["live", "dry_run"], "description": "Override projection mode"}, "title": {"type": "string", "description": "update: new title"}, "summary": {"type": "string", "description": "update: new calendar summary"}, "content": {"type": "string", "description": "update: new content/body"}, "start": {"$ref": "#/components/schemas/CalendarEventTime", "description": "update: new event start"}, "end": {"$ref": "#/components/schemas/CalendarEventTime", "description": "update: new event end"}, "due": {"type": "string", "description": "update: new task due date"}}}}}},
                "responses": {"200": {"description": "Project result"}}
            }
        },
        "/hud/mcp": {
            "post": {
                "summary": "1 MCP JSON-RPC entrypoint — all tools (hud.* including first-class narrow onboarding: read/write_soul/set_atomic/set_push; legacy combined deprecated). Narrow methods also exposed as dedicated first-class paths (/hud/onboarding/read etc) for direct typed OpenAPI tools matching mcp_meta names.",
                "operationId": "hud_mcp",
                "requestBody": {"required": True, "content": {"application/json": {"schema": {"type": "object"}}}},
                "responses": {"200": {"description": "JSON-RPC response"}}
            }
        },
        "/hud/onboarding/read": {
            "get": {
                "summary": "Read soul.md + machine-ready atomic template and validation status (narrow first-class: hud.onboarding.read)",
                "description": "First-class narrow decomposed read. Mirrors the legacy but is the preferred typed operationId for agents following mcp_meta. Returns markdown, current_atomic, atomic_template, validation_status, atomic_schema, mcp_meta, onboarding flags.",
                "operationId": "hud.onboarding.read",
                "responses": {
                    "200": {
                        "description": "Rich onboarding read response with scaffolding for reliable writes",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "markdown": {"type": "string", "description": "Full current soul.md content"},
                                        "current_atomic": {"$ref": "#/components/schemas/AtomicPayload", "nullable": True},
                                        "atomic_template": {"$ref": "#/components/schemas/AtomicPayload", "description": "Deterministic, correct template the agent should edit and send back"},
                                        "validation_status": {"$ref": "#/components/schemas/ValidationStatus"},
                                        "atomic_schema": {"type": "object", "description": "JSON Schema for the atomic payload (MCP standard)"},
                                        "onboarding_needed": {"type": "boolean"},
                                        "onboarding_complete": {"type": "boolean"},
                                        "mcp_meta": {"type": "object"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "/hud/onboarding/write_soul": {
            "post": {
                "summary": "Write soul.md markdown only (narrow first-class hud.onboarding.write_soul — persistence step 1/3)",
                "description": "First-class narrow for decomposed ritual. Send only markdown (no atomic required). On success, mcp_meta guides to next set_atomic. Reuses internal build logic with narrow bypass. Legacy combined deprecated.",
                "operationId": "hud.onboarding.write_soul",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["markdown"],
                                "properties": {
                                    "markdown": {"type": "string", "description": "Full polished soul.md (from atomic_template + user input)"},
                                    "external_push_without_approval": {"type": "boolean"}
                                }
                            }
                        }
                    }
                },
                "responses": {
                    "200": {
                        "description": "Soul written (narrow path). Returns path, flags, next_action for set_atomic, mcp_meta.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "onboarding_complete": {"type": "boolean"},
                                        "mcp_meta": {"type": "object"}
                                    }
                                }
                            }
                        }
                    },
                    "409": {
                        "description": "Onboarding ritual violation or validation failure. Rich error with next_action.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "error": {"type": "string"},
                                        "code": {"type": "string"},
                                        "data": {
                                            "type": "object",
                                            "properties": {
                                                "validation_issues": {"type": "array", "items": {"type": "string"}},
                                                "atomic_schema": {"type": "object"},
                                                "atomic_template": {"$ref": "#/components/schemas/AtomicPayload"},
                                                "next_action": {"type": "string"}
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "/hud/onboarding/set_atomic": {
            "post": {
                "summary": "Persist atomic roles/goals (narrow first-class hud.onboarding.set_atomic — persistence step 2/3)",
                "description": "First-class narrow. Request body is AtomicPayload {roles, goals_by_role} (refined from read's atomic_template). Reuses dispatch + set logic. After this, call set_push. Legacy deprecated.",
                "operationId": "hud.onboarding.set_atomic",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/AtomicPayload"}
                        }
                    }
                },
                "responses": {
                    "200": {
                        "description": "Atomic set. mcp_meta will indicate next set_push or brief verify.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "atomic_set": {"type": "boolean"},
                                        "onboarding_complete": {"type": "boolean"},
                                        "mcp_meta": {"type": "object"}
                                    }
                                }
                            }
                        }
                    },
                    "409": {
                        "description": "Validation failure on atomic payload.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "error": {"type": "string"},
                                        "code": {"type": "string"},
                                        "data": {"type": "object"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "/hud/onboarding/set_push": {
            "post": {
                "summary": "Set external push policy (narrow first-class hud.onboarding.set_push — persistence step 3/3)",
                "description": "First-class narrow. Body: { \"external_push_without_approval\": true/false }. Reuses existing dispatch push logic. Then hud.brief to confirm fully_onboarded. Legacy deprecated.",
                "operationId": "hud.onboarding.set_push",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["external_push_without_approval"],
                                "properties": {
                                    "external_push_without_approval": {"type": "boolean", "description": "Whether to push without further approval"}
                                }
                            }
                        }
                    }
                },
                "responses": {
                    "200": {
                        "description": "Push policy recorded. Call hud.brief to verify fully_onboarded state and exit onboarding mode.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "onboarding_complete": {"type": "boolean"},
                                        "mcp_meta": {"type": "object"}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        },
        "/hud/onboarding/soul": {
            "get": {
                "summary": "Read soul.md + machine-ready atomic template and validation status (LEGACY — prefer narrow first-class /hud/onboarding/read)",
                "description": "DEPRECATED legacy path (use narrow first-class instead). Returns the current soul.md, a server-generated atomic_template (ready-to-edit template derived from Roles Matrix / Goals Matrix tables), current persisted atomic, and structured validation feedback. This is the primary way agents obtain the correct payload shape. Legacy combined deprecated; narrow read at dedicated path.",
                "operationId": "hud_onboarding_soul_read",
                "responses": {
                    "200": {
                        "description": "Rich onboarding read response with scaffolding for reliable writes",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "markdown": {"type": "string", "description": "Full current soul.md content"},
                                        "current_atomic": {"$ref": "#/components/schemas/AtomicPayload", "nullable": True},
                                        "atomic_template": {"$ref": "#/components/schemas/AtomicPayload", "description": "Deterministic, correct template the agent should edit and send back"},
                                        "validation_status": {"$ref": "#/components/schemas/ValidationStatus"},
                                        "atomic_schema": {"type": "object", "description": "JSON Schema for the atomic payload (MCP standard)"},
                                        "onboarding_needed": {"type": "boolean"},
                                        "onboarding_complete": {"type": "boolean"},
                                        "mcp_meta": {"type": "object"}
                                    }
                                }
                            }
                        }
                    }
                }
            },
            "post": {
                "summary": "Write soul.md + atomic roles/goals (LEGACY COMBINED DEPRECATED — use narrow first-class: /hud/onboarding/write_soul + /hud/onboarding/set_atomic + /hud/onboarding/set_push)",
                "description": "DEPRECATED legacy combined path. Prefer the 4 narrow decomposed methods as first-class direct operations (now exposed in this OpenAPI for typed tool use). The narrow allow markdown-only write_soul then separate atomic/push. Use /hud/mcp for JSON-RPC too. This path kept for backward compat only.",
                "operationId": "hud_onboarding_write",
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["markdown", "atomic"],
                                "properties": {
                                    "markdown": {"type": "string", "description": "Full soul.md content to persist"},
                                    "atomic": {"$ref": "#/components/schemas/AtomicPayload"},
                                    "mode": {"type": "string", "enum": ["initial", "correction", "partial"], "default": "initial"},
                                    "partial_update": {"type": "boolean", "default": False, "description": "Merge instead of replace"},
                                    "external_push_without_approval": {"type": "boolean"}
                                }
                            }
                        }
                    }
                },
                "responses": {
                    "200": {
                        "description": "Write accepted. Returns validation status of what was persisted.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "path": {"type": "string"},
                                        "validation_status": {"$ref": "#/components/schemas/ValidationStatus"},
                                        "onboarding_complete": {"type": "boolean"},
                                        "mcp_meta": {"type": "object"}
                                    }
                                }
                            }
                        }
                    },
                    "409": {
                        "description": "Onboarding ritual violation or validation failure. Rich error with schema, issues, and exact next action.",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "properties": {
                                        "error": {"type": "string"},
                                        "code": {"type": "string"},
                                        "data": {
                                            "type": "object",
                                            "properties": {
                                                "validation_issues": {"type": "array", "items": {"type": "string"}},
                                                "atomic_schema": {"type": "object"},
                                                "atomic_template": {"$ref": "#/components/schemas/AtomicPayload"},
                                                "next_action": {"type": "string"}
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    },
    "components": {
        "schemas": {
            "AtomicPayload": {
                "type": "object",
                "required": ["roles", "goals_by_role"],
                "properties": {
                    "roles": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "required": ["slug", "name"],
                            "properties": {
                                "slug": {"type": "string"},
                                "name": {"type": "string"},
                                "description": {"type": "string"}
                            }
                        }
                    },
                    "goals_by_role": {
                        "type": "object",
                        "additionalProperties": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["goal"],
                                "properties": {
                                    "goal": {"type": "string"},
                                    "done_definition": {"type": "string"}
                                }
                            }
                        }
                    }
                }
            },
            "ValidationStatus": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "enum": ["complete", "incomplete"]},
                    "issues": {"type": "array", "items": {"type": "string"}},
                    "message": {"type": "string"}
                }
            },
            "CalendarEventTime": {
                "description": "Google Calendar event time (timed or all-day).",
                "oneOf": [
                    {
                        "type": "object",
                        "required": ["dateTime"],
                        "properties": {
                            "dateTime": {"type": "string", "description": "RFC3339 local or offset datetime, e.g. 2026-06-26T16:30:00"},
                            "timeZone": {"type": "string", "description": "IANA timezone when dateTime has no offset"}
                        }
                    },
                    {
                        "type": "object",
                        "required": ["date"],
                        "properties": {
                            "date": {"type": "string", "description": "All-day date YYYY-MM-DD"}
                        }
                    },
                    {"type": "string", "description": "ISO dateTime string or YYYY-MM-DD (all-day); pair with root time_zone for timed events"}
                ]
            }
        },
        "securitySchemes": {
            "HUDAdminKey": {"type": "apiKey", "in": "header", "name": "X-HUD-Admin-Key"}
        }
    }
}

async def handle_openapi(request: web.Request) -> web.Response:
    return web.json_response(OPENAPI_SPEC)

