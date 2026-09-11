import hashlib
import json
import logging
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

_HUD_TERMINAL_STATUSES = frozenset({"approved", "rejected", "failed", "duplicate", "synced", "retracted"})
_HUD_DEFAULT_PROJECTION_MODE = "dry_run"
_HUD_VALID_PROJECTION_MODES = frozenset({"dry_run", "live"})
_HUD_USER_PROJECTION_TABLE = "hud_user_projection_preferences"
_HUD_USER_ONBOARDING_TABLE = "hud_user_onboarding_states"
_HUD_USER_PUSH_POLICY_TABLE = "hud_user_push_policy"
_HUD_AGENT_KEYS_TABLE = "hud_agent_keys"
_HUD_STATUS_TRANSITIONS = {
    "pending": frozenset(
        {"queued", "approved", "rejected", "failed", "duplicate", "ready", "synced"}
    ),
    "pending_approval": frozenset(
        {"approved", "rejected", "failed", "duplicate", "queued", "synced"}
    ),
    "queued": frozenset(
        {"approved", "rejected", "failed", "duplicate", "queued", "ready", "synced"}
    ),
    "failed": frozenset(
        {"approved", "rejected", "failed", "duplicate", "synced"}
    ),
    "approved": frozenset({"approved", "synced", "retracted"}),
    "rejected": frozenset({"rejected", "synced"}),
    "duplicate": frozenset({"duplicate", "synced"}),
    "ready": frozenset({"ready", "synced"}),
    "synced": frozenset({"synced", "retracted"}),
    "retracted": frozenset({"retracted"}),
}

class HUDStore:
    def __init__(self, db_path: str = "data/hud.db"):
        self.db_path = str(Path(db_path).expanduser())
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
        except Exception:
            logger.exception("Failed to initialize HUDStore database at %s", self.db_path)
            raise

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _coerce_int(value: Any, default: int = 0) -> int:
        if value is None:
            return default
        try:
            return int(value)
        except (TypeError, ValueError):
            logger.warning("Invalid integer, defaulting to %s: %r", default, value)
            return default

    @staticmethod
    def _coerce_payload_json(payload: Any) -> str:
        if payload is None:
            return "{}"
        if isinstance(payload, str):
            try:
                json.loads(payload)
                return payload
            except json.JSONDecodeError:
                return json.dumps(payload)
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _coerce_timestamp(value: Any) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).isoformat()
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
        return str(value) if not isinstance(value, str) else value

    @staticmethod
    def _coerce_projection_mode(value: Any) -> str:
        if value is None:
            return _HUD_DEFAULT_PROJECTION_MODE
        candidate = str(value).strip().lower()
        if candidate == "direct":
            candidate = "live"
        if candidate in _HUD_VALID_PROJECTION_MODES:
            return candidate
        raise ValueError(f"Unsupported projection_mode '{value}'")

    @staticmethod
    def _coerce_user_id(value: Any) -> str:
        if value is None:
            return ""
        candidate = str(value).strip()
        return candidate

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _create_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS hud_items ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "internal_id TEXT NOT NULL UNIQUE,"
            "google_id TEXT,"
            "external_id TEXT,"
            "actor TEXT,"
            "intent TEXT,"
            "scope TEXT,"
            "payload_json TEXT NOT NULL,"
            "sync_hash TEXT,"
            "status TEXT NOT NULL,"
            "retry_count INTEGER NOT NULL DEFAULT 0,"
            "last_error TEXT,"
            "priority_class TEXT,"
            "google_target TEXT,"
            "semantic_type TEXT,"
            "role_ref TEXT,"
            "goal_ref TEXT,"
            "idempotency_key TEXT,"
            "created_at TEXT NOT NULL,"
            "updated_at TEXT NOT NULL,"
            "approved_by TEXT,"
            "reviewed_by TEXT,"
            "next_run_at TEXT,"
            "source_id TEXT,"
            "source_type TEXT,"
            "source_ref TEXT,"
            "worker_ref TEXT,"
            "last_synced_at TEXT,"
            "calendar_id TEXT,"
            "tasklist_id TEXT,"
            "relative_path TEXT,"
            "queue_rank INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_hud_items_idempotency_key ON hud_items(idempotency_key) WHERE idempotency_key IS NOT NULL")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hud_items_status ON hud_items(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_hud_items_scope ON hud_items(scope)")
        conn.execute(f"CREATE TABLE IF NOT EXISTS {_HUD_USER_PROJECTION_TABLE} ("
            "user_id TEXT PRIMARY KEY NOT NULL,"
            "projection_mode TEXT NOT NULL,"
            "updated_at TEXT NOT NULL)"
        )
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_HUD_USER_ONBOARDING_TABLE} ("
            "user_id TEXT PRIMARY KEY NOT NULL,"
            "role_ref TEXT,"
            "goal_ref TEXT,"
            "requires_approval INTEGER,"
            "roles_json TEXT,"
            "goals_json TEXT,"
            "updated_at TEXT NOT NULL)"
        )
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_HUD_USER_PUSH_POLICY_TABLE} ("
            "user_id TEXT PRIMARY KEY NOT NULL,"
            "external_push INTEGER NOT NULL,"
            "updated_at TEXT NOT NULL)"
        )
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_HUD_AGENT_KEYS_TABLE} ("
            "agent_id TEXT PRIMARY KEY NOT NULL,"
            "key_hash TEXT NOT NULL,"
            "grants_json TEXT NOT NULL,"
            "created_at TEXT NOT NULL,"
            "revoked_at TEXT)"
        )

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            self._create_schema(conn)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(hud_items)").fetchall()}
            if "id" not in columns:
                logger.warning("Adding missing id column to legacy hud_items table")
                conn.execute("ALTER TABLE hud_items ADD COLUMN id INTEGER")
                conn.execute("UPDATE hud_items SET id = rowid WHERE id IS NULL")
            if "queue_rank" not in columns:
                conn.execute("ALTER TABLE hud_items ADD COLUMN queue_rank INTEGER NOT NULL DEFAULT 0")
            if "google_target" not in columns:
                conn.execute("ALTER TABLE hud_items ADD COLUMN google_target TEXT")
            if "semantic_type" not in columns:
                conn.execute("ALTER TABLE hud_items ADD COLUMN semantic_type TEXT")
            if "role_ref" not in columns:
                conn.execute("ALTER TABLE hud_items ADD COLUMN role_ref TEXT")
            if "goal_ref" not in columns:
                conn.execute("ALTER TABLE hud_items ADD COLUMN goal_ref TEXT")
            if "source_id" not in columns:
                conn.execute("ALTER TABLE hud_items ADD COLUMN source_id TEXT")
            if "last_synced_at" not in columns:
                conn.execute("ALTER TABLE hud_items ADD COLUMN last_synced_at TEXT")
            if "calendar_id" not in columns:
                conn.execute("ALTER TABLE hud_items ADD COLUMN calendar_id TEXT")
            if "tasklist_id" not in columns:
                conn.execute("ALTER TABLE hud_items ADD COLUMN tasklist_id TEXT")
            if "relative_path" not in columns:
                conn.execute("ALTER TABLE hud_items ADD COLUMN relative_path TEXT")
            onboarding_cols = {
                row["name"]
                for row in conn.execute(
                    f"PRAGMA table_info({_HUD_USER_ONBOARDING_TABLE})"
                ).fetchall()
            }
            if "roles_json" not in onboarding_cols:
                conn.execute(
                    f"ALTER TABLE {_HUD_USER_ONBOARDING_TABLE} ADD COLUMN roles_json TEXT"
                )
            if "goals_json" not in onboarding_cols:
                conn.execute(
                    f"ALTER TABLE {_HUD_USER_ONBOARDING_TABLE} ADD COLUMN goals_json TEXT"
                )
            conn.commit()
        except sqlite3.Error:
            logger.exception("Failed to initialize HUDStore schema at %s", self.db_path)
            raise
        finally:
            conn.close()

    def _row_to_item(self, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        payload: Any = row["payload_json"]
        try:
            payload = json.loads(payload) if payload is not None else {}
        except (TypeError, json.JSONDecodeError):
            logger.warning("Invalid stored payload_json for internal_id=%s", row["internal_id"])
        return {
            "id": row["id"] if "id" in row.keys() and row["id"] is not None else row["row_id"],
            "internal_id": row["internal_id"], "google_id": row["google_id"], "external_id": row["external_id"],
            "actor": row["actor"], "intent": row["intent"], "scope": row["scope"],
            "payload_json": payload, "sync_hash": row["sync_hash"], "status": row["status"],
            "retry_count": self._coerce_int(row["retry_count"]), "last_error": row["last_error"],
            "priority_class": row["priority_class"], "google_target": row["google_target"] if "google_target" in row.keys() else None,
            "semantic_type": row["semantic_type"] if "semantic_type" in row.keys() else None,
            "role_ref": row["role_ref"] if "role_ref" in row.keys() else None,
            "goal_ref": row["goal_ref"] if "goal_ref" in row.keys() else None,
            "idempotency_key": row["idempotency_key"],
            "created_at": row["created_at"], "updated_at": row["updated_at"], "approved_by": row["approved_by"],
            "reviewed_by": row["reviewed_by"], "next_run_at": row["next_run_at"], "source_id": row["source_id"] if "source_id" in row.keys() else None,
            "source_type": row["source_type"],
            "source_ref": row["source_ref"], "worker_ref": row["worker_ref"], "queue_rank": row["queue_rank"],
            "last_synced_at": row["last_synced_at"] if "last_synced_at" in row.keys() else None,
            "calendar_id": row["calendar_id"] if "calendar_id" in row.keys() else None,
            "tasklist_id": row["tasklist_id"] if "tasklist_id" in row.keys() else None,
            "relative_path": row["relative_path"] if "relative_path" in row.keys() else None,
        }

    def _get_by_idempotency(self, conn: sqlite3.Connection, idempotency_key: str) -> Optional[Dict[str, Any]]:
        return self._row_to_item(
            conn.execute(
                "SELECT rowid AS row_id, * FROM hud_items WHERE idempotency_key = ? ORDER BY created_at ASC, internal_id ASC LIMIT 1",
                (idempotency_key,),
            ).fetchone()
        )

    def set_user_projection_mode(self, user_id: Any, projection_mode: str) -> bool:
        normalized_user_id = self._coerce_user_id(user_id)
        if not normalized_user_id:
            raise ValueError("user_id is required for projection mode persistence")
        normalized_projection_mode = self._coerce_projection_mode(projection_mode)
        now = self._now_iso()
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT INTO {_HUD_USER_PROJECTION_TABLE} (user_id, projection_mode, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "projection_mode=excluded.projection_mode, updated_at=excluded.updated_at",
                (normalized_user_id, normalized_projection_mode, now),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def delete_user_projection_mode(self, user_id: Any) -> bool:
        """Remove stored projection preference for user_id (revert to default dry_run resolution)."""
        normalized_user_id = self._coerce_user_id(user_id)
        if not normalized_user_id:
            raise ValueError("user_id is required to delete projection mode preference")
        conn = self._connect()
        try:
            cursor = conn.execute(
                f"DELETE FROM {_HUD_USER_PROJECTION_TABLE} WHERE user_id = ?",
                (normalized_user_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def get_user_projection_mode(self, user_id: Any) -> Optional[str]:
        normalized_user_id = self._coerce_user_id(user_id)
        if not normalized_user_id:
            return None

        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT projection_mode FROM {_HUD_USER_PROJECTION_TABLE} WHERE user_id = ? LIMIT 1",
                (normalized_user_id,),
            ).fetchone()
            if row is None:
                return None
            try:
                return self._coerce_projection_mode(row["projection_mode"])
            except ValueError:
                logger.warning("Invalid projection_mode stored for user_id=%s", normalized_user_id)
                return None
        finally:
            conn.close()

    def set_user_onboarding_state(
        self,
        user_id: Any,
        *,
        role_ref: Optional[str] = None,
        goal_ref: Optional[str] = None,
        requires_approval: Optional[bool] = None,
    ) -> bool:
        normalized_user_id = self._coerce_user_id(user_id)
        if not normalized_user_id:
            raise ValueError("user_id is required for onboarding state persistence")

        payload_requires_approval = None
        if requires_approval is not None:
            payload_requires_approval = 1 if bool(requires_approval) else 0
        now = self._now_iso()
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT INTO {_HUD_USER_ONBOARDING_TABLE} "
                "(user_id, role_ref, goal_ref, requires_approval, updated_at) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "role_ref=CASE WHEN excluded.role_ref IS NOT NULL THEN excluded.role_ref ELSE role_ref END, "
                "goal_ref=CASE WHEN excluded.goal_ref IS NOT NULL THEN excluded.goal_ref ELSE goal_ref END, "
                "requires_approval=CASE WHEN excluded.requires_approval IS NOT NULL THEN excluded.requires_approval ELSE requires_approval END, "
                "updated_at=excluded.updated_at",
                (
                    normalized_user_id,
                    role_ref,
                    goal_ref,
                    payload_requires_approval,
                    now,
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def get_user_onboarding_state(self, user_id: Any) -> Optional[Dict[str, Any]]:
        normalized_user_id = self._coerce_user_id(user_id)
        if not normalized_user_id:
            return None

        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT user_id, role_ref, goal_ref, requires_approval, roles_json, goals_json "
                f"FROM {_HUD_USER_ONBOARDING_TABLE} WHERE user_id = ? LIMIT 1",
                (normalized_user_id,),
            ).fetchone()
            if row is None:
                return None
            requires_approval = row["requires_approval"]
            if requires_approval is None:
                normalized_requires_approval = None
            else:
                normalized_requires_approval = bool(requires_approval)
            return {
                "user_id": row["user_id"],
                "role_ref": row["role_ref"],
                "goal_ref": row["goal_ref"],
                "requires_approval": normalized_requires_approval,
                "roles_json": row["roles_json"] if "roles_json" in row.keys() else None,
                "goals_json": row["goals_json"] if "goals_json" in row.keys() else None,
            }
        finally:
            conn.close()

    def set_user_onboarding_atomic(
        self,
        user_id: Any,
        *,
        roles: List[Mapping[str, Any]],
        goals_by_role: Mapping[str, Any],
        primary_role_ref: Optional[str] = None,
        primary_goal_ref: Optional[str] = None,
        requires_approval: Optional[bool] = None,
    ) -> bool:
        from hud.onboarding_db import validate_atomic_payload

        issues = validate_atomic_payload(roles, goals_by_role)
        if issues:
            raise ValueError(f"invalid atomic onboarding payload: {', '.join(issues)}")

        normalized_user_id = self._coerce_user_id(user_id)
        if not normalized_user_id:
            raise ValueError("user_id is required for onboarding state persistence")
        roles_json = json.dumps(list(roles), separators=(",", ":"), ensure_ascii=False)
        goals_json = json.dumps(dict(goals_by_role), separators=(",", ":"), ensure_ascii=False)
        payload_requires_approval = None
        if requires_approval is not None:
            payload_requires_approval = 1 if bool(requires_approval) else 0
        now = self._now_iso()
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT INTO {_HUD_USER_ONBOARDING_TABLE} "
                "(user_id, role_ref, goal_ref, requires_approval, roles_json, goals_json, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "role_ref=CASE WHEN excluded.role_ref IS NOT NULL THEN excluded.role_ref ELSE role_ref END, "
                "goal_ref=CASE WHEN excluded.goal_ref IS NOT NULL THEN excluded.goal_ref ELSE goal_ref END, "
                "requires_approval=CASE WHEN excluded.requires_approval IS NOT NULL "
                "THEN excluded.requires_approval ELSE requires_approval END, "
                "roles_json=excluded.roles_json, "
                "goals_json=excluded.goals_json, "
                "updated_at=excluded.updated_at",
                (
                    normalized_user_id,
                    primary_role_ref,
                    primary_goal_ref,
                    payload_requires_approval,
                    roles_json,
                    goals_json,
                    now,
                ),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def get_user_onboarding_atomic(self, user_id: Any) -> Optional[Dict[str, Any]]:
        row = self.get_user_onboarding_state(user_id)
        if not row:
            return None
        roles_json = row.get("roles_json")
        goals_json = row.get("goals_json")
        if not roles_json or not goals_json:
            return None
        try:
            roles = json.loads(roles_json)
            goals_by_role = json.loads(goals_json)
        except json.JSONDecodeError:
            return None
        if not isinstance(roles, list) or not isinstance(goals_by_role, dict):
            return None
        return {
            "user_id": row["user_id"],
            "requires_approval": row.get("requires_approval"),
            "roles": roles,
            "goals_by_role": goals_by_role,
        }

    def get_user_push_policy(self, user_id: Any) -> Optional[bool]:
        """True/False once user set post-onboarding push preference; None if never set."""
        normalized_user_id = self._coerce_user_id(user_id)
        if not normalized_user_id:
            return None
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT external_push FROM {_HUD_USER_PUSH_POLICY_TABLE} WHERE user_id = ? LIMIT 1",
                (normalized_user_id,),
            ).fetchone()
            if row is None:
                return None
            return bool(row["external_push"])
        finally:
            conn.close()

    def set_user_push_policy(self, user_id: Any, *, external_push: bool) -> bool:
        normalized_user_id = self._coerce_user_id(user_id)
        if not normalized_user_id:
            raise ValueError("user_id is required for push policy persistence")
        now = self._now_iso()
        val = 1 if external_push else 0
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT INTO {_HUD_USER_PUSH_POLICY_TABLE} (user_id, external_push, updated_at) "
                "VALUES (?, ?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET "
                "external_push=excluded.external_push, updated_at=excluded.updated_at",
                (normalized_user_id, val, now),
            )
            conn.commit()
            return True
        finally:
            conn.close()

    def _mark_duplicate_rows(self, conn: sqlite3.Connection, idempotency_key: str, fallback_internal_id: str) -> bool:
        reason = f"duplicate_of:{fallback_internal_id}"
        now = self._now_iso()
        cursor = conn.execute(
            "UPDATE hud_items SET status='duplicate', last_error = CASE WHEN last_error IS NULL OR last_error='' THEN ? ELSE last_error || '; ' || ? END, updated_at=? WHERE idempotency_key = ? AND internal_id != ?",
            (reason, reason, now, idempotency_key, fallback_internal_id),
        )
        return cursor.rowcount > 0

    def upsert_item(self, payload_dict: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload_dict, dict):
            raise ValueError("payload_dict must be a dictionary")

        now = self._now_iso()
        internal_id = str(payload_dict.get("internal_id") or str(uuid.uuid4()))
        idempotency_key = payload_dict.get("idempotency_key")
        if idempotency_key is not None:
            idempotency_key = str(idempotency_key)

        payload = self._coerce_payload_json(payload_dict.get("payload_json", {}))
        created_at = self._coerce_timestamp(payload_dict.get("created_at")) or now
        next_run_at = self._coerce_timestamp(payload_dict.get("next_run_at"))
        last_synced_at = self._coerce_timestamp(payload_dict.get("last_synced_at"))
        queue_rank = self._coerce_int(payload_dict.get("queue_rank", 0) or 0)

        insert_values = (
            internal_id, payload_dict.get("google_id"), payload_dict.get("external_id"),
            payload_dict.get("actor"), payload_dict.get("intent"), payload_dict.get("scope"), payload,
            payload_dict.get("sync_hash"), payload_dict.get("status") or "pending",
            self._coerce_int(payload_dict.get("retry_count", 0)), payload_dict.get("last_error"),
            payload_dict.get("priority_class"), payload_dict.get("google_target"), payload_dict.get("semantic_type"),
            payload_dict.get("role_ref"), payload_dict.get("goal_ref"), idempotency_key, created_at, now,
            payload_dict.get("approved_by"), payload_dict.get("reviewed_by"), next_run_at, payload_dict.get("source_id"),
            payload_dict.get("source_type"), payload_dict.get("source_ref"), payload_dict.get("worker_ref"), queue_rank,
            last_synced_at,
            payload_dict.get("calendar_id"), payload_dict.get("tasklist_id"), payload_dict.get("relative_path"),
        )

        conn = self._connect()
        try:
            if idempotency_key:
                existing = self._get_by_idempotency(conn, idempotency_key)
                if existing is not None and existing["internal_id"] != internal_id:
                    self._mark_duplicate_rows(conn, idempotency_key, existing["internal_id"])
                    conn.commit()
                    logger.info("Deduped upsert by key=%s using internal_id=%s", idempotency_key, existing["internal_id"])
                    return existing

            if conn.execute("SELECT 1 FROM hud_items WHERE internal_id = ?", (internal_id,)).fetchone() is not None:
                conn.execute(
                    "UPDATE hud_items SET google_id=?, external_id=?, actor=?, intent=?, scope=?, payload_json=?, sync_hash=?, status=?, retry_count=?, last_error=?, priority_class=?, google_target=?, semantic_type=?, role_ref=?, goal_ref=?, idempotency_key=?, updated_at=?, approved_by=?, reviewed_by=?, next_run_at=?, source_id=?, source_type=?, source_ref=?, worker_ref=?, queue_rank=?, last_synced_at=?, calendar_id=?, tasklist_id=?, relative_path=? WHERE internal_id=?",
                    (
                        payload_dict.get("google_id"), payload_dict.get("external_id"), payload_dict.get("actor"),
                        payload_dict.get("intent"), payload_dict.get("scope"), payload,
                        payload_dict.get("sync_hash"), payload_dict.get("status") or "pending",
                        self._coerce_int(payload_dict.get("retry_count", 0)), payload_dict.get("last_error"),
                        payload_dict.get("priority_class"), payload_dict.get("google_target"), payload_dict.get("semantic_type"),
                        payload_dict.get("role_ref"), payload_dict.get("goal_ref"), idempotency_key, now, payload_dict.get("approved_by"),
                        payload_dict.get("reviewed_by"), next_run_at, payload_dict.get("source_id"), payload_dict.get("source_type"),
                        payload_dict.get("source_ref"), payload_dict.get("worker_ref"), queue_rank, last_synced_at,
                        payload_dict.get("calendar_id"), payload_dict.get("tasklist_id"), payload_dict.get("relative_path"),
                        internal_id,
                    ),
                )
            else:
                conn.execute(
                    "INSERT INTO hud_items (internal_id, google_id, external_id, actor, intent, scope, payload_json, sync_hash, status, retry_count, last_error, priority_class, google_target, semantic_type, role_ref, goal_ref, idempotency_key, created_at, updated_at, approved_by, reviewed_by, next_run_at, source_id, source_type, source_ref, worker_ref, queue_rank, last_synced_at, calendar_id, tasklist_id, relative_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    insert_values,
                )
            conn.commit()
            return self._row_to_item(
                conn.execute("SELECT rowid AS row_id, * FROM hud_items WHERE internal_id = ?", (internal_id,)).fetchone()
            )
        except sqlite3.IntegrityError:
            if idempotency_key is None:
                raise
            existing = self._get_by_idempotency(conn, idempotency_key)
            if existing is not None:
                logger.warning("Handled idempotent upsert collision for key=%s", idempotency_key)
                return existing
            raise
        finally:
            conn.close()

    def get_item(self, internal_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            return self._row_to_item(
                conn.execute("SELECT rowid AS row_id, * FROM hud_items WHERE internal_id = ?", (internal_id,)).fetchone()
            )
        finally:
            conn.close()

    def list_items(self, status: Optional[str] = None, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        limit = max(0, int(limit)); offset = max(0, int(offset))
        query = "SELECT rowid AS row_id, * FROM hud_items"
        params = []
        if status is not None:
            query += " WHERE status = ?"
            params.append(status)
        query += " ORDER BY queue_rank ASC, created_at DESC LIMIT ? OFFSET ?"
        params.extend((limit, offset))

        conn = self._connect()
        try:
            rows = conn.execute(query, tuple(params)).fetchall()
            return [self._row_to_item(row) for row in rows if row is not None]
        finally:
            conn.close()

    def update_status(self, internal_id: str, status: str, last_error: Optional[str] = None,
                      retry_count: Optional[int] = None, actor: Optional[str] = None,
                      reviewed_by: Optional[str] = None, google_id: Optional[str] = None,
                      last_synced_at: Optional[Any] = None, external_id: Optional[str] = None,
                      calendar_id: Optional[str] = None, tasklist_id: Optional[str] = None,
                      relative_path: Optional[str] = None) -> bool:
        updates = {"status": status, "updated_at": self._now_iso()}
        if last_error is not None:
            updates["last_error"] = last_error
        if retry_count is not None:
            updates["retry_count"] = self._coerce_int(retry_count)
        if actor is not None:
            updates["actor"] = actor
        if reviewed_by is not None:
            updates["reviewed_by"] = reviewed_by
        if google_id is not None:
            updates["google_id"] = google_id
        if external_id is not None:
            updates["external_id"] = external_id
        if calendar_id is not None:
            updates["calendar_id"] = calendar_id
        if tasklist_id is not None:
            updates["tasklist_id"] = tasklist_id
        if relative_path is not None:
            updates["relative_path"] = relative_path
        if last_synced_at is not None:
            updates["last_synced_at"] = self._coerce_timestamp(last_synced_at)

        assign = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [internal_id]

        conn = self._connect()
        try:
            cursor = conn.execute(f"UPDATE hud_items SET {assign} WHERE internal_id = ?", tuple(values))
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    def update_external_identity(
        self,
        internal_id: str,
        *,
        google_id: Optional[str] = None,
        external_id: Optional[str] = None,
        calendar_id: Optional[str] = None,
        tasklist_id: Optional[str] = None,
        relative_path: Optional[str] = None,
    ) -> bool:
        """Persist external projection identity (Google ids / Obsidian path) without changing status.

        Called after a successful live projection so a later update/retract can
        target the exact external object(s).
        """
        updates: Dict[str, Any] = {"updated_at": self._now_iso()}
        if google_id is not None:
            updates["google_id"] = google_id
        if external_id is not None:
            updates["external_id"] = external_id
        if calendar_id is not None:
            updates["calendar_id"] = calendar_id
        if tasklist_id is not None:
            updates["tasklist_id"] = tasklist_id
        if relative_path is not None:
            updates["relative_path"] = relative_path
        assign = ", ".join(f"{key} = ?" for key in updates)
        values = list(updates.values()) + [internal_id]
        conn = self._connect()
        try:
            cursor = conn.execute(
                f"UPDATE hud_items SET {assign} WHERE internal_id = ?", tuple(values)
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def _coerce_status(value: Any) -> str:
        if value is None:
            return ""
        return str(value).strip().lower()

    @classmethod
    def _is_terminal_status(cls, status: Any) -> bool:
        return cls._coerce_status(status) in _HUD_TERMINAL_STATUSES

    @classmethod
    def _status_transition_allowed(cls, from_status: str, to_status: str) -> bool:
        if not from_status:
            return True
        if from_status == to_status:
            return True
        allowed = _HUD_STATUS_TRANSITIONS.get(from_status)
        if allowed is None:
            return True
        return to_status in allowed

    def transition_status(
        self,
        internal_id: str,
        status: str,
        *,
        last_error: Optional[str] = None,
        retry_count: Optional[int] = None,
        actor: Optional[str] = None,
        reviewed_by: Optional[str] = None,
        google_id: Optional[str] = None,
        last_synced_at: Optional[Any] = None,
        external_id: Optional[str] = None,
        calendar_id: Optional[str] = None,
        tasklist_id: Optional[str] = None,
        relative_path: Optional[str] = None,
    ) -> bool:
        """Transition item status through an allowed matrix; raise on invalid transition."""
        item = self.get_item(internal_id)
        if item is None:
            return False

        current_status = self._coerce_status(item.get("status"))
        target_status = self._coerce_status(status)
        if not self._status_transition_allowed(current_status, target_status):
            raise ValueError(f"invalid status transition '{current_status}' -> '{target_status}'")

        return self.update_status(
            internal_id,
            target_status,
            last_error=last_error,
            retry_count=retry_count,
            actor=actor,
            reviewed_by=reviewed_by,
            google_id=google_id,
            last_synced_at=last_synced_at,
            external_id=external_id,
            calendar_id=calendar_id,
            tasklist_id=tasklist_id,
            relative_path=relative_path,
        )

    def next_pending(self, limit: int = 50) -> List[Dict[str, Any]]:
        limit = max(0, int(limit))
        now = self._now_iso()
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT rowid AS row_id, * FROM hud_items WHERE status='pending' AND (next_run_at IS NULL OR next_run_at <= ?) ORDER BY queue_rank ASC, COALESCE(next_run_at, created_at) ASC LIMIT ?",
                (now, limit),
            ).fetchall()
            return [self._row_to_item(row) for row in rows if row is not None]
        finally:
            conn.close()

    def set_sync_hash(self, internal_id: str, sync_hash: Optional[str]) -> bool:
        conn = self._connect()
        try:
            cursor = conn.execute(
                "UPDATE hud_items SET sync_hash = ?, updated_at = ? WHERE internal_id = ?",
                (sync_hash, self._now_iso(), internal_id),
            )
            conn.commit()
            return cursor.rowcount > 0
        finally:
            conn.close()

    @staticmethod
    def hash_agent_key(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()

    @staticmethod
    def _normalize_agent_grants(grants: Mapping[str, Any], *, agent_id: str) -> Dict[str, Any]:
        allowed_role_refs = list(grants.get("allowed_role_refs") or [])
        allowed_tools = list(grants.get("allowed_tools") or [])
        allowed_google_targets = list(grants.get("allowed_google_targets") or [])
        normalized: Dict[str, Any] = {
            "agent_id": agent_id,
            "allowed_role_refs": [str(v) for v in allowed_role_refs],
            "allowed_tools": [str(v) for v in allowed_tools],
            "allowed_google_targets": [str(v) for v in allowed_google_targets],
        }
        if grants.get("calendar_id") is not None:
            normalized["calendar_id"] = str(grants.get("calendar_id"))
        if grants.get("tasklist_id") is not None:
            normalized["tasklist_id"] = str(grants.get("tasklist_id"))
        return normalized

    def _row_to_agent_key(self, row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        grants: Any = row["grants_json"]
        try:
            grants = json.loads(grants) if grants is not None else {}
        except (TypeError, json.JSONDecodeError):
            logger.warning("Invalid stored grants_json for agent_id=%s", row["agent_id"])
            grants = {}
        return {
            "agent_id": row["agent_id"],
            "key_hash": row["key_hash"],
            "grants": grants,
            "created_at": row["created_at"],
            "revoked_at": row["revoked_at"],
        }

    def get_agent_key(self, agent_id: str) -> Optional[Dict[str, Any]]:
        conn = self._connect()
        try:
            row = conn.execute(
                f"SELECT agent_id, key_hash, grants_json, created_at, revoked_at FROM {_HUD_AGENT_KEYS_TABLE} WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            return self._row_to_agent_key(row)
        finally:
            conn.close()

    def provision_agent_key(
        self,
        agent_id: str,
        grants: Mapping[str, Any],
        *,
        plaintext: Optional[str] = None,
    ) -> Dict[str, Any]:
        agent_id = str(agent_id).strip()
        if not agent_id:
            raise ValueError("agent_id is required")
        secret = plaintext if plaintext else secrets.token_urlsafe(32)
        key_hash = self.hash_agent_key(secret)
        normalized = self._normalize_agent_grants(grants, agent_id=agent_id)
        grants_json = json.dumps(normalized, separators=(",", ":"), ensure_ascii=False)
        now = self._now_iso()
        conn = self._connect()
        try:
            conn.execute(
                f"INSERT INTO {_HUD_AGENT_KEYS_TABLE} (agent_id, key_hash, grants_json, created_at, revoked_at) "
                "VALUES (?, ?, ?, ?, NULL)",
                (agent_id, key_hash, grants_json, now),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "agent_id": agent_id,
            "key": secret,
            "grants": normalized,
            "created_at": now,
        }

    def mark_duplicate(self, idempotency_key: str, fallback_internal_id: str) -> bool:
        conn = self._connect()
        try:
            changed = self._mark_duplicate_rows(conn, idempotency_key, fallback_internal_id)
            conn.commit()
            return changed
        finally:
            conn.close()
