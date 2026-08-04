"""Deterministic v1 HUD adapter scaffold for offline projection."""

from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone, timedelta
from hashlib import sha256
import base64
import logging
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence
import urllib.error
import urllib.parse
import urllib.request

logger = logging.getLogger(__name__)

HUDStoreRecord = Dict[str, Any]
_DEFAULT_CREATED_AT = "1970-01-01T00:00:00+00:00"
_STATUS_FALLBACK = "pending"
_HUD_STORE_STATUSES = {"pending", "pending_approval", "queued", "approved", "rejected", "failed", "duplicate"}
_GLOBAL_STATUS_MAP = {"new": "pending", "queued": "queued", "ready": "queued", "open": "pending", "running": "pending", "active": "queued", "pending": "pending", "approved": "approved", "done": "approved", "completed": "approved", "snoozed": "queued", "rejected": "rejected", "cancelled": "rejected", "failed": "failed", "error": "failed", "duplicate": "duplicate"}
_DATA_DIR = Path(os.environ.get("HUD_DATA_DIR", str(Path(__file__).resolve().parents[2] / "data"))).expanduser()
_GOOGLE_OAUTH_DEFAULT_FILES = ("gOAuth1.json", "gOAuth2.json")
_GOOGLE_OAUTH_DEFAULT_TOKEN_FILES = ("gOAuth1.token.json", "gOAuth2.token.json")
_GOOGLE_OAUTH_ENV_HINTS = (
    "GOOGLE_OAUTH_CREDENTIAL_ID",
    "GOOGLE_OAUTH_CREDENTIAL_FILE",
    "GOOGLE_OAUTH_CREDENTIAL_PATH",
    "GOOGLE_OAUTH_CREDENTIALS",
)
_GOOGLE_OAUTH_PAYLOAD_KEY = "oauth_credentials"
_GOOGLE_OAUTH_TOKEN_PAYLOAD_KEY = "token_credentials"
_GOOGLE_OAUTH_TOKEN_FILE_ENV = "GOOGLE_OAUTH_TOKEN_FILE"
_GOOGLE_OAUTH_REFRESH_URL = "https://oauth2.googleapis.com/token"


def _as_text(value: Any, default: Optional[str] = None) -> Optional[str]:
    if value is None:
        return default
    text = str(value).strip()
    return text if text else default


def _coerce_payload(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    return {"value": value}


def _coerce_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_fragment(value: Any, *, allow_path: bool = False) -> str:
    raw = _as_text(value, default="").replace("\\", "/").strip()
    if not raw:
        return ""
    parts = raw.split("/") if allow_path else [raw]
    cleaned = []
    for part in parts:
        token = "".join(ch if (ch.isalnum() or ch in {"_", "-", ".", " "}) else "_" for ch in part.strip())
        token = token.replace(" ", "_").strip("._-")
        if token and token not in {".", ".."}:
            cleaned.append(token)
    return "/".join(cleaned)


def _mask_client_secret(value: Optional[str]) -> Optional[str]:
    secret = _as_text(value)
    if not secret:
        return None
    if len(secret) <= 6:
        return "***"
    return f"{secret[:4]}...{secret[-4:]}"




# ── Token encryption at rest ──────────────────────────────────────
_TOKEN_ENC_PREFIX = "_enc:"

def _get_token_encryption_key() -> bytes:
    key_text = os.environ.get("GOOGLE_TOKEN_ENCRYPTION_KEY", "")
    if not key_text:
        key_text = "adventedos-default-enc-key-change-me"
    return sha256(key_text.encode("utf-8")).digest()


def _encrypt_token_value(plaintext: str, key=None) -> str:
    if not plaintext:
        return plaintext
    if key is None:
        key = _get_token_encryption_key()
    data = plaintext.encode("utf-8")
    result = bytearray(len(data))
    for i, b in enumerate(data):
        result[i] = b ^ key[i % len(key)]
    return _TOKEN_ENC_PREFIX + base64.b64encode(bytes(result)).decode("ascii")


def _decrypt_token_value(ciphertext: str, key=None) -> str:
    if not ciphertext or not ciphertext.startswith(_TOKEN_ENC_PREFIX):
        return ciphertext
    if key is None:
        key = _get_token_encryption_key()
    raw = base64.b64decode(ciphertext[len(_TOKEN_ENC_PREFIX):])
    result = bytearray(len(raw))
    for i, b in enumerate(raw):
        result[i] = b ^ key[i % len(key)]
    return result.decode("utf-8")


def _maybe_decrypt_token_payload(payload):
    payload = dict(payload)
    rt = payload.get("refresh_token")
    if isinstance(rt, str) and rt.startswith(_TOKEN_ENC_PREFIX):
        payload["refresh_token"] = _decrypt_token_value(rt)
    return payload

def _coerce_string_list(value: Any) -> Sequence[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        return []

    values: list[str] = []
    for item in raw_values:
        text = _as_text(item)
        if text:
            values.append(text)
    return values


def _read_oauth_json_file(path: Path) -> Optional[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(loaded, Mapping):
        return dict(loaded)
    return None


def _extract_oauth_credentials(raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    selected = dict(raw)
    for section in ("web", "installed"):
        section_value = selected.get(section)
        if isinstance(section_value, Mapping):
            selected = dict(section_value)
            break

    client_id = _as_text(selected.get("client_id") or selected.get("clientId"))
    client_secret = _as_text(selected.get("client_secret") or selected.get("clientSecret"))
    project_id = _as_text(selected.get("project_id") or selected.get("projectId"))
    if not client_id or not client_secret or not project_id:
        return None

    redirect_uris = _coerce_string_list(
        selected.get("redirect_uris")
        if selected.get("redirect_uris") is not None
        else selected.get("redirect_uri")
    )
    if not redirect_uris:
        return None

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "project_id": project_id,
        "redirect_uris": redirect_uris,
    }


def _resolve_oauth_paths(hint: str) -> Sequence[Path]:
    clean_hint = hint.strip()
    if not clean_hint:
        return []

    candidate = Path(clean_hint).expanduser()
    base_hint_paths: list[Path] = []
    if not candidate.is_absolute():
        base_hint_paths.append(_DATA_DIR / candidate)
        if candidate.suffix == "":
            base_hint_paths.append(_DATA_DIR / f"{candidate.name}.json")
    else:
        base_hint_paths.append(candidate)

    base_hint_paths.append(candidate)
    base_hint_paths.append(candidate.with_suffix(".json") if candidate.suffix == "" else candidate)
    base_hint_paths.append((_DATA_DIR / candidate.name).with_suffix(".json"))
    return list(dict.fromkeys(base_hint_paths))


def _resolve_oauth_from_hint(hint: Any) -> Optional[Dict[str, Any]]:
    resolved = _resolve_oauth_from_hint_with_path(hint)
    return resolved[0] if resolved is not None else None


def _resolve_oauth_from_hint_with_path(hint: Any) -> Optional[tuple[Dict[str, Any], Optional[Path]]]:
    if isinstance(hint, Mapping):
        extracted = _extract_oauth_credentials(hint)
        if extracted is None:
            return None
        return extracted, None

    text_hint = _as_text(hint)
    if text_hint is None:
        return None

    if text_hint.startswith("{") and text_hint.endswith("}"):
        try:
            loaded = json.loads(text_hint)
        except (TypeError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, Mapping):
            mapped = _extract_oauth_credentials(loaded)
            if mapped is not None:
                return mapped, None

    for candidate_path in _resolve_oauth_paths(text_hint):
        if not candidate_path.is_file():
            continue
        loaded = _read_oauth_json_file(candidate_path)
        if loaded is None:
            continue
        extracted = _extract_oauth_credentials(loaded)
        if extracted is not None:
            return extracted, candidate_path
    return None


def _scan_default_oauth_metadata() -> Optional[Dict[str, Any]]:
    metadata, _ = _scan_default_oauth_metadata_with_path()
    return metadata


def _scan_default_oauth_metadata_with_path() -> tuple[Optional[Dict[str, Any]], Optional[Path]]:
    if not _DATA_DIR.is_dir():
        return None, None

    candidate_files = []
    for filename in _GOOGLE_OAUTH_DEFAULT_FILES:
        candidate_files.append(_DATA_DIR / filename)
    if not any(path.is_file() for path in candidate_files):
        candidate_files = sorted(_DATA_DIR.glob("gOAuth*.json"))

    for path in candidate_files:
        if not path.is_file():
            continue
        loaded = _read_oauth_json_file(path)
        if loaded is None:
            continue
        extracted = _extract_oauth_credentials(loaded)
        if extracted is not None:
            return extracted, path
    return None, None


def _resolve_google_oauth_metadata(payload: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    payload_json = _coerce_payload(payload)
    if _GOOGLE_OAUTH_PAYLOAD_KEY in payload_json:
        return _resolve_oauth_from_hint(payload_json.get(_GOOGLE_OAUTH_PAYLOAD_KEY))

    for env_name in _GOOGLE_OAUTH_ENV_HINTS:
        env_value = os.environ.get(env_name)
        if env_value is None:
            continue
        resolved = _resolve_oauth_from_hint(env_value)
        if resolved is not None:
            return resolved

    return _scan_default_oauth_metadata()


def _resolve_google_oauth_metadata_with_details(payload: Mapping[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional[Path]]:
    payload_json = _coerce_payload(payload)
    if _GOOGLE_OAUTH_PAYLOAD_KEY in payload_json:
        resolved = _resolve_oauth_from_hint_with_path(payload_json.get(_GOOGLE_OAUTH_PAYLOAD_KEY))
        if resolved is not None:
            return resolved

    for env_name in _GOOGLE_OAUTH_ENV_HINTS:
        env_value = os.environ.get(env_name)
        if env_value is None:
            continue
        resolved = _resolve_oauth_from_hint_with_path(env_value)
        if resolved is not None:
            return resolved

    return _scan_default_oauth_metadata_with_path()


def _google_oauth_metadata_response(metadata: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "client_id": _as_text(metadata.get("client_id")),
        "project_id": _as_text(metadata.get("project_id")),
        "redirect_uris": list(_coerce_string_list(metadata.get("redirect_uris"))),
        "client_secret_masked": _mask_client_secret(_as_text(metadata.get("client_secret"))),
    }


def _google_oauth_metadata_with_token_state(metadata: Mapping[str, Any], token_state: Mapping[str, Any]) -> Dict[str, Any]:
    return {"oauth": _google_oauth_metadata_response(metadata), "token_state": token_state}


def _google_oauth_token_state(token_payload: Optional[Mapping[str, Any]], *, token_source: str) -> Dict[str, Any]:
    normalized = _coerce_payload(token_payload)
    return {
        "provider": "google",
        "token_source": _as_text(token_source, default="missing"),
        "has_access_token": bool(_as_text(normalized.get("access_token"), default=None)),
        "has_refresh_token": bool(_as_text(normalized.get("refresh_token"), default=None)),
    }


def _normalize_google_scopes(raw: Any) -> Sequence[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [value for value in raw.replace(",", " ").split() if value]
    if isinstance(raw, (list, tuple, set)):
        return _coerce_string_list(raw)
    return []


def _normalize_google_token_payload(raw: Mapping[str, Any]) -> Dict[str, Any]:
    normalized = dict(raw)
    normalized["scopes"] = list(_normalize_google_scopes(normalized.get("scopes") or normalized.get("scope")))
    if normalized["scopes"]:
        normalized["scopes"] = [str(scope) for scope in normalized["scopes"]]
    if (
        _as_text(normalized.get("access_token"), default=None) is not None
        and _as_text(normalized.get("expires_at"), default=None) is None
        and _as_text(normalized.get("token_expiry"), default=None) is None
    ):
        expiry_seconds = _coerce_int(normalized.get("expires_in"), default=0)
        if expiry_seconds > 0:
            normalized["expires_at"] = _to_utc_iso(datetime.now(timezone.utc) + timedelta(seconds=expiry_seconds))
            normalized["token_expiry"] = normalized["expires_at"]
    if _as_text(normalized.get("token_uri"), default=None) is None:
        normalized["token_uri"] = _GOOGLE_OAUTH_REFRESH_URL
    if "scope" in normalized:
        normalized.pop("scope")
    normalized["reauth_required"] = bool(normalized.get("reauth_required"))
    normalized = _maybe_decrypt_token_payload(normalized)
    return normalized


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)

    text = _as_text(value, default=None)
    if text is None:
        return None
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return None
    try:
        if text.isdigit():
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, OSError):
        return None


def _to_utc_iso(value: Any) -> Optional[str]:
    dt = _coerce_datetime(value)
    if dt is None:
        text = _as_text(value, default=None)
        return text
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _google_token_expiry_datetime(token_payload: Mapping[str, Any]) -> Optional[datetime]:
    for key in ("expires_at", "token_expiry"):
        expiry = _coerce_datetime(token_payload.get(key))
        if expiry is not None:
            return expiry
    return None


def _google_token_is_expired(token_payload: Mapping[str, Any], *, now: Optional[datetime] = None) -> bool:
    expiry = _google_token_expiry_datetime(token_payload)
    if expiry is None:
        return False
    now_value = now or datetime.now(timezone.utc)
    return expiry <= now_value


def _resolve_google_token_from_hint(raw: Any) -> tuple[Optional[Dict[str, Any]], Optional[Path]]:
    if isinstance(raw, Mapping):
        return _normalize_google_token_payload(raw), None

    text_hint = _as_text(raw)
    if text_hint is None:
        return None, None

    if text_hint.startswith("{") and text_hint.endswith("}"):
        try:
            loaded = json.loads(text_hint)
        except (TypeError, json.JSONDecodeError):
            loaded = None
        if isinstance(loaded, Mapping):
            return _normalize_google_token_payload(loaded), None

    token_path = Path(text_hint).expanduser()
    if token_path.is_file():
        loaded = _read_oauth_json_file(token_path)
        if loaded is not None:
            return _normalize_google_token_payload(loaded), token_path
    return {"access_token": text_hint}, None
    return None, None


def _resolve_google_token_file_candidates(*, oauth_metadata_path: Optional[Path] = None) -> Sequence[Path]:
    candidate_files: list[Path] = []
    if oauth_metadata_path is not None:
        candidate_files.append(oauth_metadata_path.with_suffix(".token.json"))
    candidate_files.extend(_DATA_DIR / filename for filename in _GOOGLE_OAUTH_DEFAULT_TOKEN_FILES)
    if _DATA_DIR.is_dir():
        candidate_files.extend(sorted(_DATA_DIR.glob("gOAuth*.token.json")))
    return list(dict.fromkeys(candidate_files))


def _resolve_google_token_context(payload: Mapping[str, Any], *, oauth_metadata_path: Optional[Path]) -> Dict[str, Any]:
    payload_json = _coerce_payload(payload)
    token_data, token_path = _resolve_google_token_from_hint(payload_json.get(_GOOGLE_OAUTH_TOKEN_PAYLOAD_KEY))
    if token_data is not None:
        return {"token": token_data, "path": token_path, "source": "payload.token_credentials"}

    env_path_value = _as_text(os.environ.get(_GOOGLE_OAUTH_TOKEN_FILE_ENV))
    if env_path_value is not None:
        env_token_path = Path(env_path_value).expanduser()
        if env_token_path.is_file():
            loaded = _read_oauth_json_file(env_token_path)
            if loaded is not None:
                return {"token": _normalize_google_token_payload(loaded), "path": env_token_path, "source": "environment.GOOGLE_OAUTH_TOKEN_FILE"}

    for token_candidate in _resolve_google_token_file_candidates(oauth_metadata_path=oauth_metadata_path):
        if not token_candidate.is_file():
            continue
        loaded = _read_oauth_json_file(token_candidate)
        if loaded is not None:
            return {"token": _normalize_google_token_payload(loaded), "path": token_candidate, "source": f"file:{token_candidate.name}"}
    return {"token": None, "path": None, "source": "missing"}


def _sanitize_google_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    sanitized = dict(payload)
    sanitized.pop(_GOOGLE_OAUTH_PAYLOAD_KEY, None)
    sanitized.pop(_GOOGLE_OAUTH_TOKEN_PAYLOAD_KEY, None)
    return sanitized


def _google_api_request_json(
    *,
    method: str,
    url: str,
    access_token: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
    query: Optional[Mapping[str, Any]] = None,
    timeout: float = 20.0,
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    request_url = url
    query_items = []
    if query is not None:
        for key, value in query.items():
            text_value = _as_text(value, default=None)
            if text_value is None:
                continue
            query_items.append((key, text_value))
    if query_items:
        request_url = f"{request_url}?{urllib.parse.urlencode(query_items)}"

    request_body: Optional[bytes] = None
    headers = {"Accept": "application/json"}
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
    if payload is not None and method.upper() in {"POST", "PATCH", "PUT"}:
        request_body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(request_body))

    request = urllib.request.Request(request_url, data=request_body, headers=headers, method=method.upper())
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        return None, {"code": str(exc.code), "status": exc.code, "reason": _as_text(exc.reason, default="http_error"), "body": error_body}
    except (OSError, ValueError, TypeError) as exc:
        return None, {"code": "request_failed", "status": "error", "body": _as_text(str(exc), default="request error"), "reason": exc.__class__.__name__}

    if not raw:
        return {}, None
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return None, {"code": "invalid_json", "reason": "invalid_json", "body": raw}
    if not isinstance(loaded, Mapping):
        return None, {"code": "invalid_payload", "reason": "invalid_payload", "body": raw}
    return dict(loaded), None


def _unwrap_payload_value(payload: Mapping[str, Any], *, max_depth: int = 5) -> Dict[str, Any]:
    normalized_payload = dict(payload)
    for _ in range(max_depth):
        nested_payload = normalized_payload.get("payload")
        if not isinstance(nested_payload, Mapping):
            break
        merged_payload = dict(nested_payload)
        merged_payload.update({key: value for key, value in normalized_payload.items() if key != "payload"})
        normalized_payload = merged_payload
    return normalized_payload


def _extract_date(s):
    if not s:
        return None
    m = re.search(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else None

def _rfc3339_date(date_str):
    return f"{date_str}T00:00:00.000Z"

def _normalize_task_due(payload):
    p = _unwrap_payload_value(payload)
    result = {
        "granularity": "date",
        "date": None,
        "dateTime": None,
        "timeZone": None,
        "inferred": False,
    }
    due_str = _as_text(p.get("due"), default=_as_text(p.get("scheduled_for"), default=None))
    if due_str:
        date_match = re.match(r"^(\d{4}-\d{2}-\d{2})", due_str)
        if date_match:
            result["date"] = date_match.group(1)
            if len(due_str) > 10:
                result["granularity"] = "datetime"
                result["dateTime"] = due_str
        return result
    d_str = _as_text(p.get("date"))
    if d_str and re.match(r"^\d{4}-\d{2}-\d{2}$", d_str):
        result["date"] = d_str
        return result
    return result

def _normalize_event_time(value, time_zone=None):
    if isinstance(value, dict):
        if "dateTime" in value:
            dt = _as_text(value.get("dateTime"))
            if dt:
                r = {"dateTime": dt}
                tz = _as_text(value.get("timeZone")) or time_zone
                if tz:
                    r["timeZone"] = tz
                return r
        if "date" in value:
            d = _as_text(value.get("date"))
            if d and re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                return {"date": d}
        return None
    text_time = _as_text(value, default=None)
    if text_time is None:
        return None
    is_all_day = len(text_time) == 10 and text_time[4] == "-" and text_time[7] == "-"
    if is_all_day:
        return {"date": text_time}
    r = {"dateTime": text_time}
    if time_zone is not None:
        r["timeZone"] = time_zone
    return r



def _google_api_is_token_error(api_error: Optional[Mapping[str, Any]]) -> bool:
    if not isinstance(api_error, Mapping):
        return False

    status = api_error.get("status")
    if status == 401 or status == "401":
        return True

    code = _as_text(api_error.get("code"), default="").lower()
    reason = _as_text(api_error.get("reason"), default="").lower()
    body = _as_text(api_error.get("body"), default="").lower()
    if code == "invalid_credentials" or "invalid credentials" in code:
        return True
    if "invalid credentials" in reason or "invalid_credentials" in reason:
        return True
    if "invalid credentials" in body or "invalid_token" in body or "invalid_grant" in body:
        return True
    return False


def _google_api_request_json_with_refresh(
    *,
    method: str,
    url: str,
    metadata: Mapping[str, Any],
    token_context: Mapping[str, Any],
    token_state: Mapping[str, Any],
    adapter_name: str,
    action: str,
    access_token: Optional[str],
    payload: Optional[Mapping[str, Any]] = None,
    query: Optional[Mapping[str, Any]] = None,
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
    response_body, api_error = _google_api_request_json(
        method=method,
        url=url,
        access_token=access_token,
        payload=payload,
        query=query,
    )
    if api_error is None:
        return response_body, None, dict(token_state)

    if not _google_api_is_token_error(api_error):
        return response_body, api_error, dict(token_state)

    _, _, refreshed_state, refresh_error = _google_ensure_access_token(
        adapter_name=adapter_name,
        action=action,
        metadata=metadata,
        token_context=token_context,
        force_refresh=True,
    )
    if refresh_error is not None:
        return None, refresh_error, dict(refreshed_state)

    refreshed_access = _as_text(refreshed_state.get("access_token"), default=None)
    if refreshed_access is None:
        return None, {"code": "auth_required", "message": f"{adapter_name} refresh result missing access_token for action={action}"}, dict(refreshed_state)

    response_body, retry_error = _google_api_request_json(
        method=method,
        url=url,
        access_token=refreshed_access,
        payload=payload,
        query=query,
    )
    return response_body, retry_error, dict(refreshed_state)


def _google_refresh_access_token(
    *,
    metadata: Mapping[str, Any],
    token_payload: Mapping[str, Any],
    token_uri: str,
) -> tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    refresh_token = _as_text(token_payload.get("refresh_token"), default=None)
    if refresh_token is None:
        return None, {"code": "auth_required", "message": "refresh_token missing"}

    client_id = _as_text(metadata.get("client_id"), default=None)
    client_secret = _as_text(metadata.get("client_secret"), default=None)
    if not client_id or not client_secret:
        return None, {"code": "config_missing", "message": "client credentials missing for refresh"}

    form_payload = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        }
    ).encode("utf-8")
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
        "Content-Length": str(len(form_payload)),
    }
    request = urllib.request.Request(token_uri, data=form_payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=20.0) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        return None, {"code": "auth_required", "message": f"refresh failed status={exc.code}", "body": error_body}
    except (OSError, ValueError, TypeError) as exc:
        return None, {"code": "auth_required", "message": f"refresh request failed: {_as_text(str(exc), default='error')}"}

    try:
        refreshed = json.loads(raw)
    except json.JSONDecodeError:
        return None, {"code": "auth_required", "message": "refresh returned invalid JSON"}
    if not isinstance(refreshed, Mapping):
        return None, {"code": "auth_required", "message": "refresh returned non-object payload"}

    access_token = _as_text(refreshed.get("access_token"), default=None)
    if access_token is None:
        return None, {"code": "auth_required", "message": "refresh response missing access_token"}

    merged = _normalize_google_token_payload(token_payload)
    merged["access_token"] = access_token
    merged["token_uri"] = token_uri
    if _as_text(refreshed.get("scope"), default=None) is not None:
        merged["scopes"] = _normalize_google_scopes(refreshed.get("scope"))
    if _coerce_int(refreshed.get("expires_in"), default=0) > 0:
        merged["expires_at"] = _to_utc_iso(datetime.now(timezone.utc) + timedelta(seconds=_coerce_int(refreshed.get("expires_in"), default=0)))
        merged["token_expiry"] = merged["expires_at"]
    return merged, None


def _persist_google_token_file(path: Path, token_payload: Mapping[str, Any]) -> None:
    try:
        payload = dict(_normalize_google_token_payload(token_payload))
        rt = payload.get("refresh_token")
        if isinstance(rt, str) and rt and not rt.startswith(_TOKEN_ENC_PREFIX):
            payload["refresh_token"] = _encrypt_token_value(rt)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
    except (OSError, TypeError, ValueError):
        return


def _google_ensure_access_token(
    *,
    adapter_name: str,
    action: str,
    metadata: Mapping[str, Any],
    force_refresh: bool = False,
    token_context: Mapping[str, Any],
) -> tuple[Optional[str], Optional[Dict[str, Any]], Dict[str, Any], Optional[Dict[str, Any]]]:
    token_payload = token_context.get("token")
    token_source = _as_text(token_context.get("source"), default="missing")
    token_path = token_context.get("path")
    if not isinstance(token_payload, Mapping):
        token_state = _google_oauth_token_state(None, token_source=token_source)
        return None, None, token_state, {"code": "auth_required", "message": f"{adapter_name} requires google token_credentials for action={action}"}

    token_payload_map = _normalize_google_token_payload(token_payload)
    token_state = _google_oauth_token_state(token_payload_map, token_source=token_source)

    if token_payload_map.get("reauth_required"):
        return None, token_payload_map, token_state, {"code": "reauth_required", "message": f"{adapter_name} reauth required for action={action}"}

    access_token = _as_text(token_payload_map.get("access_token"), default=None)
    if access_token is not None and not force_refresh and not _google_token_is_expired(token_payload_map):
        return access_token, token_payload_map, token_state, None

    refresh_token = _as_text(token_payload_map.get("refresh_token"), default=None)
    if refresh_token is None:
        return None, token_payload_map, token_state, {"code": "reauth_required", "message": f"{adapter_name} token refresh required for action={action}"}

    token_uri = _as_text(token_payload_map.get("token_uri"), default=_GOOGLE_OAUTH_REFRESH_URL)
    if token_uri is None:
        return None, token_payload_map, token_state, {"code": "config_missing", "message": f"{adapter_name} token_uri missing for action={action}"}

    refreshed, refresh_error = _google_refresh_access_token(metadata=metadata, token_payload=token_payload_map, token_uri=token_uri)
    if refresh_error is not None:
        err_body = _as_text(refresh_error.get("body"), default="").lower()
        if "invalid_grant" in err_body or "revoked" in err_body:
            logger.warning("GOOGLE_OAUTH refresh_terminal adapter=%s action=%s reason=invalid_grant", adapter_name, action)
            token_payload_map["reauth_required"] = True
            if isinstance(token_path, Path):
                _persist_google_token_file(token_path, token_payload_map)
            return None, token_payload_map, token_state, {"code": "reauth_required", "message": f"{adapter_name} token expired or revoked for action={action}"}
        logger.warning("GOOGLE_OAUTH refresh_failure adapter=%s action=%s code=%s", adapter_name, action, refresh_error.get("code", "?"))
        return None, token_payload_map, token_state, refresh_error
    if refreshed is None:
        return None, token_payload_map, token_state, {"code": "auth_required", "message": f"{adapter_name} token refresh returned empty payload for action={action}"}

    if isinstance(token_path, Path):
        _persist_google_token_file(token_path, refreshed)

    refreshed_access = _as_text(refreshed.get("access_token"), default=None)
    if refreshed_access is None:
        return None, refreshed, _google_oauth_token_state(refreshed, token_source=token_source), {"code": "auth_required", "message": f"{adapter_name} refresh result missing access_token for action={action}"}

    logger.info("GOOGLE_OAUTH refresh_ok adapter=%s action=%s expires_in=%s", adapter_name, action, refreshed.get("expires_in", "?"))
    return refreshed_access, refreshed, _google_oauth_token_state(refreshed, token_source=token_source), None


def _google_event_payload_from_input(payload: Mapping[str, Any]) -> Dict[str, Any]:
    normalized_payload = _unwrap_payload_value(payload)

    time_zone = _as_text(normalized_payload.get("time_zone"), default=None)

    body: Dict[str, Any] = {}
    summary = _as_text(normalized_payload.get("summary"), default=_as_text(normalized_payload.get("title"), default=_as_text(normalized_payload.get("text"), default=None)))
    if summary is not None:
        body["summary"] = summary
    start = _normalize_event_time(normalized_payload.get("start"), time_zone=time_zone)
    end = _normalize_event_time(normalized_payload.get("end"), time_zone=time_zone)
    if start is not None:
        body["start"] = start
    if end is not None:
        body["end"] = end
    description = _as_text(normalized_payload.get("description"), default=None)
    if description is not None:
        body["description"] = description
    location = _as_text(normalized_payload.get("location"), default=None)
    if location is not None:
        body["location"] = location
    return body


def _google_task_payload_from_input(payload: Mapping[str, Any]) -> Dict[str, Any]:
    normalized_payload = _unwrap_payload_value(payload)

    body: Dict[str, Any] = {}
    title = _as_text(
        normalized_payload.get("title"),
        default=_as_text(
            normalized_payload.get("summary"),
            default=_as_text(
                normalized_payload.get("name"),
                default=_as_text(normalized_payload.get("text"), default=None),
            ),
        ),
    )
    if title is not None:
        body["title"] = title
    description = _as_text(
        normalized_payload.get("description"),
        default=_as_text(normalized_payload.get("notes"), default=None),
    )
    if description is not None:
        body["notes"] = description

    # Due — normalized RFC 3339, always from date only
    # Google Tasks discards time: serialize at UTC midnight.
    _task_due_nt = _normalize_task_due(normalized_payload)
    if _task_due_nt["date"]:
        body["due"] = _rfc3339_date(_task_due_nt["date"])

    return body


def _google_calendar_item_from_api(item: Mapping[str, Any], *, calendar_id: str) -> Dict[str, Any]:
    external_id = _as_text(item.get("id"), default=None)
    start = item.get("start")
    end = item.get("end")
    return {
        "external_id": external_id,
        "google_id": external_id,
        "calendar_id": _as_text(calendar_id, default="primary"),
        "summary": _as_text(item.get("summary"), default=None),
        "title": _as_text(item.get("summary"), default=None),
        "description": _as_text(item.get("description"), default=None),
        "location": _as_text(item.get("location"), default=None),
        "start": start if isinstance(start, Mapping) else None,
        "end": end if isinstance(end, Mapping) else None,
        "status": _normalize_status(item.get("status"), status_map={"confirmed": "approved", "tentative": "queued", "needsAction": "pending", "cancelled": "rejected"}),
    }


def _google_task_item_from_api(item: Mapping[str, Any], *, tasklist_id: str) -> Dict[str, Any]:
    external_id = _as_text(item.get("id"), default=None)
    return {
        "external_id": external_id,
        "google_id": external_id,
        "tasklist_id": _as_text(tasklist_id, default="@default"),
        "title": _as_text(item.get("title"), default=None),
        "notes": _as_text(item.get("notes"), default=None),
        "status": _normalize_status(item.get("status"), status_map={"needsAction": "pending", "needs_action": "pending", "in_progress": "pending", "completed": "approved"}),
    }


def _google_api_error_payload(
    adapter_name: str,
    action: str,
    *,
    metadata: Mapping[str, Any],
    token_state: Mapping[str, Any],
    details: Mapping[str, Any],
) -> Dict[str, Any]:
    return {
        "status": "error",
        "code": _as_text(details.get("code"), default="error"),
        "message": f"{adapter_name} {action} request failed: {_as_text(details.get('message'), default='unknown')}",
        "metadata": _google_oauth_metadata_with_token_state(metadata, token_state),
        "error": dict(details),
    }


def _google_oauth_config_missing_error(*, adapter_name: str, action: str) -> Dict[str, Any]:
    return {
        "status": "error",
        "code": "config_missing",
        "message": f"{adapter_name} missing OAuth metadata for action={action}",
        "metadata": {
            "provider": adapter_name,
            "required_fields": ["client_id", "client_secret", "project_id", "redirect_uris"],
            "resolution": "provide oauth_credentials payload or GOOGLE_OAUTH_CREDENTIAL_ID / file path",
        },
    }


def _stable_hash(value: Mapping[str, Any], *, length: int = 16) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")).hexdigest()[:length]


def _normalize_status(value: Any, *, status_map: Optional[Mapping[str, str]] = None) -> str:
    mapping = dict(_GLOBAL_STATUS_MAP | (status_map or {}))
    normalized = _as_text(value, default=_STATUS_FALLBACK).lower()
    mapped = mapping.get(normalized, normalized)
    return mapped if mapped in _HUD_STORE_STATUSES else _STATUS_FALLBACK


def _normalize_hud_record(
    payload: Mapping[str, Any],
    *,
    adapter_name: str,
    source_type: str,
    status_map: Optional[Mapping[str, str]] = None,
    default_status: str = "ingest",
) -> HUDStoreRecord:
    payload_json = _coerce_payload(payload)
    intent = _as_text(payload_json.get("intent"), default=default_status) or default_status
    return {
        "internal_id": _as_text(payload_json.get("internal_id") or payload_json.get("item_id"))
        or f"{adapter_name}:{_stable_hash({'adapter': adapter_name, 'intent': intent})}",
        "google_id": _as_text(payload_json.get("google_id"), default=None),
        "external_id": _as_text(payload_json.get("external_id"), default=None),
        "actor": _as_text(payload_json.get("actor"), default=None),
        "intent": intent,
        "scope": _as_text(payload_json.get("scope"), default="today") or "today",
        "payload_json": payload_json,
        "sync_hash": _as_text(payload_json.get("sync_hash"), default=None),
        "status": _normalize_status(payload_json.get("status"), status_map=status_map),
        "retry_count": _coerce_int(payload_json.get("retry_count"), default=0),
        "last_error": _as_text(payload_json.get("last_error"), default=None),
        "priority_class": _as_text(payload_json.get("priority_class") or payload_json.get("priority"), default="normal"),
        "idempotency_key": _as_text(payload_json.get("idempotency_key") or payload_json.get("idempotency"), default=None),
        "created_at": _as_text(payload_json.get("created_at"), default=_DEFAULT_CREATED_AT),
        "updated_at": _as_text(payload_json.get("updated_at"), default=_DEFAULT_CREATED_AT),
        "approved_by": _as_text(payload_json.get("approved_by"), default=None),
        "reviewed_by": _as_text(payload_json.get("reviewed_by"), default=None),
        "next_run_at": _as_text(payload_json.get("next_run_at"), default=None),
        "source_type": source_type,
        "source_ref": _as_text(payload_json.get("source_ref") or payload_json.get("role_id") or payload_json.get("goal_id") or payload_json.get("event_id") or payload_json.get("task_id") or payload_json.get("external_id"), default=None),
        "worker_ref": _as_text(payload_json.get("worker_ref"), default=None),
        "queue_rank": _coerce_int(payload_json.get("queue_rank"), default=0),
    }


class BaseHUDAdapter:
    adapter_name = "base"
    source_type = "base"
    status_map: Mapping[str, str] = _GLOBAL_STATUS_MAP
    placeholder_endpoints: Mapping[str, str] = {"default": "v1://hud-adapters/base", "upsert": "v1://hud-adapters/base/upsert", "sync": "v1://hud-adapters/base/sync"}

    def __init__(self, *, allow_writes: bool = False):
        self.allow_writes = bool(allow_writes)

    @property
    def endpoints(self) -> Dict[str, str]:
        return dict(self.placeholder_endpoints)

    async def upsert_item(self, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        record = await self.to_internal_record(_coerce_payload(payload), intent="upsert")
        return {"status": "ok", "adapter": self.adapter_name, "mode": "placeholder", "endpoint": self.endpoints.get("upsert"), "record": record}

    async def sync_state(self, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        normalized = _coerce_payload(payload)
        kind = _as_text(normalized.get("kind"), default="default")
        return {"status": "ok", "adapter": self.adapter_name, "mode": "placeholder", "kind": kind, "endpoint": self.endpoints.get("sync"), "items": [], "count": 0, "sync_token": await self.build_sync_token({"adapter": self.adapter_name, "kind": kind})}

    async def build_sync_token(self, payload: Optional[Mapping[str, Any]] = None) -> str:
        return f"{self.adapter_name}:{_stable_hash({'adapter': self.adapter_name, **_coerce_payload(payload)})}"

    async def to_internal_record(self, payload: Optional[Mapping[str, Any]] = None, *, intent: str = "ingest") -> HUDStoreRecord:
        return _normalize_hud_record(_coerce_payload(payload), adapter_name=self.adapter_name, source_type=self.source_type, status_map=self.status_map, default_status=intent)

    async def _build_read_placeholder(self, *, kind: str, values: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
        return {"status": "ok", "mode": "read_only_stub", "adapter": self.adapter_name, "kind": kind, "count": len(values), "items": [dict(item) for item in values], "endpoint": self.endpoints.get(kind, self.endpoints.get("default"))}

    def _write_blocked(self, *, action: str) -> Dict[str, Any]:
        return {"status": "blocked", "code": "write_disabled", "message": f"{self.adapter_name} write disabled in v1 for action={action}"}


def _obsidian_vault_root() -> Path:
    """Resolve the Obsidian AdventedHUD vault root (overridable for tests)."""
    env_root = os.environ.get("HUD_OBSIDIAN_VAULT_ROOT")
    if env_root:
        return Path(env_root).expanduser()
    return Path(__file__).resolve().parents[3] / "data" / "obsidian" / "AdventedHUD"


class ObsidianAdapter(BaseHUDAdapter):
    adapter_name = "obsidian"
    source_type = "obsidian"
    placeholder_endpoints = {"roles": "v1://adapters/obsidian/roles", "goals": "v1://adapters/obsidian/goals", "matrix": "v1://adapters/obsidian/matrix", "upsert": "v1://adapters/obsidian/item", "delete": "v1://adapters/obsidian/item", "sync": "v1://adapters/obsidian/sync"}
    status_map = {**_GLOBAL_STATUS_MAP, "open": "pending", "active": "queued", "stale": "failed"}
    _SEEDS = {"roles": ({"id": "role:owner", "name": "owner", "matrix": "matrix:core"}, {"id": "role:editor", "name": "editor", "matrix": "matrix:core"}), "goals": ({"id": "goal:ship", "name": "ship", "state": "active"},), "matrix": ({"id": "matrix:core", "name": "core", "description": "primary"}, {"id": "matrix:review", "name": "review", "description": "quality"})}

    async def upsert_item(self, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        normalized = _coerce_payload(payload)
        record = await self.to_internal_record(normalized, intent="upsert")
        if not self.allow_writes:
            return {**self._write_blocked(action="obsidian.upsert"), "mode": "write_guard", "adapter": self.adapter_name, "endpoint": self.endpoints.get("upsert"), "record": record}
        if _normalize_status(normalized.get("status"), status_map=self.status_map) != "approved":
            return {
                "status": "blocked",
                "code": "write_not_approved",
                "message": "obsidian upsert requires approved status",
                "mode": "write_guard",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("upsert"),
                "record": record,
            }

        vault_root = _obsidian_vault_root()
        folder_hint = _safe_fragment(normalized.get("folder") or normalized.get("directory") or record.get("scope"), allow_path=True)
        path_hint = _safe_fragment(normalized.get("file_path") or normalized.get("path") or normalized.get("relative_path"), allow_path=True)
        filename = _safe_fragment(normalized.get("filename") or normalized.get("file_name") or normalized.get("name"), allow_path=False)
        if path_hint:
            path_parts = [part for part in path_hint.split("/") if part]
            if path_parts and path_parts[-1].lower().endswith(".md"):
                filename = filename or path_parts[-1]
                folder_hint = "/".join(path_parts[:-1]) or folder_hint
            elif not folder_hint:
                folder_hint = path_hint

        if not filename:
            created_at = _as_text(record.get("created_at"), default=None) or datetime.now(timezone.utc).isoformat()
            filename = f"{record['intent']}_{''.join(ch for ch in created_at if ch.isdigit())}_{_stable_hash(record['payload_json'])}.md"
        if not filename.lower().endswith(".md"):
            filename = f"{filename}.md"

        target_dir = (vault_root / (folder_hint or "inbox")).resolve()
        file_path = target_dir / filename
        repo_root = Path(__file__).resolve().parents[3]
        relative_path = str(file_path.relative_to(repo_root))
        if not str(file_path).startswith(str(vault_root)):
            return {**self._write_blocked(action="obsidian.upsert"), "mode": "write_guard", "adapter": self.adapter_name, "endpoint": self.endpoints.get("upsert"), "record": record, "write_status": "blocked", "path_hint": path_hint, "relative_path": relative_path}

        markdown_source = _as_text(normalized.get("markdown") or normalized.get("content") or normalized.get("body") or normalized.get("text") or normalized.get("note"), default=None)
        if markdown_source is None and isinstance(normalized.get("payload"), Mapping):
            nested = normalized.get("payload", {})
            markdown_source = _as_text(nested.get("markdown") or nested.get("content") or nested.get("body") or nested.get("text") or nested.get("note"), default=None)
        if markdown_source is None:
            markdown_source = json.dumps(normalized, ensure_ascii=False, indent=2)

        frontmatter = {
            "title": _as_text(normalized.get("title"), default=record["internal_id"]),
            "source": _as_text(normalized.get("source"), default=self.source_type),
            "scope": _as_text(record.get("scope"), default="today"),
            "created_at": _as_text(record.get("created_at"), default=_DEFAULT_CREATED_AT),
            "status": _as_text(record.get("status"), default=_STATUS_FALLBACK),
            "intent": _as_text(record.get("intent"), default="upsert"),
            "item_id": _as_text(record.get("internal_id"), default="item"),
        }
        content = "---\n" + "\n".join([f"{key}: {json.dumps(value)}" for key, value in frontmatter.items()]) + "\n---\n\n" + markdown_source

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            already_present = file_path.exists()
            file_path.write_text(content, encoding="utf-8")
            return {"status": "ok", "mode": "write", "adapter": self.adapter_name, "endpoint": self.endpoints.get("upsert"), "record": record, "write_status": "updated" if already_present else "created", "file_path": str(file_path), "relative_path": relative_path, "path_hint": str(Path(folder_hint) / filename) if folder_hint else filename}
        except (OSError, TypeError, ValueError) as exc:
            return {**self._write_blocked(action="obsidian.upsert"), "mode": "write_error", "adapter": self.adapter_name, "endpoint": self.endpoints.get("upsert"), "record": record, "write_status": "failed", "error": str(exc), "path_hint": path_hint, "relative_path": relative_path}

    async def delete_item(self, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        """Delete (or archive) an Obsidian note using its stored path.

        The stored ``relative_path`` is repo-root relative (e.g.
        ``data/obsidian/AdventedHUD/inbox/foo.md``). By default the note is
        moved to ``<vault>/_trash/...`` (reversible archive); pass
        ``archive: false`` to hard-delete. Already-missing notes are reported
        as ``already_gone`` (idempotent).
        """
        normalized = _unwrap_payload_value(_coerce_payload(payload))
        record = await self.to_internal_record(normalized, intent="delete")
        if not self.allow_writes:
            return {**self._write_blocked(action="obsidian.delete"), "mode": "write_guard", "adapter": self.adapter_name, "endpoint": self.endpoints.get("delete"), "record": record}

        vault_root = _obsidian_vault_root()
        repo_root = Path(__file__).resolve().parents[3]
        path_hint = _safe_fragment(
            normalized.get("relative_path")
            or normalized.get("file_path")
            or normalized.get("path")
            or normalized.get("note_path"),
            allow_path=True,
        )
        if not path_hint:
            return {
                "status": "error",
                "code": "missing_path",
                "message": "obsidian.delete requires relative_path or file_path",
                "mode": "delete",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("delete"),
                "record": record,
            }

        vault_root_str = str(vault_root)
        candidate = None
        if path_hint:
            repo_candidate = (repo_root / path_hint).resolve()
            if str(repo_candidate) == vault_root_str or str(repo_candidate).startswith(vault_root_str + os.sep):
                candidate = repo_candidate
            else:
                vault_candidate = (vault_root / path_hint).resolve()
                if str(vault_candidate) == vault_root_str or str(vault_candidate).startswith(vault_root_str + os.sep):
                    candidate = vault_candidate
        if candidate is None:
            return {
                **self._write_blocked(action="obsidian.delete"),
                "mode": "write_guard",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("delete"),
                "record": record,
                "write_status": "blocked",
                "path_hint": path_hint,
                "message": "refusing to delete a note outside the AdventedHUD vault",
            }

        if not candidate.is_file():
            return {
                "status": "ok",
                "mode": "delete",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("delete"),
                "record": record,
                "write_status": "already_gone",
                "file_path": str(candidate),
                "relative_path": path_hint,
            }

        raw_archive = _as_text(normalized.get("archive"), default=None)
        archive_note = raw_archive is None or raw_archive.strip().lower() not in {"0", "false", "no", "off"}
        try:
            if archive_note:
                archive_dir = (vault_root / "_trash" / Path(path_hint).parent).resolve()
                archive_dir.mkdir(parents=True, exist_ok=True)
                archive_target = archive_dir / Path(path_hint).name
                archive_target.write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")
                candidate.unlink()
                return {
                    "status": "ok",
                    "mode": "delete",
                    "adapter": self.adapter_name,
                    "endpoint": self.endpoints.get("delete"),
                    "record": record,
                    "write_status": "archived",
                    "file_path": str(candidate),
                    "relative_path": path_hint,
                    "archive_path": str(archive_target),
                }
            candidate.unlink()
            return {
                "status": "ok",
                "mode": "delete",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("delete"),
                "record": record,
                "write_status": "deleted",
                "file_path": str(candidate),
                "relative_path": path_hint,
            }
        except OSError as exc:
            return {
                "status": "error",
                "code": "delete_failed",
                "message": f"obsidian.delete failed: {exc}",
                "mode": "delete",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("delete"),
                "record": record,
                "write_status": "failed",
                "error": str(exc),
                "path_hint": path_hint,
                "relative_path": path_hint,
            }

    async def sync_state(self, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        kind = _as_text(_coerce_payload(payload).get("kind"), default="roles").strip().lower()
        if kind in self._SEEDS:
            return await self._build_read_placeholder(kind=kind, values=self._SEEDS[kind])
        if kind == "role" or kind == "vault_roles":
            kind = "roles"
        elif kind == "goal" or kind == "vault_goals":
            kind = "goals"
        elif kind in {"matrices", "matrix_graph"}:
            kind = "matrix"
        if kind in self._SEEDS:
            return await self._build_read_placeholder(kind=kind, values=self._SEEDS[kind])
        merged = self._SEEDS["roles"] + self._SEEDS["goals"] + self._SEEDS["matrix"]
        return await self._build_read_placeholder(kind=kind, values=merged)

    async def build_sync_token(self, payload: Optional[Mapping[str, Any]] = None) -> str:
        kind = _as_text(_coerce_payload(payload).get("kind"), default="all")
        return f"{self.adapter_name}:{kind}:{_stable_hash({'kind': kind, 'count': len(self._SEEDS['roles']) + len(self._SEEDS['goals']) + len(self._SEEDS['matrix'])})}"


class GoogleCalendarAdapter(BaseHUDAdapter):
    adapter_name = "gcal"
    source_type = "google_calendar"
    placeholder_endpoints = {"events": "v1://adapters/google-calendar/events", "upsert": "v1://adapters/google-calendar/upsert", "delete": "v1://adapters/google-calendar/events", "sync": "v1://adapters/google-calendar/sync"}
    status_map = {**_GLOBAL_STATUS_MAP, "confirmed": "approved", "tentative": "queued", "needsAction": "pending", "accepted": "approved", "declined": "rejected", "cancelled": "rejected"}
    _EVENT_SEED = ({"event_id": "evt:standup", "title": "Standup", "calendar": "primary", "status": "confirmed"}, {"event_id": "evt:shipping", "title": "Shipping", "calendar": "primary", "status": "tentative"})

    def list_calendars(self, payload: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Return the user's actual Google Calendars."""
        normalized = _coerce_payload(payload or {})
        oauth_metadata, oauth_path = _resolve_google_oauth_metadata_with_details(normalized)
        if oauth_metadata is None:
            return {"calendars": [], "error": "no_oauth"}

        token_context = _resolve_google_token_context(normalized, oauth_metadata_path=oauth_path)
        access_token, _, _, access_error = _google_ensure_access_token(
            adapter_name=self.adapter_name,
            action="gcal.list_calendars",
            metadata=oauth_metadata,
            token_context=token_context,
        )
        if access_error or not access_token:
            return {"calendars": [], "error": "auth_failed"}

        try:
            url = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
            headers = {"Authorization": f"Bearer {access_token}"}
            import urllib.request, json
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                cals = [
                    {"id": item.get("id"), "summary": item.get("summary")}
                    for item in data.get("items", [])
                ]
                return {"calendars": cals, "error": None}
        except Exception as e:
            return {"calendars": [], "error": str(e)}

    async def upsert_item(self, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        normalized = _unwrap_payload_value(_coerce_payload(payload))
        oauth_metadata, oauth_path = _resolve_google_oauth_metadata_with_details(normalized)
        if oauth_metadata is None:
            return _google_oauth_config_missing_error(adapter_name=self.adapter_name, action="gcal.upsert")
        token_context = _resolve_google_token_context(normalized, oauth_metadata_path=oauth_path)
        token_payload = token_context.get("token")
        token_state = _google_oauth_token_state(
            _normalize_google_token_payload(token_payload) if isinstance(token_payload, Mapping) else None,
            token_source=_as_text(token_context.get("source"), default="missing"),
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, token_state)
        record = await self.to_internal_record(_sanitize_google_payload(normalized), intent="upsert")
        if not self.allow_writes:
            return {
                **self._write_blocked(action="google_calendar.upsert"),
                "mode": "write_guard",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("upsert"),
                "record": record,
                "metadata": metadata_response,
            }

        if _normalize_status(normalized.get("status"), status_map=self.status_map) != "approved":
            return {
                "status": "blocked",
                "code": "write_not_approved",
                "message": "google_calendar upsert requires approved status",
                "mode": "write_guard",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("upsert"),
                "record": record,
                "metadata": metadata_response,
            }

        access_token, _, resolved_token_state, access_error = _google_ensure_access_token(
            adapter_name=self.adapter_name,
            action="gcal.upsert",
            metadata=oauth_metadata,
            token_context=token_context,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if access_error is not None:
            if _as_text(access_error.get("code"), default="error") == "config_missing":
                return _google_oauth_config_missing_error(adapter_name=self.adapter_name, action="gcal.upsert")
            return _google_api_error_payload(self.adapter_name, "gcal.upsert", metadata=oauth_metadata, token_state=resolved_token_state, details=access_error)
        if access_token is None:
            return _google_api_error_payload(self.adapter_name, "gcal.upsert", metadata=oauth_metadata, token_state=resolved_token_state, details={"code": "auth_required", "message": "No access token available"})

        calendar_id = _as_text(normalized.get("calendar_id"), default="primary")
        event_id = _as_text(normalized.get("event_id"), default=_as_text(normalized.get("external_id"), default=None))
        request_body = _google_event_payload_from_input(normalized)
        if not request_body:
            return {
                "status": "error",
                "code": "error",
                "message": "gcal.upsert missing event payload",
                "metadata": metadata_response,
            }

        # Shape validation (Google Calendar API contract)
        _cal_start = request_body.get("start")
        _cal_end = request_body.get("end")
        if not _cal_start or not _cal_end:
            return {
                "status": "error",
                "code": "validation_error",
                "message": "gcal.upsert requires both start and end",
                "metadata": metadata_response,
            }
        _cal_has_dt = "dateTime" in _cal_start
        _cal_has_d = "date" in _cal_start
        if not _cal_has_dt and not _cal_has_d:
            return {
                "status": "error",
                "code": "validation_error",
                "message": "gcal.upsert start must have date or dateTime",
                "metadata": metadata_response,
            }
        if _cal_has_dt and "dateTime" not in _cal_end:
            return {
                "status": "error",
                "code": "validation_error",
                "message": "gcal.upsert end must use dateTime when start uses dateTime",
                "metadata": metadata_response,
            }
        if _cal_has_d and "date" not in _cal_end:
            return {
                "status": "error",
                "code": "validation_error",
                "message": "gcal.upsert end must use date when start uses date",
                "metadata": metadata_response,
            }
        encoded_calendar_id = urllib.parse.quote(_as_text(calendar_id, default="primary"), safe="")
        if event_id is not None:
            request_url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_calendar_id}/events/{urllib.parse.quote(event_id, safe='')}"
            method = "PATCH"
            write_status = "updated"
        else:
            request_url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_calendar_id}/events"
            method = "POST"
            write_status = "created"

        response_body, api_error, resolved_token_state = _google_api_request_json_with_refresh(
            method=method,
            url=request_url,
            metadata=oauth_metadata,
            token_context=token_context,
            token_state=resolved_token_state,
            adapter_name=self.adapter_name,
            action="gcal.upsert",
            access_token=access_token,
            payload=request_body,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if api_error is not None or response_body is None:
            return _google_api_error_payload(self.adapter_name, "gcal.upsert", metadata=oauth_metadata, token_state=resolved_token_state, details=api_error or {"code": "error", "message": "No payload returned from api"})

        external_id = _as_text(response_body.get("id"), default=event_id)
        return {
            "status": "ok",
            "mode": "write",
            "adapter": self.adapter_name,
            "endpoint": self.endpoints.get("upsert"),
            "record": record,
            "metadata": metadata_response,
            "write_status": write_status,
            "external_id": external_id,
            "google_id": external_id,
            "calendar_id": _as_text(calendar_id, default="primary"),
            "event": _google_calendar_item_from_api(response_body, calendar_id=_as_text(calendar_id, default="primary")),
        }

    async def delete_item(self, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        """Delete a Google Calendar event by its stored external id.

        Requires ``event_id`` / ``external_id`` / ``google_id`` (from the
        original upsert write-back) and ``calendar_id``. A 404 from Google is
        treated as ``already_gone`` (idempotent success).
        """
        normalized = _unwrap_payload_value(_coerce_payload(payload))
        oauth_metadata, oauth_path = _resolve_google_oauth_metadata_with_details(normalized)
        if oauth_metadata is None:
            return _google_oauth_config_missing_error(adapter_name=self.adapter_name, action="gcal.delete")
        token_context = _resolve_google_token_context(normalized, oauth_metadata_path=oauth_path)
        token_payload = token_context.get("token")
        token_state = _google_oauth_token_state(
            _normalize_google_token_payload(token_payload) if isinstance(token_payload, Mapping) else None,
            token_source=_as_text(token_context.get("source"), default="missing"),
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, token_state)
        record = await self.to_internal_record(_sanitize_google_payload(normalized), intent="delete")
        if not self.allow_writes:
            return {
                **self._write_blocked(action="google_calendar.delete"),
                "mode": "write_guard",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("delete"),
                "record": record,
                "metadata": metadata_response,
            }

        event_id = _as_text(
            normalized.get("event_id")
            or normalized.get("external_id")
            or normalized.get("google_id")
        )
        if event_id is None:
            return {
                "status": "error",
                "code": "missing_external_id",
                "message": "gcal.delete requires event_id/external_id (stored from the original projection)",
                "mode": "delete",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("delete"),
                "record": record,
                "metadata": metadata_response,
            }
        calendar_id = _as_text(normalized.get("calendar_id"), default="primary")

        access_token, _, resolved_token_state, access_error = _google_ensure_access_token(
            adapter_name=self.adapter_name,
            action="gcal.delete",
            metadata=oauth_metadata,
            token_context=token_context,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if access_error is not None:
            if _as_text(access_error.get("code"), default="error") == "config_missing":
                return _google_oauth_config_missing_error(adapter_name=self.adapter_name, action="gcal.delete")
            return _google_api_error_payload(self.adapter_name, "gcal.delete", metadata=oauth_metadata, token_state=resolved_token_state, details=access_error)
        if access_token is None:
            return _google_api_error_payload(self.adapter_name, "gcal.delete", metadata=oauth_metadata, token_state=resolved_token_state, details={"code": "auth_required", "message": "No access token available"})

        encoded_calendar_id = urllib.parse.quote(_as_text(calendar_id, default="primary"), safe="")
        request_url = f"https://www.googleapis.com/calendar/v3/calendars/{encoded_calendar_id}/events/{urllib.parse.quote(event_id, safe='')}"
        response_body, api_error, resolved_token_state = _google_api_request_json_with_refresh(
            method="DELETE",
            url=request_url,
            metadata=oauth_metadata,
            token_context=token_context,
            token_state=resolved_token_state,
            adapter_name=self.adapter_name,
            action="gcal.delete",
            access_token=access_token,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if api_error is not None:
            if str(api_error.get("code")) == "404":
                return {
                    "status": "ok",
                    "mode": "delete",
                    "adapter": self.adapter_name,
                    "endpoint": self.endpoints.get("delete"),
                    "record": record,
                    "metadata": metadata_response,
                    "write_status": "already_gone",
                    "external_id": event_id,
                    "google_id": event_id,
                    "calendar_id": _as_text(calendar_id, default="primary"),
                }
            return _google_api_error_payload(self.adapter_name, "gcal.delete", metadata=oauth_metadata, token_state=resolved_token_state, details=api_error)

        return {
            "status": "ok",
            "mode": "delete",
            "adapter": self.adapter_name,
            "endpoint": self.endpoints.get("delete"),
            "record": record,
            "metadata": metadata_response,
            "write_status": "deleted",
            "external_id": event_id,
            "google_id": event_id,
            "calendar_id": _as_text(calendar_id, default="primary"),
        }

    async def sync_state(self, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        normalized = _coerce_payload(payload)
        oauth_metadata, oauth_path = _resolve_google_oauth_metadata_with_details(normalized)
        if oauth_metadata is None:
            return _google_oauth_config_missing_error(adapter_name=self.adapter_name, action="gcal.sync")
        token_context = _resolve_google_token_context(normalized, oauth_metadata_path=oauth_path)
        token_payload = token_context.get("token")
        token_state = _google_oauth_token_state(
            _normalize_google_token_payload(token_payload) if isinstance(token_payload, Mapping) else None,
            token_source=_as_text(token_context.get("source"), default="missing"),
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, token_state)
        calendar_id = _as_text(normalized.get("calendar_id"), default="primary")

        if not isinstance(token_context.get("token"), Mapping):
            result = await self._build_read_placeholder(kind="events", values=self._EVENT_SEED)
            result["mode"] = "read_only_stub"
            result["metadata"] = metadata_response
            result["calendar_id"] = calendar_id
            result["sync_cursor"] = None
            return result

        access_token, _, resolved_token_state, access_error = _google_ensure_access_token(
            adapter_name=self.adapter_name,
            action="gcal.sync",
            metadata=oauth_metadata,
            token_context=token_context,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if access_error is not None or access_token is None:
            return _google_api_error_payload(self.adapter_name, "gcal.sync", metadata=oauth_metadata, token_state=resolved_token_state, details=access_error or {"code": "auth_required", "message": "No access token available"})

        now = datetime.now(timezone.utc)
        time_min = _as_text(normalized.get("time_min"), default=None)
        time_max = _as_text(normalized.get("time_max"), default=None)
        if time_min is None and time_max is None:
            days = _coerce_int(normalized.get("days"), default=0)
            if days > 0:
                time_min = _to_utc_iso(now)
                time_max = _to_utc_iso(now + timedelta(days=days))
        query: Dict[str, Any] = {"maxResults": 50, "orderBy": "startTime", "singleEvents": "true"}
        if time_min is not None:
            query["timeMin"] = _to_utc_iso(time_min) or time_min
        if time_max is not None:
            query["timeMax"] = _to_utc_iso(time_max) or time_max
        encoded_calendar_id = urllib.parse.quote(_as_text(calendar_id, default="primary"), safe="")
        response_body, api_error, resolved_token_state = _google_api_request_json_with_refresh(
            method="GET",
            url=f"https://www.googleapis.com/calendar/v3/calendars/{encoded_calendar_id}/events",
            metadata=oauth_metadata,
            token_context=token_context,
            token_state=resolved_token_state,
            adapter_name=self.adapter_name,
            action="gcal.sync",
            access_token=access_token,
            query=query,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if api_error is not None or response_body is None:
            return _google_api_error_payload(self.adapter_name, "gcal.sync", metadata=oauth_metadata, token_state=resolved_token_state, details=api_error or {"code": "error", "message": "No payload returned from api"})

        items = response_body.get("items", [])
        synced_items = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping):
                    synced_items.append(_google_calendar_item_from_api(item, calendar_id=_as_text(calendar_id, default="primary")))
        sync_cursor = _as_text(response_body.get("nextSyncToken"), default=_as_text(response_body.get("nextPageToken"), default=None))
        return {
            "status": "ok",
            "mode": "read_live",
            "adapter": self.adapter_name,
            "kind": "events",
            "endpoint": self.endpoints.get("sync"),
            "count": len(synced_items),
            "items": synced_items,
            "metadata": metadata_response,
            "calendar_id": _as_text(calendar_id, default="primary"),
            "sync_cursor": sync_cursor,
        }

    async def build_sync_token(self, payload: Optional[Mapping[str, Any]] = None) -> str:
        return f"{self.adapter_name}:{_stable_hash({'calendar': _coerce_payload(payload).get('calendar'), 'count': len(self._EVENT_SEED)}, length=24)}"


class GoogleTasksAdapter(BaseHUDAdapter):
    adapter_name = "gtasks"
    source_type = "google_tasks"
    placeholder_endpoints = {"tasks": "v1://adapters/google-tasks/tasks", "upsert": "v1://adapters/google-tasks/upsert", "delete": "v1://adapters/google-tasks/tasks", "sync": "v1://adapters/google-tasks/sync"}
    status_map = {**_GLOBAL_STATUS_MAP, "needsAction": "pending", "needs_action": "pending", "in_progress": "pending", "completed": "approved"}
    _TASK_SEED = ({"task_id": "task:seed-1", "title": "Review architecture doc", "status": "needsAction"}, {"task_id": "task:seed-2", "title": "Finish test harness", "status": "completed"})

    def list_task_lists(self, payload: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
        """Return the user's actual Google Task lists."""
        normalized = _coerce_payload(payload or {})
        oauth_metadata, oauth_path = _resolve_google_oauth_metadata_with_details(normalized)
        if oauth_metadata is None:
            return {"task_lists": [], "error": "no_oauth"}

        token_context = _resolve_google_token_context(normalized, oauth_metadata_path=oauth_path)
        access_token, _, _, access_error = _google_ensure_access_token(
            adapter_name=self.adapter_name,
            action="gtasks.list_task_lists",
            metadata=oauth_metadata,
            token_context=token_context,
        )
        if access_error or not access_token:
            return {"task_lists": [], "error": "auth_failed"}

        try:
            url = "https://www.googleapis.com/tasks/v1/users/@me/lists"
            headers = {"Authorization": f"Bearer {access_token}"}
            import urllib.request, json
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
                lists = [
                    {"id": item.get("id"), "title": item.get("title")}
                    for item in data.get("items", [])
                ]
                return {"task_lists": lists, "error": None}
        except Exception as e:
            return {"task_lists": [], "error": str(e)}

    async def upsert_item(self, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        normalized = _unwrap_payload_value(_coerce_payload(payload))
        oauth_metadata, oauth_path = _resolve_google_oauth_metadata_with_details(normalized)
        if oauth_metadata is None:
            return _google_oauth_config_missing_error(adapter_name=self.adapter_name, action="gtasks.upsert")
        token_context = _resolve_google_token_context(normalized, oauth_metadata_path=oauth_path)
        token_payload = token_context.get("token")
        token_state = _google_oauth_token_state(
            _normalize_google_token_payload(token_payload) if isinstance(token_payload, Mapping) else None,
            token_source=_as_text(token_context.get("source"), default="missing"),
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, token_state)
        record = await self.to_internal_record(_sanitize_google_payload(normalized), intent="upsert")
        if not self.allow_writes:
            return {
                **self._write_blocked(action="google_tasks.upsert"),
                "mode": "write_guard",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("upsert"),
                "record": record,
                "metadata": metadata_response,
            }

        if _normalize_status(normalized.get("status"), status_map=self.status_map) != "approved":
            return {
                "status": "blocked",
                "code": "write_not_approved",
                "message": "google_tasks upsert requires approved status",
                "mode": "write_guard",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("upsert"),
                "record": record,
                "metadata": metadata_response,
            }

        access_token, _, resolved_token_state, access_error = _google_ensure_access_token(
            adapter_name=self.adapter_name,
            action="gtasks.upsert",
            metadata=oauth_metadata,
            token_context=token_context,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if access_error is not None:
            if _as_text(access_error.get("code"), default="error") == "config_missing":
                return _google_oauth_config_missing_error(adapter_name=self.adapter_name, action="gtasks.upsert")
            return _google_api_error_payload(self.adapter_name, "gtasks.upsert", metadata=oauth_metadata, token_state=resolved_token_state, details=access_error)
        if access_token is None:
            return _google_api_error_payload(self.adapter_name, "gtasks.upsert", metadata=oauth_metadata, token_state=resolved_token_state, details={"code": "auth_required", "message": "No access token available"})

        tasklist_id = _as_text(normalized.get("tasklist_id"), default="@default")
        request_body = _google_task_payload_from_input(normalized)
        if not request_body:
            return {
                "status": "error",
                "code": "error",
                "message": "gtasks.upsert missing task payload",
                "metadata": metadata_response,
            }

        encoded_tasklist_id = urllib.parse.quote(_as_text(tasklist_id, default="@default"), safe="")
        task_id = _as_text(normalized.get("task_id"), default=_as_text(normalized.get("external_id"), default=None))
        if task_id is not None:
            request_url = f"https://www.googleapis.com/tasks/v1/lists/{encoded_tasklist_id}/tasks/{urllib.parse.quote(task_id, safe='')}"
            method = "PATCH"
            write_status = "updated"
        else:
            request_url = f"https://www.googleapis.com/tasks/v1/lists/{encoded_tasklist_id}/tasks"
            method = "POST"
            write_status = "created"
        response_body, api_error, resolved_token_state = _google_api_request_json_with_refresh(
            method=method,
            url=request_url,
            metadata=oauth_metadata,
            token_context=token_context,
            token_state=resolved_token_state,
            adapter_name=self.adapter_name,
            action="gtasks.upsert",
            access_token=access_token,
            payload=request_body,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if api_error is not None or response_body is None:
            return _google_api_error_payload(self.adapter_name, "gtasks.upsert", metadata=oauth_metadata, token_state=resolved_token_state, details=api_error or {"code": "error", "message": "No payload returned from api"})

        external_id = _as_text(response_body.get("id"), default=None)
        return {
            "status": "ok",
            "mode": "write",
            "adapter": self.adapter_name,
            "endpoint": self.endpoints.get("upsert"),
            "record": record,
            "metadata": metadata_response,
            "write_status": write_status,
            "external_id": external_id,
            "google_id": external_id,
            "tasklist_id": _as_text(tasklist_id, default="@default"),
        }

    async def delete_item(self, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        """Delete a Google Task by its stored external id.

        Requires ``task_id`` / ``external_id`` / ``google_id`` (from the
        original upsert write-back) and ``tasklist_id``. A 404 from Google is
        treated as ``already_gone`` (idempotent success).
        """
        normalized = _unwrap_payload_value(_coerce_payload(payload))
        oauth_metadata, oauth_path = _resolve_google_oauth_metadata_with_details(normalized)
        if oauth_metadata is None:
            return _google_oauth_config_missing_error(adapter_name=self.adapter_name, action="gtasks.delete")
        token_context = _resolve_google_token_context(normalized, oauth_metadata_path=oauth_path)
        token_payload = token_context.get("token")
        token_state = _google_oauth_token_state(
            _normalize_google_token_payload(token_payload) if isinstance(token_payload, Mapping) else None,
            token_source=_as_text(token_context.get("source"), default="missing"),
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, token_state)
        record = await self.to_internal_record(_sanitize_google_payload(normalized), intent="delete")
        if not self.allow_writes:
            return {
                **self._write_blocked(action="google_tasks.delete"),
                "mode": "write_guard",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("delete"),
                "record": record,
                "metadata": metadata_response,
            }

        task_id = _as_text(
            normalized.get("task_id")
            or normalized.get("external_id")
            or normalized.get("google_id")
        )
        if task_id is None:
            return {
                "status": "error",
                "code": "missing_external_id",
                "message": "gtasks.delete requires task_id/external_id (stored from the original projection)",
                "mode": "delete",
                "adapter": self.adapter_name,
                "endpoint": self.endpoints.get("delete"),
                "record": record,
                "metadata": metadata_response,
            }
        tasklist_id = _as_text(normalized.get("tasklist_id"), default="@default")

        access_token, _, resolved_token_state, access_error = _google_ensure_access_token(
            adapter_name=self.adapter_name,
            action="gtasks.delete",
            metadata=oauth_metadata,
            token_context=token_context,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if access_error is not None:
            if _as_text(access_error.get("code"), default="error") == "config_missing":
                return _google_oauth_config_missing_error(adapter_name=self.adapter_name, action="gtasks.delete")
            return _google_api_error_payload(self.adapter_name, "gtasks.delete", metadata=oauth_metadata, token_state=resolved_token_state, details=access_error)
        if access_token is None:
            return _google_api_error_payload(self.adapter_name, "gtasks.delete", metadata=oauth_metadata, token_state=resolved_token_state, details={"code": "auth_required", "message": "No access token available"})

        encoded_tasklist_id = urllib.parse.quote(_as_text(tasklist_id, default="@default"), safe="")
        request_url = f"https://www.googleapis.com/tasks/v1/lists/{encoded_tasklist_id}/tasks/{urllib.parse.quote(task_id, safe='')}"
        response_body, api_error, resolved_token_state = _google_api_request_json_with_refresh(
            method="DELETE",
            url=request_url,
            metadata=oauth_metadata,
            token_context=token_context,
            token_state=resolved_token_state,
            adapter_name=self.adapter_name,
            action="gtasks.delete",
            access_token=access_token,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if api_error is not None:
            if str(api_error.get("code")) == "404":
                return {
                    "status": "ok",
                    "mode": "delete",
                    "adapter": self.adapter_name,
                    "endpoint": self.endpoints.get("delete"),
                    "record": record,
                    "metadata": metadata_response,
                    "write_status": "already_gone",
                    "external_id": task_id,
                    "google_id": task_id,
                    "tasklist_id": _as_text(tasklist_id, default="@default"),
                }
            return _google_api_error_payload(self.adapter_name, "gtasks.delete", metadata=oauth_metadata, token_state=resolved_token_state, details=api_error)

        return {
            "status": "ok",
            "mode": "delete",
            "adapter": self.adapter_name,
            "endpoint": self.endpoints.get("delete"),
            "record": record,
            "metadata": metadata_response,
            "write_status": "deleted",
            "external_id": task_id,
            "google_id": task_id,
            "tasklist_id": _as_text(tasklist_id, default="@default"),
        }

    async def sync_state(self, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        normalized = _coerce_payload(payload)
        oauth_metadata, oauth_path = _resolve_google_oauth_metadata_with_details(normalized)
        if oauth_metadata is None:
            return _google_oauth_config_missing_error(adapter_name=self.adapter_name, action="gtasks.sync")
        token_context = _resolve_google_token_context(normalized, oauth_metadata_path=oauth_path)
        token_payload = token_context.get("token")
        token_state = _google_oauth_token_state(
            _normalize_google_token_payload(token_payload) if isinstance(token_payload, Mapping) else None,
            token_source=_as_text(token_context.get("source"), default="missing"),
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, token_state)
        tasklist_id = _as_text(normalized.get("tasklist_id"), default="@default")

        if not isinstance(token_context.get("token"), Mapping):
            result = await self._build_read_placeholder(kind="tasks", values=self._TASK_SEED)
            result["mode"] = "read_only_stub"
            result["metadata"] = metadata_response
            result["tasklist_id"] = tasklist_id
            result["sync_cursor"] = None
            return result

        access_token, _, resolved_token_state, access_error = _google_ensure_access_token(
            adapter_name=self.adapter_name,
            action="gtasks.sync",
            metadata=oauth_metadata,
            token_context=token_context,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if access_error is not None or access_token is None:
            return _google_api_error_payload(self.adapter_name, "gtasks.sync", metadata=oauth_metadata, token_state=resolved_token_state, details=access_error or {"code": "auth_required", "message": "No access token available"})

        encoded_tasklist_id = urllib.parse.quote(_as_text(tasklist_id, default="@default"), safe="")
        response_body, api_error, resolved_token_state = _google_api_request_json_with_refresh(
            method="GET",
            url=f"https://www.googleapis.com/tasks/v1/lists/{encoded_tasklist_id}/tasks",
            metadata=oauth_metadata,
            token_context=token_context,
            token_state=resolved_token_state,
            adapter_name=self.adapter_name,
            action="gtasks.sync",
            access_token=access_token,
        )
        metadata_response = _google_oauth_metadata_with_token_state(oauth_metadata, resolved_token_state)
        if api_error is not None or response_body is None:
            return _google_api_error_payload(self.adapter_name, "gtasks.sync", metadata=oauth_metadata, token_state=resolved_token_state, details=api_error or {"code": "error", "message": "No payload returned from api"})

        items = response_body.get("items", [])
        synced_items = []
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping):
                    synced_items.append(_google_task_item_from_api(item, tasklist_id=_as_text(tasklist_id, default="@default")))
        sync_cursor = _as_text(response_body.get("nextPageToken"), default=_as_text(response_body.get("etag"), default=None))
        return {
            "status": "ok",
            "mode": "read_live",
            "adapter": self.adapter_name,
            "kind": "tasks",
            "endpoint": self.endpoints.get("sync"),
            "count": len(synced_items),
            "items": synced_items,
            "metadata": metadata_response,
            "tasklist_id": _as_text(tasklist_id, default="@default"),
            "sync_cursor": sync_cursor,
        }

    async def build_sync_token(self, payload: Optional[Mapping[str, Any]] = None) -> str:
        return f"{self.adapter_name}:{_stable_hash({'list': _coerce_payload(payload).get('list'), 'count': len(self._TASK_SEED)}, length=24)}"


class HUDAdapterHub:
    _PROVIDER_ALIASES = {"obsidian": "obsidian", "calendar": "gcal", "gcal": "gcal", "google_calendar": "gcal", "gtasks": "gtasks", "tasks": "gtasks", "google_tasks": "gtasks"}
    _ACTION_ALIASES = {
        "ingest": ("obsidian", "upsert_item", {}),
        "roles": ("obsidian", "sync_state", {"kind": "roles"}),
        "role": ("obsidian", "sync_state", {"kind": "roles"}),
        "goals": ("obsidian", "sync_state", {"kind": "goals"}),
        "goal": ("obsidian", "sync_state", {"kind": "goals"}),
        "matrix": ("obsidian", "sync_state", {"kind": "matrix"}),
        "sync": ("obsidian", "sync_state", {"kind": "matrix"}),
        "upsert": ("obsidian", "upsert_item", {}),
        "events": ("gcal", "sync_state", {"kind": "events"}),
        "event": ("gcal", "sync_state", {"kind": "events"}),
        "gcal.upsert": ("gcal", "upsert_item", {}),
        "gcal.sync": ("gcal", "sync_state", {"kind": "events"}),
        "gcal.delete": ("gcal", "delete_item", {}),
        "calendar.upsert": ("gcal", "upsert_item", {}),
        "calendar.sync": ("gcal", "sync_state", {"kind": "events"}),
        "calendar.delete": ("gcal", "delete_item", {}),
        "tasks": ("gtasks", "sync_state", {"kind": "tasks"}),
        "task": ("gtasks", "sync_state", {"kind": "tasks"}),
        "gtasks.upsert": ("gtasks", "upsert_item", {}),
        "gtasks.sync": ("gtasks", "sync_state", {"kind": "tasks"}),
        "gtasks.delete": ("gtasks", "delete_item", {}),
        "tasks.delete": ("gtasks", "delete_item", {}),
        "obsidian.delete": ("obsidian", "delete_item", {}),
    }

    def __init__(
        self,
        *,
        obsidian: Optional[BaseHUDAdapter] = None,
        google_calendar: Optional[BaseHUDAdapter] = None,
        google_tasks: Optional[BaseHUDAdapter] = None,
        allow_writes: bool = False,
        allow_google_writes: Optional[bool] = None,
    ):
        google_writes = allow_google_writes if allow_google_writes is not None else allow_writes
        self.adapters = {
            "obsidian": obsidian or ObsidianAdapter(allow_writes=allow_writes),
            "gcal": google_calendar or GoogleCalendarAdapter(allow_writes=google_writes),
            "gtasks": google_tasks or GoogleTasksAdapter(allow_writes=google_writes),
        }

    def _normalize_intent(self, intent: Any) -> str:
        return _as_text(intent, default="").strip().lower().replace("/", ".")

    def _resolve(self, normalized_intent: str, _payload: Dict[str, Any]) -> Optional[tuple[str, str, Dict[str, Any]]]:
        if not normalized_intent:
            return None
        if normalized_intent in self._ACTION_ALIASES:
            return self._ACTION_ALIASES[normalized_intent]
        if "." not in normalized_intent:
            provider = self._PROVIDER_ALIASES.get(normalized_intent, normalized_intent)
            if provider in {"obsidian", "gcal", "gtasks"}:
                kind = "roles" if provider == "obsidian" else "events" if provider == "gcal" else "tasks"
                return (provider, "sync_state", {"kind": kind})
            return None

        provider_raw, action_raw = normalized_intent.split(".", 1)
        provider = self._PROVIDER_ALIASES.get(provider_raw, provider_raw)
        if provider == "obsidian":
            alias = f"obsidian.{action_raw}"
            if alias in self._ACTION_ALIASES:
                return self._ACTION_ALIASES[alias]
            if action_raw in {"delete", "remove", "retract", "cancel"}:
                return ("obsidian", "delete_item", {})
            if action_raw in {"read", "state"}:
                return ("obsidian", "sync_state", {"kind": "roles"})
            if action_raw.startswith("read_"):
                return ("obsidian", "sync_state", {"kind": action_raw[5:]})
            return None
        if provider not in {"gcal", "gtasks"}:
            return None

        alias = f"{provider}.{action_raw}"
        if alias in self._ACTION_ALIASES:
            return self._ACTION_ALIASES[alias]
        if action_raw in {"upsert", "write", "create", "update"}:
            return (provider, "upsert_item", {})
        if action_raw in {"delete", "remove", "retract", "cancel"}:
            return (provider, "delete_item", {})
        if action_raw in {"sync", "state", "read"}:
            return (provider, "sync_state", {"kind": "events"} if provider == "gcal" else {"kind": "tasks"})
        if action_raw in {"tasks", "events"}:
            return (provider, "sync_state", {"kind": action_raw})
        return None

    async def dispatch(self, intent: Any, payload: Optional[Mapping[str, Any]] = None) -> HUDStoreRecord:
        normalized_payload = _coerce_payload(payload)
        normalized_intent = self._normalize_intent(intent)
        resolved = self._resolve(normalized_intent, normalized_payload)
        if resolved is None:
            return {"status": "error", "intent": normalized_intent, "adapter": None, "action": None, "error": {"code": "unsupported_intent", "message": "No adapter route for intent"}}
        adapter_name, method_name, route_defaults = resolved
        adapter = self.adapters.get(adapter_name)
        if adapter is None:
            return {"status": "error", "intent": normalized_intent, "adapter": adapter_name, "action": method_name, "error": {"code": "missing_adapter", "message": f"Adapter '{adapter_name}' not configured"}}
        handler = getattr(adapter, method_name, None)
        if handler is None or not callable(handler):
            return {"status": "error", "intent": normalized_intent, "adapter": adapter_name, "action": method_name, "error": {"code": "handler_missing", "message": f"Handler '{method_name}' missing"}}
        merged_payload: Dict[str, Any] = {**route_defaults, **normalized_payload}
        result = await handler(merged_payload)  # type: ignore[arg-type]
        result_status = result.get("status", "ok")
        normalized_result: HUDStoreRecord = {"status": "ok" if result_status == "ok" else result_status, "intent": normalized_intent, "adapter": adapter_name, "action": method_name, "payload": merged_payload, "result": result}
        normalized_result["result"]["endpoint"] = result.get("endpoint") or adapter.endpoints.get(method_name.replace("sync_state", "sync").replace("upsert_item", "upsert")) or adapter.endpoints.get("default")
        return normalized_result


__all__ = ["BaseHUDAdapter", "ObsidianAdapter", "GoogleCalendarAdapter", "GoogleTasksAdapter", "HUDAdapterHub", "_normalize_hud_record", "_normalize_status"]
