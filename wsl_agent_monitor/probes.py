from __future__ import annotations

import json
import subprocess
from textwrap import dedent

from .constants import PROBE_TIMEOUT_SECONDS


CLAUDE_PROBE_SCRIPT = dedent(
    """
    import json
    import sys
    from pathlib import Path

    INITIAL_BYTES = 65536
    TAIL_BYTES = 32768
    MAX_EVENTS = 80


    def clip(value, limit=220):
        text = " ".join(str(value).split())
        if len(text) <= limit:
            return text
        return text[: limit - 3] + "..."


    def short_id(value):
        return str(value).split("-")[0]


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
        except Exception:
            pass
        return clip(fallback, 80)


    def pick_active_sessions(state_map):
        sessions_dir = Path.home() / ".claude" / "sessions"
        candidates = []
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

        candidates.sort(key=lambda item: (item["started_at"], item["pid"]), reverse=True)
        return candidates


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
                    "detail": "Waiting for live claude processes in WSL",
                    "sources": [],
                    "offsets": {},
                    "sessions": [],
                    "events": [],
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

    for active in active_sessions:
        log_path = active["log_path"]
        source_key = str(log_path)
        current_offset = int(state_map.get(source_key) or 0)
        chunk_lines, end_offset, _reset = read_lines(log_path, current_offset)
        session_prefix = short_id(active["session_id"])
        name = session_name(log_path, active["cwd"])

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
                rendered = render_entry(entry, session_prefix)
                if rendered:
                    stamp_value, line = rendered[-1]
                    if stamp_value >= latest_stamp:
                        latest_stamp = stamp_value
                        latest_summary = line
                    break
        except Exception:
            pass

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

    for rollout in rollouts:
        meta = session_meta(rollout)
        source_key = str(rollout)
        current_offset = int(state_map.get(source_key) or 0)
        chunk_lines, end_offset, _reset = read_lines(rollout, current_offset)
        session_prefix = meta["id"]

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
                rendered = render_entry(entry, session_prefix)
                if rendered:
                    stamp_value, line = rendered[-1]
                    if stamp_value >= latest_stamp:
                        latest_stamp = stamp_value
                        latest_summary = line
                    break
        except Exception:
            pass

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
    }
    print(json.dumps(result, ensure_ascii=False))
    """
)


PROBE_SCRIPTS = {
    "Claude": CLAUDE_PROBE_SCRIPT,
    "Codex": CODEX_PROBE_SCRIPT,
}


def run_probe_once(agent_name: str, distro: str, offsets: dict[str, int] | None) -> dict[str, object]:
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
        }

    try:
        return json.loads(raw_output.splitlines()[-1])
    except json.JSONDecodeError as exc:
        return {
            "status": "error",
            "summary": "Probe returned invalid JSON",
            "detail": f"{exc}: {raw_output[:300]}",
            "sources": [],
            "offsets": offsets or {},
            "sessions": [],
            "events": [],
        }
