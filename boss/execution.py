from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable

from agents import function_tool
from agents.items import ToolApprovalItem
from agents.run_context import RunContextWrapper

from boss.config import settings
from boss.observability import log_permission_event


class ExecutionType(StrEnum):
    READ = "read"
    SEARCH = "search"
    PLAN = "plan"
    EDIT = "edit"
    RUN = "run"
    EXTERNAL = "external"


class PermissionDecision(StrEnum):
    ALLOW_ONCE = "allow_once"
    ALWAYS_ALLOW = "always_allow"
    DENY = "deny"


class PendingStatus(StrEnum):
    PENDING = "pending"
    EXPIRED = "expired"


AUTO_ALLOWED_EXECUTION_TYPES = {
    ExecutionType.READ,
    ExecutionType.SEARCH,
    ExecutionType.PLAN,
}


@dataclass(frozen=True)
class ToolMetadata:
    name: str
    title: str
    execution_type: ExecutionType
    describe_call: Callable[[dict[str, Any]], str]
    scope_key: Callable[[dict[str, Any]], str]
    scope_label: Callable[[dict[str, Any]], str]


@dataclass
class PermissionRule:
    tool_name: str
    scope_key: str
    execution_type: str
    decision: str
    updated_at: float
    scope_label: str = ""
    last_used_at: float | None = None


@dataclass
class PendingApproval:
    approval_id: str
    tool_name: str
    title: str
    description: str
    execution_type: str
    scope_key: str
    requested_at: float
    scope_label: str = ""
    status: str = PendingStatus.PENDING.value
    expires_at: float | None = None
    expired_at: float | None = None


@dataclass
class PendingRun:
    run_id: str
    session_id: str
    state: dict[str, Any]
    approvals: list[PendingApproval]
    updated_at: float
    status: str = PendingStatus.PENDING.value
    expires_at: float | None = None
    expired_at: float | None = None


_TOOL_METADATA: dict[str, ToolMetadata] = {}


def slugify(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower())
    return text.strip("-") or "default"


def hash_text(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


def scope_value(prefix: str, value: Any) -> str:
    return f"{prefix}:{slugify(value)}"


def hashed_scope(prefix: str, value: Any) -> str:
    return f"{prefix}:{hash_text(value)}"


def display_value(value: Any, *, fallback: str = "this item") -> str:
    text = str(value).strip()
    return text if text else fallback


def extract_domain(value: str) -> str | None:
    site_match = re.search(r"(?:site:|domain:)([a-z0-9.-]+\.[a-z]{2,})", value, re.IGNORECASE)
    if site_match:
        return site_match.group(1).lower()

    url_match = re.search(r"https?://([^/\s]+)", value, re.IGNORECASE)
    if url_match:
        return url_match.group(1).lower()

    bare_domain_match = re.search(r"\b([a-z0-9.-]+\.[a-z]{2,})\b", value, re.IGNORECASE)
    if bare_domain_match:
        return bare_domain_match.group(1).lower()

    return None


def applescript_scope_key(script: str) -> str:
    targets = sorted(
        {
            match.group(1).strip().lower()
            for match in re.finditer(r'tell\s+application\s+"([^"]+)"', script, re.IGNORECASE)
        }
    )
    hash_suffix = hash_text(script)
    if targets:
        return f"applescript:{'-'.join(slugify(target) for target in targets)}:{hash_suffix}"
    return f"applescript:any:{hash_suffix}"


def applescript_scope_label(script: str) -> str:
    targets = [
        match.group(1).strip()
        for match in re.finditer(r'tell\s+application\s+"([^"]+)"', script, re.IGNORECASE)
    ]
    if targets:
        unique_targets = ", ".join(dict.fromkeys(targets))
        return f"AppleScript for {unique_targets}"
    return f"AppleScript {hash_text(script)}"


def web_scope_key(query: str) -> str:
    domain = extract_domain(query)
    if domain:
        return f"web-domain:{slugify(domain)}"
    return hashed_scope("web-query", query)


def web_scope_label(query: str) -> str:
    domain = extract_domain(query)
    if domain:
        return domain
    text = display_value(query, fallback="web search")
    return text if len(text) <= 72 else text[:69] + "..."


def fallback_scope_label(scope_key: str) -> str:
    if not scope_key:
        return "Any"

    prefix, _, remainder = scope_key.partition(":")
    if not remainder:
        return display_value(scope_key, fallback="Any")

    if prefix == "app":
        return remainder.replace("-", " ").title()
    if prefix == "web-domain":
        return remainder
    if prefix == "clipboard":
        return "Clipboard write"
    if prefix == "memory":
        return remainder.replace("-", " / ").title()
    if prefix == "project":
        return remainder.replace("-", " ")
    if prefix == "screenshot":
        return remainder
    if prefix == "notification":
        return "Notification"
    if prefix == "applescript":
        parts = remainder.split(":")
        target = parts[0].replace("-", " ").title() if parts else "Any"
        return f"AppleScript for {target}" if target and target != "Any" else "AppleScript"

    return remainder.replace("-", " ").title()


def prettify_tool_name(name: str) -> str:
    return name.replace("_", " ").strip().title() or "Tool"


def register_tool_metadata(
    *,
    tool_name: str,
    title: str,
    execution_type: ExecutionType,
    describe_call: Callable[[dict[str, Any]], str] | None = None,
    scope_key: Callable[[dict[str, Any]], str] | None = None,
    scope_label: Callable[[dict[str, Any]], str] | None = None,
) -> None:
    _TOOL_METADATA[tool_name] = ToolMetadata(
        name=tool_name,
        title=title,
        execution_type=execution_type,
        describe_call=describe_call or (lambda _: title),
        scope_key=scope_key or (lambda _: "any"),
        scope_label=scope_label or (lambda _: "Any"),
    )


def get_tool_metadata(tool_name: str) -> ToolMetadata | None:
    return _TOOL_METADATA.get(tool_name)


def governed_function_tool(
    *,
    execution_type: ExecutionType,
    title: str,
    describe_call: Callable[[dict[str, Any]], str] | None = None,
    scope_key: Callable[[dict[str, Any]], str] | None = None,
    scope_label: Callable[[dict[str, Any]], str] | None = None,
    **tool_kwargs: Any,
):
    def decorator(func: Callable[..., Any]):
        tool_name = tool_kwargs.get("name_override") or func.__name__
        register_tool_metadata(
            tool_name=tool_name,
            title=title,
            execution_type=execution_type,
            describe_call=describe_call,
            scope_key=scope_key,
            scope_label=scope_label,
        )

        async def needs_approval(
            _context: RunContextWrapper[Any], tool_parameters: dict[str, Any], _call_id: str
        ) -> bool:
            if execution_type in AUTO_ALLOWED_EXECUTION_TYPES:
                return False
            metadata = _TOOL_METADATA[tool_name]
            rule = get_permission_rule(tool_name, metadata.scope_key(tool_parameters or {}))
            return rule is None or rule.decision != PermissionDecision.ALWAYS_ALLOW.value

        return function_tool(
            needs_approval=needs_approval
            if execution_type not in AUTO_ALLOWED_EXECUTION_TYPES
            else False,
            **tool_kwargs,
        )(func)

    return decorator


def register_hosted_tool(
    tool: Any,
    *,
    execution_type: ExecutionType,
    title: str,
    describe_call: Callable[[dict[str, Any]], str] | None = None,
    scope_key: Callable[[dict[str, Any]], str] | None = None,
    scope_label: Callable[[dict[str, Any]], str] | None = None,
) -> Any:
    tool_name = getattr(tool, "name", prettify_tool_name(tool.__class__.__name__))
    register_tool_metadata(
        tool_name=tool_name,
        title=title,
        execution_type=execution_type,
        describe_call=describe_call,
        scope_key=scope_key,
        scope_label=scope_label,
    )
    return tool


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(f"{path.suffix}.tmp")
    temp_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    temp_path.replace(path)


def _permission_key(tool_name: str, scope_key: str) -> str:
    return f"{tool_name}:{scope_key}"


def load_permission_rules() -> dict[str, PermissionRule]:
    payload = _read_json(settings.permissions_file, {"rules": []})
    rules: dict[str, PermissionRule] = {}
    for item in payload.get("rules", []):
        try:
            rule = PermissionRule(**item)
        except TypeError:
            continue
        if not rule.scope_label:
            rule.scope_label = fallback_scope_label(rule.scope_key)
        rules[_permission_key(rule.tool_name, rule.scope_key)] = rule
    return rules


def list_permission_rules() -> list[PermissionRule]:
    rules = list(load_permission_rules().values())
    return sorted(
        rules,
        key=lambda rule: rule.last_used_at or rule.updated_at,
        reverse=True,
    )


def get_permission_rule(tool_name: str, scope_key: str) -> PermissionRule | None:
    return load_permission_rules().get(_permission_key(tool_name, scope_key))


def delete_permission_rule(tool_name: str, scope_key: str) -> bool:
    rules = load_permission_rules()
    key = _permission_key(tool_name, scope_key)
    if key not in rules:
        return False
    rules.pop(key, None)
    _write_json(settings.permissions_file, {"rules": [asdict(rule) for rule in rules.values()]})
    return True


def store_permission_rule(
    *,
    tool_name: str,
    scope_key: str,
    scope_label: str,
    execution_type: ExecutionType,
    decision: PermissionDecision,
) -> None:
    if decision == PermissionDecision.ALLOW_ONCE:
        return
    rules = load_permission_rules()
    key = _permission_key(tool_name, scope_key)
    rules[key] = PermissionRule(
        tool_name=tool_name,
        scope_key=scope_key,
        scope_label=scope_label,
        execution_type=execution_type.value,
        decision=decision.value,
        updated_at=time.time(),
        last_used_at=time.time(),
    )
    _write_json(settings.permissions_file, {"rules": [asdict(rule) for rule in rules.values()]})


def record_permission_rule_use(tool_name: str, scope_key: str) -> None:
    rules = load_permission_rules()
    key = _permission_key(tool_name, scope_key)
    rule = rules.get(key)
    if rule is None:
        return
    rule.last_used_at = time.time()
    _write_json(settings.permissions_file, {"rules": [asdict(item) for item in rules.values()]})


def append_permission_log(
    *,
    tool_name: str,
    execution_type: ExecutionType,
    decision: PermissionDecision,
    approval_time_ms: int,
    scope_key: str,
    source: str,
) -> None:
    settings.permission_log_file.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "timestamp": time.time(),
        "tool_name": tool_name,
        "execution_type": execution_type.value,
        "decision": decision.value,
        "approval_time_ms": approval_time_ms,
        "scope_key": scope_key,
        "source": source,
    }
    with settings.permission_log_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")
    log_permission_event(
        stage="decision",
        tool_name=tool_name,
        execution_type=execution_type.value,
        scope_label=fallback_scope_label(scope_key),
        scope_key=scope_key,
        decision=decision.value,
        source=source,
        approval_time_ms=approval_time_ms,
    )


def extract_tool_parameters(raw_item: Any) -> dict[str, Any]:
    if isinstance(raw_item, dict):
        arguments = raw_item.get("arguments")
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                parsed = {"value": arguments}
            return parsed if isinstance(parsed, dict) else {"value": parsed}
        query = raw_item.get("query")
        if isinstance(query, str):
            return {"query": query}
        return {}

    arguments = getattr(raw_item, "arguments", None)
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            parsed = {"value": arguments}
        return parsed if isinstance(parsed, dict) else {"value": parsed}

    query = getattr(raw_item, "query", None)
    if isinstance(query, str):
        return {"query": query}

    return {}


def get_tool_call_id(raw_item: Any) -> str:
    if isinstance(raw_item, dict):
        call_id = raw_item.get("call_id") or raw_item.get("id")
        return str(call_id) if call_id else uuid.uuid4().hex
    call_id = getattr(raw_item, "call_id", None) or getattr(raw_item, "id", None)
    return str(call_id) if call_id else uuid.uuid4().hex


def build_tool_display(
    tool_name: str, raw_item: Any
) -> tuple[str, str, ExecutionType | None, str, str]:
    metadata = get_tool_metadata(tool_name)
    params = extract_tool_parameters(raw_item)
    if metadata is None:
        if tool_name.startswith("transfer_to_"):
            target = tool_name.removeprefix("transfer_to_").replace("_", " ").strip().title()
            return "Route", f"Route to {target}", ExecutionType.PLAN, f"route:{slugify(target)}", target
        title = prettify_tool_name(tool_name)
        return title, title, None, "any", "Any"
    return (
        metadata.title,
        metadata.describe_call(params),
        metadata.execution_type,
        metadata.scope_key(params),
        metadata.scope_label(params),
    )


def pending_approval_from_item(item: ToolApprovalItem) -> PendingApproval:
    tool_name = item.tool_name or "tool"
    title, description, execution_type, scope_key, scope_label = build_tool_display(
        tool_name, item.raw_item
    )
    requested_at = time.time()
    return PendingApproval(
        approval_id=get_tool_call_id(item.raw_item),
        tool_name=tool_name,
        title=title,
        description=description,
        execution_type=(execution_type or ExecutionType.RUN).value,
        scope_key=scope_key,
        scope_label=scope_label,
        requested_at=requested_at,
        expires_at=requested_at + settings.pending_run_expiration_seconds,
    )


def _pending_runs_archive_dir() -> Path:
    return settings.pending_runs_dir / "expired"


def _expiry_deadline(started_at: float | None) -> float:
    base = started_at if started_at is not None else time.time()
    return base + settings.pending_run_expiration_seconds


def _approval_from_payload(item: dict[str, Any]) -> PendingApproval | None:
    try:
        requested_at = float(item.get("requested_at", time.time()))
        expires_at = item.get("expires_at")
        expired_at = item.get("expired_at")
        return PendingApproval(
            approval_id=item["approval_id"],
            tool_name=item.get("tool_name", "tool"),
            title=item.get("title", prettify_tool_name(item.get("tool_name", "tool"))),
            description=item.get("description", item.get("title", "Tool approval")),
            execution_type=item.get("execution_type", ExecutionType.RUN.value),
            scope_key=item.get("scope_key", "any"),
            scope_label=item.get("scope_label", "Any"),
            requested_at=requested_at,
            status=item.get("status", PendingStatus.PENDING.value),
            expires_at=float(expires_at) if expires_at is not None else _expiry_deadline(requested_at),
            expired_at=float(expired_at) if expired_at is not None else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _approval_is_expired(approval: PendingApproval, now: float | None = None) -> bool:
    if approval.status == PendingStatus.EXPIRED.value:
        return True
    now = time.time() if now is None else now
    expires_at = approval.expires_at if approval.expires_at is not None else _expiry_deadline(approval.requested_at)
    return expires_at <= now


def _pending_run_is_expired(record: PendingRun, now: float | None = None) -> bool:
    if record.status == PendingStatus.EXPIRED.value:
        return True
    now = time.time() if now is None else now
    expires_at = record.expires_at if record.expires_at is not None else _expiry_deadline(record.updated_at)
    if expires_at <= now:
        return True
    return any(_approval_is_expired(approval, now) for approval in record.approvals)


def _expired_record(record: PendingRun, *, expired_at: float | None = None) -> PendingRun:
    expired_at = time.time() if expired_at is None else expired_at
    approvals = [
        PendingApproval(
            approval_id=approval.approval_id,
            tool_name=approval.tool_name,
            title=approval.title,
            description=approval.description,
            execution_type=approval.execution_type,
            scope_key=approval.scope_key,
            scope_label=approval.scope_label,
            requested_at=approval.requested_at,
            status=PendingStatus.EXPIRED.value,
            expires_at=approval.expires_at if approval.expires_at is not None else _expiry_deadline(approval.requested_at),
            expired_at=expired_at,
        )
        for approval in record.approvals
    ]
    return PendingRun(
        run_id=record.run_id,
        session_id=record.session_id,
        state=record.state,
        approvals=approvals,
        updated_at=record.updated_at,
        status=PendingStatus.EXPIRED.value,
        expires_at=record.expires_at if record.expires_at is not None else _expiry_deadline(record.updated_at),
        expired_at=expired_at,
    )


def _archive_pending_run(record: PendingRun, *, expired_at: float | None = None) -> PendingRun:
    archived = _expired_record(record, expired_at=expired_at)
    archive_dir = _pending_runs_archive_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    _write_json(archive_dir / f"{record.run_id}.json", asdict(archived))
    delete_pending_run(record.run_id)
    return archived


def _prune_expired_pending_runs(now: float | None = None) -> int:
    archive_dir = _pending_runs_archive_dir()
    if not archive_dir.exists():
        return 0

    now = time.time() if now is None else now
    pruned = 0
    for path in archive_dir.glob("*.json"):
        payload = _read_json(path, None)
        if not isinstance(payload, dict):
            continue
        record = _pending_run_from_payload(payload)
        if record is None:
            continue
        expired_at = record.expired_at or record.updated_at
        if expired_at + settings.expired_pending_run_retention_seconds > now:
            continue
        try:
            path.unlink()
            pruned += 1
        except OSError:
            continue
    return pruned


def cleanup_stale_pending_runs() -> int:
    if not settings.pending_runs_dir.exists():
        return 0

    expired = 0
    now = time.time()
    for path in sorted(settings.pending_runs_dir.glob("*.json")):
        payload = _read_json(path, None)
        if not isinstance(payload, dict):
            continue
        record = _pending_run_from_payload(payload)
        if record is None or not _pending_run_is_expired(record, now):
            continue
        _archive_pending_run(record, expired_at=now)
        expired += 1
    _prune_expired_pending_runs(now)
    return expired


def save_pending_run(
    *,
    session_id: str,
    state: dict[str, Any],
    approvals: list[PendingApproval],
    run_id: str | None = None,
) -> str:
    run_id = run_id or uuid.uuid4().hex
    settings.pending_runs_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    expires_at = now + settings.pending_run_expiration_seconds
    hydrated_approvals = [
        PendingApproval(
            approval_id=approval.approval_id,
            tool_name=approval.tool_name,
            title=approval.title,
            description=approval.description,
            execution_type=approval.execution_type,
            scope_key=approval.scope_key,
            scope_label=approval.scope_label,
            requested_at=approval.requested_at,
            status=PendingStatus.PENDING.value,
            expires_at=approval.expires_at if approval.expires_at is not None else expires_at,
            expired_at=None,
        )
        for approval in approvals
    ]
    record = PendingRun(
        run_id=run_id,
        session_id=session_id,
        state=state,
        approvals=hydrated_approvals,
        updated_at=now,
        status=PendingStatus.PENDING.value,
        expires_at=expires_at,
        expired_at=None,
    )
    _write_json(settings.pending_runs_dir / f"{run_id}.json", asdict(record))
    return run_id


def _pending_run_from_payload(payload: dict[str, Any]) -> PendingRun | None:
    try:
        approvals = [
            approval
            for item in payload.get("approvals", [])
            if isinstance(item, dict)
            for approval in [_approval_from_payload(item)]
            if approval is not None
        ]
        updated_at = float(payload.get("updated_at", time.time()))
        expires_at = payload.get("expires_at")
        expired_at = payload.get("expired_at")
        return PendingRun(
            run_id=payload["run_id"],
            session_id=payload["session_id"],
            state=payload["state"],
            approvals=approvals,
            updated_at=updated_at,
            status=payload.get("status", PendingStatus.PENDING.value),
            expires_at=float(expires_at) if expires_at is not None else _expiry_deadline(updated_at),
            expired_at=float(expired_at) if expired_at is not None else None,
        )
    except (KeyError, TypeError, ValueError):
        return None


def load_pending_run(run_id: str) -> PendingRun | None:
    cleanup_stale_pending_runs()
    payload = _read_json(settings.pending_runs_dir / f"{run_id}.json", None)
    if not payload:
        return None
    record = _pending_run_from_payload(payload)
    if record is None:
        return None
    if _pending_run_is_expired(record):
        _archive_pending_run(record)
        return None
    return record


def load_expired_pending_run(run_id: str) -> PendingRun | None:
    payload = _read_json(_pending_runs_archive_dir() / f"{run_id}.json", None)
    if not payload:
        return None
    return _pending_run_from_payload(payload)


def list_pending_runs() -> list[PendingRun]:
    cleanup_stale_pending_runs()
    if not settings.pending_runs_dir.exists():
        return []

    records: list[PendingRun] = []
    for path in sorted(settings.pending_runs_dir.glob("*.json")):
        payload = _read_json(path, None)
        if not isinstance(payload, dict):
            continue
        record = _pending_run_from_payload(payload)
        if record is not None and record.status != PendingStatus.EXPIRED.value:
            records.append(record)
    return records


def stale_pending_run_count() -> int:
    archive_dir = _pending_runs_archive_dir()
    if not archive_dir.exists():
        return 0
    return sum(1 for _ in archive_dir.glob("*.json"))


def pending_run_counts() -> tuple[int, int]:
    records = list_pending_runs()
    return len(records), sum(len(record.approvals) for record in records)


def pending_run_metrics() -> tuple[int, int, int]:
    cleanup_stale_pending_runs()
    records = list_pending_runs()
    return len(records), sum(len(record.approvals) for record in records), stale_pending_run_count()


def delete_pending_run(run_id: str) -> None:
    path = settings.pending_runs_dir / f"{run_id}.json"
    if path.exists():
        path.unlink()