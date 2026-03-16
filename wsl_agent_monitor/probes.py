from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from textwrap import dedent
from typing import Any

from .constants import PROBE_TIMEOUT_SECONDS


INITIAL_BYTES = 65536
TAIL_BYTES = 32768
MAX_EVENTS = 80
ACTIVE_WINDOW_SECONDS = 600


def _clip(value: object, limit: int = 220) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def _short_id(value: object) -> str:
    return str(value).split("-")[0]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


def _read_lines(path: Path, current_offset: int) -> tuple[list[str], int, bool]:
    size = path.stat().st_size
    reset = current_offset <= 0 or current_offset > size
    start = max(size - INITIAL_BYTES, 0) if reset else current_offset

    with path.open("rb") as handle:
        handle.seek(start)
        if start:
            handle.readline()
        payload = handle.read()
        end_offset = handle.tell()

    return payload.decode("utf-8", "replace").splitlines(), end_offset, reset


def _tail_lines(path: Path) -> list[str]:
    size = path.stat().st_size
    start = max(size - TAIL_BYTES, 0)
    with path.open("rb") as handle:
        handle.seek(start)
        if start:
            handle.readline()
        payload = handle.read()
    return payload.decode("utf-8", "replace").splitlines()


def _stamp(obj: dict[str, Any]) -> str:
    value = str(obj.get("timestamp", ""))
    return value[11:19] if len(value) >= 19 else "--:--:--"


def _timestamp_value(obj: dict[str, Any]) -> str:
    return str(obj.get("timestamp", ""))


def _compact_number(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"

    abs_number = abs(number)
    if abs_number >= 1_000_000_000:
        rendered = f"{number / 1_000_000_000:.1f}B"
    elif abs_number >= 1_000_000:
        rendered = f"{number / 1_000_000:.1f}M"
    elif abs_number >= 1_000:
        rendered = f"{number / 1_000:.1f}k"
    else:
        rendered = f"{number:.0f}"
    return rendered.replace(".0", "")


def _compact_percent(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number.is_integer():
        return f"{int(number)}%"
    return f"{number:.1f}%"


def _process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _empty_result(summary: str, detail: str, offsets: dict[str, int] | None, usage_text: str = "Usage unavailable") -> dict[str, object]:
    return {
        "status": "idle",
        "summary": summary,
        "detail": detail,
        "sources": [],
        "offsets": offsets or {},
        "sessions": [],
        "events": [],
        "usage_text": usage_text,
    }


def _summarize_mapping(mapping: object) -> str:
    if not isinstance(mapping, dict):
        return _clip(mapping)
    if mapping.get("command"):
        return _clip(mapping["command"])
    if mapping.get("description"):
        return _clip(mapping["description"])
    if mapping.get("task_id"):
        return "task " + _clip(mapping["task_id"])
    return _clip(json.dumps(mapping, ensure_ascii=False))


def _summarize_tool_result(result: object) -> str:
    if not isinstance(result, dict):
        return _clip(result)
    stdout = str(result.get("stdout") or "").strip()
    stderr = str(result.get("stderr") or "").strip()
    if stdout:
        return _clip(stdout)
    if stderr:
        return "stderr: " + _clip(stderr)
    if result.get("interrupted"):
        return "interrupted"
    return "(no output)"


def _render_claude_entry(obj: dict[str, Any], session_prefix: str) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    when = _stamp(obj)
    entry_type = obj.get("type")
    message = obj.get("message") or {}

    if entry_type == "assistant":
        for item in message.get("content", []):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "text":
                text = _clip(item.get("text", ""))
                if text:
                    lines.append((_timestamp_value(obj), f"{when} [{session_prefix}] assistant: {text}"))
            elif item_type == "thinking":
                text = _clip(item.get("thinking", ""))
                if text:
                    lines.append((_timestamp_value(obj), f"{when} [{session_prefix}] thinking: {text}"))
            elif item_type == "tool_use":
                name = item.get("name", "tool")
                summary = _summarize_mapping(item.get("input") or {})
                lines.append((_timestamp_value(obj), f"{when} [{session_prefix}] tool {name}: {summary}"))
    elif entry_type == "user" and obj.get("toolUseResult"):
        lines.append((_timestamp_value(obj), f"{when} [{session_prefix}] result: {_summarize_tool_result(obj['toolUseResult'])}"))
    elif entry_type == "progress":
        data = obj.get("data") or {}
        summary = _clip(data.get("taskDescription") or data.get("type") or "progress")
        lines.append((_timestamp_value(obj), f"{when} [{session_prefix}] progress: {summary}"))

    return [line for line in lines if line[1]]


def _extract_claude_usage(entry: dict[str, Any]) -> dict[str, float] | None:
    message = entry.get("message") or {}
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    result: dict[str, float] = {}
    for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            result[key] = float(value)

    for key in ("total_cost_usd", "cost_usd", "total_cost"):
        value = usage.get(key)
        if isinstance(value, (int, float)):
            result["cost_usd"] = float(value)
            break

    return result or None


def _merge_claude_usage(total: dict[str, float], usage: dict[str, float] | None) -> None:
    if not usage:
        return
    for key, value in usage.items():
        total[key] = total.get(key, 0.0) + value


def _format_claude_usage(usage: dict[str, float], session_count: int) -> str:
    if not usage:
        return "Usage unavailable"

    parts = [
        f"in {_compact_number(usage.get('input_tokens', 0))}",
        f"out {_compact_number(usage.get('output_tokens', 0))}",
    ]
    if usage.get("cache_read_input_tokens"):
        parts.append(f"cache {_compact_number(usage.get('cache_read_input_tokens', 0))}")
    if usage.get("cache_creation_input_tokens"):
        parts.append(f"cache+ {_compact_number(usage.get('cache_creation_input_tokens', 0))}")
    if usage.get("cost_usd"):
        parts.append(f"${usage.get('cost_usd', 0.0):.2f}")
    parts.append(f"{session_count} sess")
    return "Usage " + " | ".join(parts)


def _locate_claude_project_log(session_id: str) -> Path | None:
    projects_dir = Path.home() / ".claude" / "projects"
    try:
        return next(projects_dir.rglob(session_id + ".jsonl"))
    except StopIteration:
        return None


def _extract_claude_cwd(entry: dict[str, Any]) -> str:
    cwd = entry.get("cwd")
    return str(cwd).strip() if cwd else "-"


def _project_name_from_cwd(cwd: str) -> str:
    name = Path(cwd).name if cwd and cwd != "-" else ""
    return _clip(name or "claude-session", 80)


def _session_name_claude(log_path: Path, fallback_cwd: str) -> str:
    try:
        for raw_line in reversed(_tail_lines(log_path)):
            entry = json.loads(raw_line)
            if not isinstance(entry, dict):
                continue
            slug = entry.get("slug")
            if slug:
                return _clip(slug, 80)
            cwd = _extract_claude_cwd(entry)
            if cwd != "-":
                fallback_cwd = cwd
    except Exception:
        pass
    return _project_name_from_cwd(fallback_cwd)


def _latest_claude_summary_and_usage(log_path: Path, session_prefix: str) -> tuple[str, str, dict[str, float] | None, str]:
    latest_summary = "Watching Claude sessions"
    latest_stamp = ""
    latest_usage: dict[str, float] | None = None
    latest_cwd = "-"

    try:
        for raw_line in reversed(_tail_lines(log_path)):
            entry = json.loads(raw_line)
            if not isinstance(entry, dict):
                continue
            if latest_usage is None:
                latest_usage = _extract_claude_usage(entry)
            if latest_cwd == "-":
                latest_cwd = _extract_claude_cwd(entry)
            rendered = _render_claude_entry(entry, session_prefix)
            if rendered:
                stamp_value, line = rendered[-1]
                if stamp_value >= latest_stamp:
                    latest_stamp = stamp_value
                    latest_summary = line
            if latest_usage is not None and latest_cwd != "-" and rendered:
                break
    except Exception:
        pass

    return latest_summary, latest_stamp, latest_usage, latest_cwd


def _pick_local_claude_sessions() -> list[dict[str, Any]]:
    sessions_dir = Path.home() / ".claude" / "sessions"
    candidates: list[dict[str, Any]] = []

    if sessions_dir.exists():
        for meta_path in sessions_dir.glob("*.json"):
            data = _read_json(meta_path)
            if not data:
                continue
            pid = int(data.get("pid") or 0)
            session_id = str(data.get("sessionId") or "")
            if not session_id or not _process_exists(pid):
                continue

            log_path = _locate_claude_project_log(session_id)
            if log_path is None:
                continue

            candidates.append(
                {
                    "pid": pid,
                    "session_id": session_id,
                    "cwd": str(data.get("cwd") or "-"),
                    "started_at": int(data.get("startedAt") or 0),
                    "log_path": log_path,
                }
            )

    if candidates:
        candidates.sort(key=lambda item: (item["started_at"], item["pid"]), reverse=True)
        return candidates

    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return []

    now = time.time()
    fallback: list[dict[str, Any]] = []
    for log_path in projects_dir.rglob("*.jsonl"):
        try:
            stat = log_path.stat()
        except OSError:
            continue
        if now - stat.st_mtime > ACTIVE_WINDOW_SECONDS:
            continue
        fallback.append(
            {
                "pid": 0,
                "session_id": log_path.stem,
                "cwd": "-",
                "started_at": int(stat.st_mtime),
                "log_path": log_path,
            }
        )

    fallback.sort(key=lambda item: item["started_at"], reverse=True)
    return fallback


def _run_local_claude_probe(offsets: dict[str, int] | None) -> dict[str, object]:
    active_sessions = _pick_local_claude_sessions()
    if not active_sessions:
        return _empty_result("No active Claude session found", "Waiting for live Claude activity on host", offsets)

    events: list[tuple[str, str]] = []
    sessions: list[dict[str, str]] = []
    next_offsets: dict[str, int] = {}
    sources: list[str] = []
    latest_summary = "Watching Claude sessions"
    latest_stamp = ""
    usage_totals: dict[str, float] = {}

    offset_map = offsets or {}
    for active in active_sessions:
        log_path = active["log_path"]
        source_key = str(log_path)
        current_offset = int(offset_map.get(source_key) or 0)
        try:
            chunk_lines, end_offset, _reset = _read_lines(log_path, current_offset)
        except OSError:
            continue

        session_prefix = _short_id(active["session_id"])
        summary_line, summary_stamp, latest_usage, latest_cwd = _latest_claude_summary_and_usage(log_path, session_prefix)
        session_cwd = active.get("cwd") or latest_cwd or "-"
        name = _session_name_claude(log_path, session_cwd)

        sessions.append({"id": session_prefix, "name": name})
        next_offsets[source_key] = end_offset
        sources.append(source_key)
        _merge_claude_usage(usage_totals, latest_usage)

        for raw_line in chunk_lines:
            try:
                entry = json.loads(raw_line)
            except Exception:
                continue
            if isinstance(entry, dict):
                events.extend(_render_claude_entry(entry, session_prefix))

        if summary_stamp >= latest_stamp:
            latest_stamp = summary_stamp
            latest_summary = summary_line

    if not sessions:
        return _empty_result("No active Claude session found", "Waiting for live Claude activity on host", offsets)

    events.sort(key=lambda item: item[0])
    sessions.sort(key=lambda item: item["id"])
    return {
        "status": "watching",
        "summary": latest_summary,
        "detail": f"{len(sessions)} active Claude session(s)",
        "sources": sources,
        "offsets": next_offsets,
        "sessions": sessions,
        "events": [line for _stamp, line in events[-MAX_EVENTS:]],
        "usage_text": _format_claude_usage(usage_totals, len(sessions)),
    }


def _compact_output(value: object) -> str:
    if not value:
        return "(no output)"
    return _clip(value)


def _summarize_arguments(value: object) -> str:
    if not value:
        return ""
    return _clip(value, 180)


def _render_codex_response_message(payload: dict[str, Any], when: str, session_prefix: str, timestamp: str) -> list[tuple[str, str]]:
    lines: list[tuple[str, str]] = []
    phase = payload.get("phase")
    for item in payload.get("content", []):
        if not isinstance(item, dict) or item.get("type") != "output_text":
            continue
        text = _clip(item.get("text", ""))
        if not text:
            continue
        label = "assistant"
        if phase == "commentary":
            label = "commentary"
        elif phase == "final_answer":
            label = "final"
        lines.append((timestamp, f"{when} [{session_prefix}] {label}: {text}"))
    return lines


def _render_codex_entry(obj: dict[str, Any], session_prefix: str) -> list[tuple[str, str]]:
    when = _stamp(obj)
    entry_type = obj.get("type")
    payload = obj.get("payload") or {}
    timestamp = _timestamp_value(obj)

    if entry_type == "event_msg":
        kind = payload.get("type")
        if kind == "agent_message":
            text = _clip(payload.get("message", ""))
            return [(timestamp, f"{when} [{session_prefix}] status: {text}")] if text else []
        if kind == "task_started":
            turn_id = _clip(payload.get("turn_id", "task"))
            return [(timestamp, f"{when} [{session_prefix}] task started: {turn_id}")]
        if kind == "task_complete":
            text = _clip(payload.get("last_agent_message", "task complete"))
            return [(timestamp, f"{when} [{session_prefix}] task complete: {text}")]
        if kind == "user_message":
            text = _clip(payload.get("message", ""))
            return [(timestamp, f"{when} [{session_prefix}] user: {text}")] if text else []
        return []

    if entry_type != "response_item":
        return []

    payload_type = payload.get("type")
    if payload_type == "message" and payload.get("role") == "assistant":
        return _render_codex_response_message(payload, when, session_prefix, timestamp)
    if payload_type == "function_call":
        name = payload.get("name", "function_call")
        arguments = _summarize_arguments(payload.get("arguments"))
        return [(timestamp, f"{when} [{session_prefix}] function {name}: {arguments}")]
    if payload_type == "function_call_output":
        return [(timestamp, f"{when} [{session_prefix}] output: {_compact_output(payload.get('output', ''))}")]
    if payload_type == "custom_tool_call":
        name = payload.get("name", "tool")
        arguments = _summarize_arguments(payload.get("input"))
        return [(timestamp, f"{when} [{session_prefix}] tool {name}: {arguments}")]
    if payload_type == "custom_tool_call_output":
        return [(timestamp, f"{when} [{session_prefix}] tool output: {_compact_output(payload.get('output', ''))}")]
    return []


def _extract_codex_usage(entry: dict[str, Any]) -> dict[str, float] | None:
    if entry.get("type") != "event_msg":
        return None
    payload = entry.get("payload") or {}
    if payload.get("type") != "token_count":
        return None

    result: dict[str, float] = {}
    info = payload.get("info") or {}
    total_usage = info.get("total_token_usage") or {}
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
        value = total_usage.get(key)
        if isinstance(value, (int, float)):
            result[key] = float(value)

    context_window = info.get("model_context_window")
    if isinstance(context_window, (int, float)):
        result["model_context_window"] = float(context_window)

    rate_limits = payload.get("rate_limits") or {}
    primary = (rate_limits.get("primary") or {}).get("used_percent")
    secondary = (rate_limits.get("secondary") or {}).get("used_percent")
    credits = rate_limits.get("credits")
    if isinstance(primary, (int, float)):
        result["primary_used_percent"] = float(primary)
    if isinstance(secondary, (int, float)):
        result["secondary_used_percent"] = float(secondary)
    if isinstance(credits, (int, float)):
        result["credits"] = float(credits)

    return result or None


def _merge_codex_usage(total: dict[str, float], usage: dict[str, float] | None) -> None:
    if not usage:
        return
    for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
        if key in usage:
            total[key] = total.get(key, 0.0) + usage[key]

    for key in ("model_context_window", "credits"):
        if key in usage:
            total[key] = max(total.get(key, 0.0), usage[key])

    for key in ("primary_used_percent", "secondary_used_percent"):
        if key in usage:
            total[key] = max(total.get(key, 0.0), usage[key])


def _format_codex_usage(usage: dict[str, float], session_count: int) -> str:
    if not usage:
        return "Usage unavailable"

    parts = []
    if usage.get("total_tokens"):
        parts.append(f"tot {_compact_number(usage.get('total_tokens', 0))}")
    parts.append(f"in {_compact_number(usage.get('input_tokens', 0))}")
    parts.append(f"out {_compact_number(usage.get('output_tokens', 0))}")
    if usage.get("cached_input_tokens"):
        parts.append(f"cache {_compact_number(usage.get('cached_input_tokens', 0))}")
    if usage.get("reasoning_output_tokens"):
        parts.append(f"reason {_compact_number(usage.get('reasoning_output_tokens', 0))}")
    if usage.get("primary_used_percent"):
        parts.append(f"24h {_compact_percent(usage.get('primary_used_percent', 0))}")
    if usage.get("secondary_used_percent"):
        parts.append(f"30d {_compact_percent(usage.get('secondary_used_percent', 0))}")
    parts.append(f"{session_count} sess")
    return "Usage " + " | ".join(parts)


def _session_meta_codex(path: Path) -> dict[str, str]:
    meta = {"id": _short_id(path.stem.split("-")[-1]), "cwd": "-", "origin": "-", "name": path.name}
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(120):
                raw_line = handle.readline()
                if not raw_line:
                    break
                try:
                    entry = json.loads(raw_line)
                except Exception:
                    continue
                if not isinstance(entry, dict):
                    continue
                if entry.get("type") == "session_meta":
                    payload = entry.get("payload") or {}
                    meta["id"] = _short_id(payload.get("id") or meta["id"])
                    meta["cwd"] = str(payload.get("cwd") or meta["cwd"])
                    meta["origin"] = str(payload.get("originator") or meta["origin"])
                elif entry.get("type") == "event_msg":
                    payload = entry.get("payload") or {}
                    if payload.get("type") == "user_message":
                        text = _clip(payload.get("message", ""), 80)
                        if text:
                            meta["name"] = text
                            break
    except OSError:
        return meta

    if meta["name"] == path.name and meta["cwd"] != "-":
        meta["name"] = _clip(Path(meta["cwd"]).name or meta["name"], 80)
    return meta


def _latest_codex_summary_and_usage(rollout: Path, session_prefix: str) -> tuple[str, str, dict[str, float] | None]:
    latest_summary = "Watching Codex rollouts"
    latest_stamp = ""
    latest_usage: dict[str, float] | None = None

    try:
        for raw_line in reversed(_tail_lines(rollout)):
            entry = json.loads(raw_line)
            if not isinstance(entry, dict):
                continue
            if latest_usage is None:
                latest_usage = _extract_codex_usage(entry)
            rendered = _render_codex_entry(entry, session_prefix)
            if rendered:
                stamp_value, line = rendered[-1]
                if stamp_value >= latest_stamp:
                    latest_stamp = stamp_value
                    latest_summary = line
            if latest_usage is not None and rendered:
                break
    except Exception:
        pass

    return latest_summary, latest_stamp, latest_usage


def _pick_local_codex_rollouts() -> tuple[list[Path], bool]:
    sessions_dir = Path.home() / ".codex" / "sessions"
    if not sessions_dir.exists():
        return [], False

    candidates: list[tuple[float, Path]] = []
    for path in sessions_dir.rglob("rollout-*.jsonl"):
        try:
            stat = path.stat()
        except OSError:
            continue
        candidates.append((stat.st_mtime, path))

    if not candidates:
        return [], False

    candidates.sort(reverse=True)
    now = time.time()
    active = [path for mtime, path in candidates if now - mtime <= ACTIVE_WINDOW_SECONDS]
    return active, bool(active)


def _run_local_codex_probe(offsets: dict[str, int] | None) -> dict[str, object]:
    rollouts, any_active = _pick_local_codex_rollouts()
    if not rollouts:
        return _empty_result("No active Codex session found", "Waiting for a host Codex rollout updated within the last 10 minutes", offsets)

    events: list[tuple[str, str]] = []
    sessions: list[dict[str, str]] = []
    next_offsets: dict[str, int] = {}
    sources: list[str] = []
    latest_summary = "Watching Codex rollouts"
    latest_stamp = ""
    usage_totals: dict[str, float] = {}
    offset_map = offsets or {}

    for rollout in rollouts:
        meta = _session_meta_codex(rollout)
        source_key = str(rollout)
        current_offset = int(offset_map.get(source_key) or 0)
        try:
            chunk_lines, end_offset, _reset = _read_lines(rollout, current_offset)
        except OSError:
            continue

        session_prefix = meta["id"]
        summary_line, summary_stamp, latest_usage = _latest_codex_summary_and_usage(rollout, session_prefix)

        sessions.append({"id": session_prefix, "name": meta["name"]})
        next_offsets[source_key] = end_offset
        sources.append(source_key)
        _merge_codex_usage(usage_totals, latest_usage)

        for raw_line in chunk_lines:
            try:
                entry = json.loads(raw_line)
            except Exception:
                continue
            if isinstance(entry, dict):
                events.extend(_render_codex_entry(entry, session_prefix))

        if summary_stamp >= latest_stamp:
            latest_stamp = summary_stamp
            latest_summary = summary_line

    if not sessions:
        return _empty_result("No active Codex session found", "Waiting for a host Codex rollout updated within the last 10 minutes", offsets)

    events.sort(key=lambda item: item[0])
    sessions.sort(key=lambda item: item["id"])
    return {
        "status": "watching" if any_active else "idle",
        "summary": latest_summary,
        "detail": f"{len(sessions)} active Codex session(s)",
        "sources": sources,
        "offsets": next_offsets,
        "sessions": sessions,
        "events": [line for _stamp, line in events[-MAX_EVENTS:]],
        "usage_text": _format_codex_usage(usage_totals, len(sessions)),
    }


def _run_local_probe_once(agent_name: str, offsets: dict[str, int] | None) -> dict[str, object]:
    if agent_name == "Claude":
        return _run_local_claude_probe(offsets)
    return _run_local_codex_probe(offsets)


CLAUDE_PROBE_SCRIPT = dedent(
    """
    import json
    import sys
    import time
    from pathlib import Path

    INITIAL_BYTES = 65536
    TAIL_BYTES = 32768
    MAX_EVENTS = 80
    ACTIVE_WINDOW_SECONDS = 600


    def clip(value, limit=220):
        text = " ".join(str(value).split())
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."


    def short_id(value):
        return str(value).split("-")[0]


    def compact_number(value):
        try:
            number = float(value)
        except Exception:
            return "0"
        abs_number = abs(number)
        if abs_number >= 1000000000:
            rendered = f"{number / 1000000000:.1f}B"
        elif abs_number >= 1000000:
            rendered = f"{number / 1000000:.1f}M"
        elif abs_number >= 1000:
            rendered = f"{number / 1000:.1f}k"
        else:
            rendered = f"{number:.0f}"
        return rendered.replace(".0", "")


    def format_usage(usage, session_count):
        if not usage:
            return "Usage unavailable"
        parts = [f"in {compact_number(usage.get('input_tokens', 0))}", f"out {compact_number(usage.get('output_tokens', 0))}"]
        if usage.get("cache_read_input_tokens"):
            parts.append(f"cache {compact_number(usage.get('cache_read_input_tokens', 0))}")
        if usage.get("cache_creation_input_tokens"):
            parts.append(f"cache+ {compact_number(usage.get('cache_creation_input_tokens', 0))}")
        if usage.get("cost_usd"):
            parts.append(f"${usage.get('cost_usd', 0.0):.2f}")
        parts.append(f"{session_count} sess")
        return "Usage " + " | ".join(parts)


    def summarize_mapping(mapping):
        if not isinstance(mapping, dict):
            return clip(mapping)
        if mapping.get("command"):
            return clip(mapping["command"])
        if mapping.get("description"):
            return clip(mapping["description"])
        if mapping.get("task_id"):
            return "task " + clip(mapping["task_id"])
        return clip(json.dumps(mapping, ensure_ascii=False))


    def summarize_tool_result(result):
        if not isinstance(result, dict):
            return clip(result)
        stdout = (result.get("stdout") or "").strip()
        stderr = (result.get("stderr") or "").strip()
        if stdout:
            return clip(stdout)
        if stderr:
            return "stderr: " + clip(stderr)
        if result.get("interrupted"):
            return "interrupted"
        return "(no output)"


    def stamp(obj):
        value = obj.get("timestamp", "")
        return value[11:19] if len(value) >= 19 else "--:--:--"


    def timestamp_value(obj):
        return obj.get("timestamp", "")


    def render_entry(obj, session_prefix):
        lines = []
        when = stamp(obj)
        entry_type = obj.get("type")
        message = obj.get("message") or {}

        if entry_type == "assistant":
            for item in message.get("content", []):
                item_type = item.get("type")
                if item_type == "text":
                    text = clip(item.get("text", ""))
                    if text:
                        lines.append((timestamp_value(obj), f"{when} [{session_prefix}] assistant: {text}"))
                elif item_type == "thinking":
                    text = clip(item.get("thinking", ""))
                    if text:
                        lines.append((timestamp_value(obj), f"{when} [{session_prefix}] thinking: {text}"))
                elif item_type == "tool_use":
                    name = item.get("name", "tool")
                    summary = summarize_mapping(item.get("input") or {})
                    lines.append((timestamp_value(obj), f"{when} [{session_prefix}] tool {name}: {summary}"))

        elif entry_type == "user" and obj.get("toolUseResult"):
            lines.append((timestamp_value(obj), f"{when} [{session_prefix}] result: {summarize_tool_result(obj['toolUseResult'])}"))

        elif entry_type == "progress":
            data = obj.get("data") or {}
            summary = clip(data.get("taskDescription") or data.get("type") or "progress")
            lines.append((timestamp_value(obj), f"{when} [{session_prefix}] progress: {summary}"))

        return [line for line in lines if line[1]]


    def extract_usage(entry):
        message = entry.get("message") or {}
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return None
        result = {}
        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                result[key] = float(value)
        for key in ("total_cost_usd", "cost_usd", "total_cost"):
            value = usage.get(key)
            if isinstance(value, (int, float)):
                result["cost_usd"] = float(value)
                break
        return result or None


    def merge_usage(total, usage):
        if not usage:
            return
        for key, value in usage.items():
            total[key] = total.get(key, 0.0) + value


    def read_json(path):
        try:
            return json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            return None


    def is_alive_claude(pid):
        cmdline = Path("/proc") / str(pid) / "cmdline"
        if not cmdline.exists():
            return False
        try:
            command = cmdline.read_bytes().decode("utf-8", "ignore").replace("\\x00", " ")
        except Exception:
            return False
        return "claude" in command


    def locate_project_log(session_id, state_map):
        for source_path in state_map:
            current = Path(source_path)
            if current.name == session_id + ".jsonl" and current.exists():
                return current

        projects_dir = Path.home() / ".claude" / "projects"
        try:
            return next(projects_dir.rglob(session_id + ".jsonl"))
        except StopIteration:
            return None


    def session_name(log_path, cwd):
        fallback = Path(cwd).name or "claude-session"
        try:
            for raw_line in reversed(tail_lines(log_path)):
                entry = json.loads(raw_line)
                slug = entry.get("slug")
                if slug:
                    return clip(slug, 80)
                line_cwd = entry.get("cwd")
                if line_cwd:
                    fallback = Path(line_cwd).name or fallback
        except Exception:
            pass
        return clip(fallback, 80)


    def pick_active_sessions(state_map):
        sessions_dir = Path.home() / ".claude" / "sessions"
        candidates = []
        if sessions_dir.exists():
            for meta_path in sessions_dir.glob("*.json"):
                data = read_json(meta_path)
                if not isinstance(data, dict):
                    continue

                pid = int(data.get("pid") or 0)
                session_id = data.get("sessionId")
                if not pid or not session_id or not is_alive_claude(pid):
                    continue

                log_path = locate_project_log(session_id, state_map)
                if log_path is None:
                    continue

                candidates.append(
                    {
                        "pid": pid,
                        "session_id": session_id,
                        "cwd": data.get("cwd") or "-",
                        "started_at": int(data.get("startedAt") or 0),
                        "log_path": log_path,
                    }
                )

        if candidates:
            candidates.sort(key=lambda item: (item["started_at"], item["pid"]), reverse=True)
            return candidates

        projects_dir = Path.home() / ".claude" / "projects"
        if not projects_dir.exists():
            return []

        now = time.time()
        fallback = []
        for log_path in projects_dir.rglob("*.jsonl"):
            try:
                stat = log_path.stat()
            except OSError:
                continue
            if now - stat.st_mtime > ACTIVE_WINDOW_SECONDS:
                continue
            fallback.append(
                {
                    "pid": 0,
                    "session_id": log_path.stem,
                    "cwd": "-",
                    "started_at": int(stat.st_mtime),
                    "log_path": log_path,
                }
            )

        fallback.sort(key=lambda item: item["started_at"], reverse=True)
        return fallback


    def read_lines(path, current_offset):
        size = path.stat().st_size
        reset = current_offset <= 0 or current_offset > size
        start = max(size - INITIAL_BYTES, 0) if reset else current_offset

        with path.open("rb") as handle:
            handle.seek(start)
            if start:
                handle.readline()
            payload = handle.read()
            end_offset = handle.tell()

        text = payload.decode("utf-8", "replace")
        return text.splitlines(), end_offset, reset


    def tail_lines(path):
        size = path.stat().st_size
        start = max(size - TAIL_BYTES, 0)
        with path.open("rb") as handle:
            handle.seek(start)
            if start:
                handle.readline()
            payload = handle.read()
        return payload.decode("utf-8", "replace").splitlines()


    state_map = {}
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        try:
            state_map = json.loads(sys.argv[1])
        except Exception:
            state_map = {}

    active_sessions = pick_active_sessions(state_map)
    if not active_sessions:
        print(
            json.dumps(
                {
                    "status": "idle",
                    "summary": "No active Claude session found",
                    "detail": "Waiting for live Claude activity",
                    "sources": [],
                    "offsets": {},
                    "sessions": [],
                    "events": [],
                    "usage_text": "Usage unavailable",
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(0)

    events = []
    sessions = []
    offsets = {}
    sources = []
    latest_summary = "Watching Claude sessions"
    latest_stamp = ""
    usage_totals = {}

    for active in active_sessions:
        log_path = active["log_path"]
        source_key = str(log_path)
        current_offset = int(state_map.get(source_key) or 0)
        chunk_lines, end_offset, _reset = read_lines(log_path, current_offset)
        session_prefix = short_id(active["session_id"])
        name = session_name(log_path, active["cwd"])
        latest_usage = None

        sessions.append({"id": session_prefix, "name": name})
        offsets[source_key] = end_offset
        sources.append(source_key)

        for raw_line in chunk_lines:
            try:
                entry = json.loads(raw_line)
            except Exception:
                continue
            events.extend(render_entry(entry, session_prefix))

        try:
            for raw_line in reversed(tail_lines(log_path)):
                entry = json.loads(raw_line)
                if latest_usage is None:
                    latest_usage = extract_usage(entry)
                rendered = render_entry(entry, session_prefix)
                if rendered:
                    stamp_value, line = rendered[-1]
                    if stamp_value >= latest_stamp:
                        latest_stamp = stamp_value
                        latest_summary = line
                if latest_usage is not None and rendered:
                    break
        except Exception:
            pass

        merge_usage(usage_totals, latest_usage)

    events.sort(key=lambda item: item[0])
    rendered_events = [line for _stamp, line in events[-MAX_EVENTS:]]
    sessions.sort(key=lambda item: item["id"])

    result = {
        "status": "watching",
        "summary": latest_summary,
        "detail": f"{len(sessions)} active Claude session(s)",
        "sources": sources,
        "offsets": offsets,
        "sessions": sessions,
        "events": rendered_events,
        "usage_text": format_usage(usage_totals, len(sessions)),
    }
    print(json.dumps(result, ensure_ascii=False))
    """
)


CODEX_PROBE_SCRIPT = dedent(
    """
    import json
    import sys
    import time
    from pathlib import Path

    INITIAL_BYTES = 65536
    TAIL_BYTES = 32768
    MAX_EVENTS = 80
    ACTIVE_WINDOW_SECONDS = 600


    def clip(value, limit=220):
        text = " ".join(str(value).split())
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."


    def short_id(value):
        return str(value).split("-")[0]


    def compact_number(value):
        try:
            number = float(value)
        except Exception:
            return "0"
        abs_number = abs(number)
        if abs_number >= 1000000000:
            rendered = f"{number / 1000000000:.1f}B"
        elif abs_number >= 1000000:
            rendered = f"{number / 1000000:.1f}M"
        elif abs_number >= 1000:
            rendered = f"{number / 1000:.1f}k"
        else:
            rendered = f"{number:.0f}"
        return rendered.replace(".0", "")


    def compact_percent(value):
        try:
            number = float(value)
        except Exception:
            return "-"
        if number.is_integer():
            return f"{int(number)}%"
        return f"{number:.1f}%"


    def format_usage(usage, session_count):
        if not usage:
            return "Usage unavailable"
        parts = []
        if usage.get("total_tokens"):
            parts.append(f"tot {compact_number(usage.get('total_tokens', 0))}")
        parts.append(f"in {compact_number(usage.get('input_tokens', 0))}")
        parts.append(f"out {compact_number(usage.get('output_tokens', 0))}")
        if usage.get("cached_input_tokens"):
            parts.append(f"cache {compact_number(usage.get('cached_input_tokens', 0))}")
        if usage.get("reasoning_output_tokens"):
            parts.append(f"reason {compact_number(usage.get('reasoning_output_tokens', 0))}")
        if usage.get("primary_used_percent"):
            parts.append(f"24h {compact_percent(usage.get('primary_used_percent', 0))}")
        if usage.get("secondary_used_percent"):
            parts.append(f"30d {compact_percent(usage.get('secondary_used_percent', 0))}")
        parts.append(f"{session_count} sess")
        return "Usage " + " | ".join(parts)


    def compact_output(value):
        if not value:
            return "(no output)"
        return clip(value)


    def summarize_arguments(value):
        if not value:
            return ""
        return clip(value, 180)


    def pick_rollouts():
        sessions_dir = Path.home() / ".codex" / "sessions"
        if not sessions_dir.exists():
            return [], False

        candidates = []
        for path in sessions_dir.rglob("rollout-*.jsonl"):
            try:
                stat = path.stat()
            except OSError:
                continue
            candidates.append((stat.st_mtime, path))

        if not candidates:
            return [], False
        candidates.sort(reverse=True)
        now = time.time()
        active = [path for mtime, path in candidates if now - mtime <= ACTIVE_WINDOW_SECONDS]
        return active, bool(active)


    def read_lines(path, current_offset):
        size = path.stat().st_size
        reset = current_offset <= 0 or current_offset > size
        start = max(size - INITIAL_BYTES, 0) if reset else current_offset

        with path.open("rb") as handle:
            handle.seek(start)
            if start:
                handle.readline()
            payload = handle.read()
            end_offset = handle.tell()

        return payload.decode("utf-8", "replace").splitlines(), end_offset, reset


    def tail_lines(path):
        size = path.stat().st_size
        start = max(size - TAIL_BYTES, 0)
        with path.open("rb") as handle:
            handle.seek(start)
            if start:
                handle.readline()
            payload = handle.read()
        return payload.decode("utf-8", "replace").splitlines()


    def stamp(obj):
        value = obj.get("timestamp", "")
        return value[11:19] if len(value) >= 19 else "--:--:--"


    def timestamp_value(obj):
        return obj.get("timestamp", "")


    def render_response_message(payload, when, session_prefix, timestamp):
        lines = []
        phase = payload.get("phase")
        for item in payload.get("content", []):
            if item.get("type") != "output_text":
                continue
            text = clip(item.get("text", ""))
            if not text:
                continue
            label = "assistant"
            if phase == "commentary":
                label = "commentary"
            elif phase == "final_answer":
                label = "final"
            lines.append((timestamp, f"{when} [{session_prefix}] {label}: {text}"))
        return lines


    def render_entry(obj, session_prefix):
        when = stamp(obj)
        entry_type = obj.get("type")
        payload = obj.get("payload") or {}
        timestamp = timestamp_value(obj)

        if entry_type == "event_msg":
            kind = payload.get("type")
            if kind == "agent_message":
                text = clip(payload.get("message", ""))
                return [(timestamp, f"{when} [{session_prefix}] status: {text}")] if text else []
            if kind == "task_started":
                turn_id = clip(payload.get("turn_id", "task"))
                return [(timestamp, f"{when} [{session_prefix}] task started: {turn_id}")]
            if kind == "task_complete":
                text = clip(payload.get("last_agent_message", "task complete"))
                return [(timestamp, f"{when} [{session_prefix}] task complete: {text}")]
            if kind == "user_message":
                text = clip(payload.get("message", ""))
                return [(timestamp, f"{when} [{session_prefix}] user: {text}")] if text else []
            return []

        if entry_type != "response_item":
            return []

        payload_type = payload.get("type")
        if payload_type == "message" and payload.get("role") == "assistant":
            return render_response_message(payload, when, session_prefix, timestamp)
        if payload_type == "function_call":
            name = payload.get("name", "function_call")
            arguments = summarize_arguments(payload.get("arguments"))
            return [(timestamp, f"{when} [{session_prefix}] function {name}: {arguments}")]
        if payload_type == "function_call_output":
            return [(timestamp, f"{when} [{session_prefix}] output: {compact_output(payload.get('output', ''))}")]
        if payload_type == "custom_tool_call":
            name = payload.get("name", "tool")
            arguments = summarize_arguments(payload.get("input"))
            return [(timestamp, f"{when} [{session_prefix}] tool {name}: {arguments}")]
        if payload_type == "custom_tool_call_output":
            return [(timestamp, f"{when} [{session_prefix}] tool output: {compact_output(payload.get('output', ''))}")]
        return []


    def extract_usage(entry):
        if entry.get("type") != "event_msg":
            return None
        payload = entry.get("payload") or {}
        if payload.get("type") != "token_count":
            return None
        result = {}
        info = payload.get("info") or {}
        total_usage = info.get("total_token_usage") or {}
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
            value = total_usage.get(key)
            if isinstance(value, (int, float)):
                result[key] = float(value)
        rate_limits = payload.get("rate_limits") or {}
        primary = (rate_limits.get("primary") or {}).get("used_percent")
        secondary = (rate_limits.get("secondary") or {}).get("used_percent")
        credits = rate_limits.get("credits")
        if isinstance(primary, (int, float)):
            result["primary_used_percent"] = float(primary)
        if isinstance(secondary, (int, float)):
            result["secondary_used_percent"] = float(secondary)
        if isinstance(credits, (int, float)):
            result["credits"] = float(credits)
        return result or None


    def merge_usage(total, usage):
        if not usage:
            return
        for key in ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens", "total_tokens"):
            if key in usage:
                total[key] = total.get(key, 0.0) + usage[key]
        for key in ("primary_used_percent", "secondary_used_percent", "credits"):
            if key in usage:
                total[key] = max(total.get(key, 0.0), usage[key])


    def session_meta(path):
        meta = {"id": short_id(path.stem.split("-")[-1]), "cwd": "-", "origin": "-", "name": path.name}
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(120):
                raw_line = handle.readline()
                if not raw_line:
                    break
                try:
                    entry = json.loads(raw_line)
                except Exception:
                    continue
                if entry.get("type") == "session_meta":
                    payload = entry.get("payload") or {}
                    meta["id"] = short_id(payload.get("id") or meta["id"])
                    meta["cwd"] = payload.get("cwd") or meta["cwd"]
                    meta["origin"] = payload.get("originator") or meta["origin"]
                elif entry.get("type") == "event_msg":
                    payload = entry.get("payload") or {}
                    if payload.get("type") == "user_message":
                        text = clip(payload.get("message", ""), 80)
                        if text:
                            meta["name"] = text
                            break
        if meta["name"] == path.name and meta["cwd"] != "-":
            meta["name"] = clip(Path(meta["cwd"]).name or meta["name"], 80)
        return meta


    state_map = {}
    if len(sys.argv) > 1 and sys.argv[1] != "-":
        try:
            state_map = json.loads(sys.argv[1])
        except Exception:
            state_map = {}

    rollouts, any_active = pick_rollouts()
    if not rollouts:
        print(
            json.dumps(
                {
                    "status": "idle",
                    "summary": "No active Codex session found",
                    "detail": "Waiting for a Codex rollout updated within the last 10 minutes",
                    "sources": [],
                    "offsets": {},
                    "sessions": [],
                    "events": [],
                    "usage_text": "Usage unavailable",
                },
                ensure_ascii=False,
            )
        )
        raise SystemExit(0)

    events = []
    sessions = []
    offsets = {}
    sources = []
    latest_summary = "Watching Codex rollouts"
    latest_stamp = ""
    usage_totals = {}

    for rollout in rollouts:
        meta = session_meta(rollout)
        source_key = str(rollout)
        current_offset = int(state_map.get(source_key) or 0)
        chunk_lines, end_offset, _reset = read_lines(rollout, current_offset)
        session_prefix = meta["id"]
        latest_usage = None

        sessions.append({"id": session_prefix, "name": meta["name"]})
        offsets[source_key] = end_offset
        sources.append(source_key)

        for raw_line in chunk_lines:
            try:
                entry = json.loads(raw_line)
            except Exception:
                continue
            events.extend(render_entry(entry, session_prefix))

        try:
            for raw_line in reversed(tail_lines(rollout)):
                entry = json.loads(raw_line)
                if latest_usage is None:
                    latest_usage = extract_usage(entry)
                rendered = render_entry(entry, session_prefix)
                if rendered:
                    stamp_value, line = rendered[-1]
                    if stamp_value >= latest_stamp:
                        latest_stamp = stamp_value
                        latest_summary = line
                if latest_usage is not None and rendered:
                    break
        except Exception:
            pass

        merge_usage(usage_totals, latest_usage)

    events.sort(key=lambda item: item[0])
    rendered_events = [line for _stamp, line in events[-MAX_EVENTS:]]
    sessions.sort(key=lambda item: item["id"])

    result = {
        "status": "watching" if any_active else "idle",
        "summary": latest_summary,
        "detail": f"{len(sessions)} active Codex session(s)",
        "sources": sources,
        "offsets": offsets,
        "sessions": sessions,
        "events": rendered_events,
        "usage_text": format_usage(usage_totals, len(sessions)),
    }
    print(json.dumps(result, ensure_ascii=False))
    """
)


PROBE_SCRIPTS = {
    "Claude": CLAUDE_PROBE_SCRIPT,
    "Codex": CODEX_PROBE_SCRIPT,
}


def run_probe_once(
    agent_name: str,
    distro: str,
    offsets: dict[str, int] | None,
    probe_target: str = "wsl",
) -> dict[str, object]:
    if probe_target != "wsl":
        try:
            return _run_local_probe_once(agent_name, offsets)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "summary": "Probe failed",
                "detail": str(exc),
                "sources": [],
                "offsets": offsets or {},
                "sessions": [],
                "events": [],
                "usage_text": "Usage unavailable",
            }

    script = PROBE_SCRIPTS[agent_name]
    payload = json.dumps(offsets or {}, ensure_ascii=False)
    command = ["wsl.exe"]
    if distro:
        command.extend(["-d", distro])
    command.extend(["python3", "-", payload])

    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        completed = subprocess.run(
            command,
            input=script,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PROBE_TIMEOUT_SECONDS,
            creationflags=creationflags,
        )
    except FileNotFoundError as exc:
        return {
            "status": "error",
            "summary": "Probe failed",
            "detail": str(exc),
            "sources": [],
            "offsets": offsets or {},
            "sessions": [],
            "events": [],
            "usage_text": "Usage unavailable",
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "error",
            "summary": "Probe timed out",
            "detail": f"WSL probe exceeded {PROBE_TIMEOUT_SECONDS}s",
            "sources": [],
            "offsets": offsets or {},
            "sessions": [],
            "events": [],
            "usage_text": "Usage unavailable",
        }

    if completed.returncode != 0:
        error_text = completed.stderr.strip() or completed.stdout.strip() or f"wsl exited with code {completed.returncode}"
        return {
            "status": "error",
            "summary": "Probe failed",
            "detail": error_text,
            "sources": [],
            "offsets": offsets or {},
            "sessions": [],
            "events": [],
            "usage_text": "Usage unavailable",
        }

    raw_output = completed.stdout.strip()
    if not raw_output:
        return {
            "status": "error",
            "summary": "Probe returned no data",
            "detail": "The WSL helper produced no JSON payload",
            "sources": [],
            "offsets": offsets or {},
            "sessions": [],
            "events": [],
            "usage_text": "Usage unavailable",
        }

    try:
        result = json.loads(raw_output.splitlines()[-1])
    except json.JSONDecodeError as exc:
        return {
            "status": "error",
            "summary": "Probe returned invalid JSON",
            "detail": f"{exc}: {raw_output[:300]}",
            "sources": [],
            "offsets": offsets or {},
            "sessions": [],
            "events": [],
            "usage_text": "Usage unavailable",
        }

    if not isinstance(result, dict):
        return {
            "status": "error",
            "summary": "Probe returned invalid JSON",
            "detail": "The WSL helper returned a non-object payload",
            "sources": [],
            "offsets": offsets or {},
            "sessions": [],
            "events": [],
            "usage_text": "Usage unavailable",
        }

    result.setdefault("usage_text", "Usage unavailable")
    return result
