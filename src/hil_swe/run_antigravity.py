"""
Google Antigravity harness for trust_horizon HiL-SWE runs.

Outputs to OUTPUT_DIR:
  attempt.json
  trajectory.json
  stats.json
  patch.diff
  result.json
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import typing as _typing
from datetime import datetime, timezone
from functools import lru_cache
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib import error as _urllib_error
from urllib import request as _urllib_request

if not hasattr(_typing, "Self"):
    from typing_extensions import Self as _typing_self  # type: ignore[import]
    _typing.Self = _typing_self  # type: ignore[attr-defined]

from google.antigravity import Agent, LocalAgentConfig
from google.antigravity.connections.local import local_connection
from google.antigravity.connections.local import localharness_pb2
from google.antigravity.hooks import hooks
from google.antigravity.hooks import policy
from google.antigravity.types import (
    CapabilitiesConfig,
    GenerationConfig,
    GeminiConfig,
    McpStdioServer,
    ModelConfig,
    ModelEntry,
    QuestionHookResult,
    QuestionResponse,
    ThinkingLevel,
)


# ── Environment constants ─────────────────────────────────────────────────────

WORKSPACE = "/app"
TASK_DIR = os.environ.get("TASK_DIR", "/task")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")


def _normalize_mode(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return "ask_human"
    if raw in {"ask_human", "full_info"}:
        return raw
    raise ValueError(f"Unknown MODE={raw!r}. Expected ask_human or full_info.")


MODE = _normalize_mode(os.environ.get("MODE", "ask_human"))
ASK_HUMAN_ENABLED = MODE == "ask_human"
FULL_INFO_ENABLED = MODE == "full_info"
WITH_CUSTOM_TOOL = ASK_HUMAN_ENABLED and re.match(
    r"^(1|true|yes|on)$", str(os.environ.get("WITH_CUSTOM_TOOL", "0")), re.IGNORECASE
) is not None
SKILL_TEMPLATE_VERSION = str(os.environ.get("WITH_SKILL", "")).strip() if ASK_HUMAN_ENABLED else ""
SKILL_ENABLED = ASK_HUMAN_ENABLED and bool(SKILL_TEMPLATE_VERSION)
ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION = str(os.environ.get("WITH_ASK_GUIDANCE", "")).strip() if ASK_HUMAN_ENABLED else ""
ASK_HUMAN_GUIDANCE_ENABLED = ASK_HUMAN_ENABLED and bool(ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION)
PASS_INDEX = int(os.environ.get("PASS_INDEX", "1"))
RUN_ID = os.environ.get("RUN_ID", "swe-run")
TIMEOUT_MS = int(os.environ.get("ATTEMPT_TIMEOUT_MS", str(3 * 3_600_000)))
LITELLM_CALL_TIMEOUT_S = float(os.environ.get("LITELLM_CALL_TIMEOUT_MS", str(20 * 60 * 1000))) / 1000.0
STEP_LITELLM_TRIES = int(os.environ.get("STEP_LITELLM_TRIES", "3"))
MAX_STEPS = int(os.environ.get("MAX_STEPS", "0"))
ANTIGRAVITY_MODEL = os.environ.get("ANTIGRAVITY_MODEL", "gemini/gemini-3.5-flash")
ANTIGRAVITY_REASONING_EFFORT = (os.environ.get("ANTIGRAVITY_REASONING_EFFORT", "") or "").strip().lower()
LITELLM_BASE_URL = os.environ.get("LITELLM_BASE_URL", "")
LITELLM_API_KEY = (
    os.environ.get("LITELLM_API_KEY")
    or os.environ.get("LITELLM_PROXY_API_KEY")
    or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    or ""
)

# Sidecar uses this to judge ask_human calls.
ASK_HUMAN_BASE_URL = (os.environ.get("ASK_HUMAN_BASE_URL") or "").strip()
ASK_HUMAN_MODEL = os.environ.get("ASK_HUMAN_MODEL", "llmengine/llama-3-3-70b-instruct")

THOUGHT_CAP = 4_000
ACT_CAP = 4_000
OBS_CAP = 8_000
UNKNOWN_RESOLUTION = "irrelevant question"
UNKNOWN_BLOCKER_ID = "UNKNOWN"
CANT_ANSWER = "can't answer (perhaps transient hiccup)"
ASK_HUMAN_REQUEST_TYPES = frozenset({"clarification", "elicitation"})

SKILL_NAME = "clarify-information"
SKILL_TOOL_REF = "ask_question and/or ask_human"
_TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
_TEMPLATE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

_SIDECAR_SCRIPT = str(Path(__file__).resolve().parent / "ask_human_sidecar.mjs")
_BRIDGE_SCRIPT = str(Path(__file__).resolve().parent / "ask_human_mcp_bridge.mjs")


def _cap(s: Any, limit: int) -> str:
    text = str(s or "")
    return f"{text[:limit]}… [truncated]" if len(text) > limit else text


def _is_token_limit_error(text: Any) -> bool:
    s = str(text or "").strip().lower()
    if not s:
        return False
    return (
        "contextwindowexceeded" in s
        or "max_output_tokens" in s
        or "max output token" in s
        or "max_tokens" in s
        or "token limit" in s
        or "generation exceeded max tokens" in s
        or "generation exceeded the maximum output token limit" in s
        or "context window" in s
        or "context length" in s
    )


_TOKEN_LIMIT_CODES = {
    "contextwindowexceeded",
    "max_output_tokens",
    "max_tokens",
    "token_limit",
    "context_length_exceeded",
    "length",
}


def _normalize_code(value: Any) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower())


def _is_token_limit_structured(value: Any, depth: int = 0) -> bool:
    if depth > 7 or value is None:
        return False
    if isinstance(value, str):
        return _normalize_code(value) in _TOKEN_LIMIT_CODES
    if isinstance(value, (list, tuple, set)):
        return any(_is_token_limit_structured(v, depth + 1) for v in value)
    if isinstance(value, dict):
        for k, v in value.items():
            key = str(k or "").lower()
            if key in {"codexerrorinfo", "error", "details", "additionaldetails", "cause", "data"}:
                if _is_token_limit_structured(v, depth + 1):
                    return True
            if key in {
                "code", "type", "subtype", "reason", "stop_reason", "stopreason",
                "finish_reason", "finishreason", "errorcode", "error_code",
            }:
                if _normalize_code(v) in _TOKEN_LIMIT_CODES:
                    return True
            if _is_token_limit_structured(v, depth + 1):
                return True
        return False
    if hasattr(value, "__dict__"):
        return _is_token_limit_structured(vars(value), depth + 1)
    return False


def _is_token_limit_exception(exc: BaseException) -> bool:
    if _is_token_limit_structured(exc):
        return True
    if _is_token_limit_structured(getattr(exc, "args", ())):
        return True
    return _is_token_limit_error(exc)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_json(path: str | Path, data: Any) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _resolve_template_path(flag_name: str, version: str, extension: str) -> Path:
    if not version:
        raise RuntimeError(f"{flag_name} must be set when this feature is enabled.")
    if not _TEMPLATE_VERSION_RE.fullmatch(version):
        raise RuntimeError(
            f"{flag_name} invalid value {version!r}. "
            "Use only letters, digits, dot, underscore, or hyphen."
        )
    path = _TEMPLATE_DIR / f"{version}.{extension}"
    if not path.exists():
        raise RuntimeError(f"{flag_name}={version!r} requires template file {path}.")
    return path


@lru_cache(maxsize=8)
def _shared_skill_template(version: str) -> str:
    path = _resolve_template_path("WITH_SKILL", version, "md")
    return path.read_text(encoding="utf-8")


def _render_shared_skill(tool_name: str) -> str:
    return re.sub(r"\{\{\s*TOOL_NAME\s*\}\}", str(tool_name or ""), _shared_skill_template(SKILL_TEMPLATE_VERSION))


def _install_workspace_skill_for_discovery(workspace: str, tool_name: str) -> Path:
    skill_dir = Path(workspace) / "skills" / SKILL_NAME
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_path = skill_dir / "SKILL.md"
    skill_path.write_text(_render_shared_skill(tool_name), encoding="utf-8")
    return skill_dir


def _remove_workspace_ask_human_skill_dirs(workspace: str) -> None:
    root = Path(workspace)
    for rel in (
        Path(".claude") / "skills" / SKILL_NAME,
        Path(".agents") / "skills" / SKILL_NAME,
        Path(".opencode") / "skills" / SKILL_NAME,
        Path("skills") / SKILL_NAME,
    ):
        d = root / rel
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


@lru_cache(maxsize=8)
def _ask_human_guidance_template(version: str) -> str:
    path = _resolve_template_path("WITH_ASK_GUIDANCE", version, "txt")
    return path.read_text(encoding="utf-8")


def _build_ask_human_guidance(tool_name: str) -> str:
    return re.sub(
        r"\{\{\s*TOOL_NAME\s*\}\}",
        str(tool_name or ""),
        _ask_human_guidance_template(ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION),
    )


def _build_instruction(mode: str) -> str:
    base = "You are a helpful assistant that can interact with a computer to solve tasks."
    if _normalize_mode(mode) == "ask_human" and ASK_HUMAN_GUIDANCE_ENABLED:
        return f"{base}\n\n{_build_ask_human_guidance(SKILL_TOOL_REF)}"
    return base


def _instance_template(problem_statement: str) -> str:
    return "\n".join([
        "<uploaded_files>",
        WORKSPACE,
        "</uploaded_files>",
        f"I've uploaded a code repository in the directory {WORKSPACE}. Consider the following PR description:",
        "",
        "<pr_description>",
        problem_statement,
        "</pr_description>",
        "",
        "Can you help me implement the necessary changes to the repository so that the requirements specified in the <pr_description> are met?",
        "I've already taken care of all changes to any of the test files described in the <pr_description>. This means you DON'T have to modify the testing logic or any of the tests in any way!",
        f"Your task is to make the minimal changes to non-tests files in the {WORKSPACE} directory to ensure the <pr_description> is satisfied. **Do not make changes for anything that isn't explicitly or implicitly requested in the <pr_description>.**",
        "",
        "Explore the codebase only as much as you need to understand the problem; prioritize actually implementing and testing your changes. Think about edge cases and make sure your changes handle them as well!",
        "",
        "If you encounter trouble using any tool, find alternative ways to achieve the same goal, e.g. different tool arguments, a set of different tools, etc.",
    ])


def _build_full_info_prompt(problem_statement: str, blockers: list[dict]) -> str:
    if not blockers:
        return _instance_template(problem_statement)
    sections = "\n\n".join(f"### {b['description']}\n\n{b['resolution']}" for b in blockers)
    augmented = "\n".join([
        problem_statement, "", "---", "", "## Additional Context", "",
        "The following clarifications are provided to help you complete this task:",
        "", sections, "",
    ])
    return _instance_template(augmented)


def build_swe_prompt(problem_statement: str, mode: str, blockers: list[dict]) -> str:
    if mode == "full_info":
        return _build_full_info_prompt(problem_statement, blockers)
    if mode == "ask_human":
        return _instance_template(problem_statement)
    raise ValueError(f"Unknown mode: {mode!r}. Expected ask_human or full_info.")


def _start_sidecar(uid: str) -> tuple[subprocess.Popen, str]:
    env = {**os.environ, "TASK_UID": uid}
    proc = subprocess.Popen(
        ["node", _SIDECAR_SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )

    def _drain_stderr() -> None:
        for _ in proc.stderr:
            pass

    import threading
    threading.Thread(target=_drain_stderr, daemon=True).start()
    line = proc.stdout.readline()
    decoded = line.decode().strip()
    if not decoded.startswith("SIDECAR_PORT="):
        rc = proc.poll()
        proc.kill()
        raise RuntimeError(
            "ask_human sidecar did not announce its port "
            f"(exit_code={rc}). Got: {decoded!r}"
        )
    port = int(decoded.split("=", 1)[1])
    url = f"http://127.0.0.1:{port}"
    for _ in range(20):
        try:
            with _urllib_request.urlopen(f"{url}/health", timeout=2) as resp:
                if resp.status == 200:
                    return proc, url
        except Exception:
            pass
        time.sleep(0.25)
    proc.kill()
    raise RuntimeError("ask_human sidecar health check failed after 20 attempts")


def _stop_sidecar(proc: Optional[subprocess.Popen]) -> None:
    if proc is None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _start_litellm_auth_proxy(upstream_base_url: str, api_key: str) -> tuple[ThreadingHTTPServer, str]:
    upstream = upstream_base_url.rstrip("/")
    auth_header = f"Bearer {api_key}" if api_key else ""

    class _ProxyHandler(BaseHTTPRequestHandler):
        def _forward(self) -> None:
            content_len = int(self.headers.get("Content-Length", "0") or "0")
            body = self.rfile.read(content_len) if content_len > 0 else b""
            target_url = f"{upstream}{self.path}"
            outbound_headers: dict[str, str] = {}
            for k, v in self.headers.items():
                lk = k.lower()
                if lk in {"host", "content-length", "connection", "transfer-encoding"}:
                    continue
                outbound_headers[k] = v
            if auth_header and "Authorization" not in outbound_headers and "x-goog-api-key" not in outbound_headers:
                outbound_headers["Authorization"] = auth_header

            req = _urllib_request.Request(
                target_url,
                data=body if self.command in {"POST", "PUT", "PATCH"} else None,
                headers=outbound_headers,
                method=self.command,
            )

            try:
                with _urllib_request.urlopen(req, timeout=1200) as resp:
                    resp_code = int(getattr(resp, "status", 200) or 200)
                    resp_headers = list(resp.headers.items())
                    resp_body = resp.read()
            except _urllib_error.HTTPError as exc:
                resp_code = int(getattr(exc, "code", 500) or 500)
                resp_headers = list(getattr(exc, "headers", {}).items()) if getattr(exc, "headers", None) else []
                resp_body = exc.read() if hasattr(exc, "read") else b""
            except Exception as exc:
                payload = json.dumps({"error": str(exc)}).encode("utf-8", errors="replace")
                self.send_response(502)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return

            self.send_response(resp_code)
            for hk, hv in resp_headers:
                lk = hk.lower()
                if lk in {"transfer-encoding", "connection", "content-length"}:
                    continue
                self.send_header(hk, hv)
            self.send_header("Content-Length", str(len(resp_body)))
            self.end_headers()
            if resp_body:
                try:
                    self.wfile.write(resp_body)
                except BrokenPipeError:
                    return

        def do_GET(self) -> None:  # noqa: N802
            self._forward()

        def do_POST(self) -> None:  # noqa: N802
            self._forward()

        def do_PUT(self) -> None:  # noqa: N802
            self._forward()

        def do_PATCH(self) -> None:  # noqa: N802
            self._forward()

        def do_DELETE(self) -> None:  # noqa: N802
            self._forward()

        def log_message(self, fmt: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{server.server_port}"


def _stop_litellm_auth_proxy(server: Optional[ThreadingHTTPServer]) -> None:
    if server is None:
        return
    try:
        server.shutdown()
    except Exception:
        pass
    try:
        server.server_close()
    except Exception:
        pass


def _sidecar_ask_sync(url: str, payload: dict) -> dict:
    body = json.dumps(payload).encode()
    req = _urllib_request.Request(
        f"{url}/ask",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urllib_request.urlopen(req, timeout=1200) as resp:
        return json.loads(resp.read())


def _sidecar_events_sync(url: str) -> list[dict]:
    with _urllib_request.urlopen(f"{url}/events", timeout=30) as resp:
        data = json.loads(resp.read())
    events = data.get("events") if isinstance(data, dict) else []
    return events if isinstance(events, list) else []


def _sidecar_events_reset_sync(url: str) -> None:
    req = _urllib_request.Request(
        f"{url}/events/reset",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with _urllib_request.urlopen(req, timeout=30):
        return


def _slice_sidecar_events(events: list[dict], start_index: int) -> list[dict]:
    if start_index <= 0:
        return list(events)
    if start_index >= len(events):
        return []
    return list(events[start_index:])


def _merge_sidecar_events(all_events: list[dict], sidecar_events: list[dict]) -> None:
    """
    Merge sidecar events into all_events while avoiding duplicate ask-human rows.

    Native Antigravity ask flow already appends sidecar human_input_* events at
    question time. A final /events fetch is still useful for custom-tool events,
    but can duplicate native events. Deduplicate by (type, request_id) for
    human_input_* events; append everything else as-is.
    """
    seen_request_events: set[tuple[str, str]] = set()
    for ev in all_events:
        if not isinstance(ev, dict):
            continue
        ev_type = str(ev.get("type", ""))
        if not ev_type.startswith("human_input_"):
            continue
        req_id = str(ev.get("request_id", "") or "")
        if req_id:
            seen_request_events.add((ev_type, req_id))

    for ev in sidecar_events:
        if not isinstance(ev, dict):
            continue
        ev_type = str(ev.get("type", ""))
        if ev_type.startswith("human_input_"):
            req_id = str(ev.get("request_id", "") or "")
            if req_id:
                key = (ev_type, req_id)
                if key in seen_request_events:
                    continue
                seen_request_events.add(key)
        all_events.append(ev)


def _antigravity_thinking_level(effort: str) -> Optional[ThinkingLevel]:
    val = (effort or "").strip().lower()
    if val in {"low", "minimal"}:
        return ThinkingLevel.LOW
    if val == "medium":
        return ThinkingLevel.MEDIUM
    if val in {"high", "xhigh", "max"}:
        return ThinkingLevel.HIGH
    return None


def _litellm_base_url(value: str) -> str:
    url = (value or "").strip().replace("localhost", "host.docker.internal")
    return url.rstrip("/")


def _custom_mcp_stdio_server(sidecar_url: str) -> McpStdioServer:
    return McpStdioServer(
        command="env",
        args=[
            f"SIDECAR_URL={sidecar_url}",
            "NATIVE_EVENT_TYPE=antigravity.mcp.ask_human",
            "node",
            _BRIDGE_SCRIPT,
        ],
    )


class TrustHorizonLocalConnectionStrategy(local_connection.LocalConnectionStrategy):
    def __init__(self, *, gemini_base_url: str = "", **kwargs: Any):
        self._th_gemini_base_url = gemini_base_url
        super().__init__(**kwargs)

    def _build_harness_config(self) -> localharness_pb2.HarnessConfig:
        cfg = super()._build_harness_config()
        if self._th_gemini_base_url and cfg.HasField("gemini_config"):
            cfg.gemini_config.base_url = self._th_gemini_base_url
        return cfg


class TrustHorizonLocalAgentConfig(LocalAgentConfig):
    litellm_base_url: str = ""

    def create_strategy(self, *, tool_runner: Any, hook_runner: Any):
        if isinstance(self.system_instructions, str):
            from google.antigravity import types as ag_types
            si = ag_types.TemplatedSystemInstructions(
                sections=[ag_types.SystemInstructionSection(content=self.system_instructions)]
            )
        else:
            si = self.system_instructions

        save_dir = self.save_dir
        if save_dir is None:
            save_dir = tempfile.mkdtemp(prefix="antigravity_")
            logging.info("No save_dir specified; using %s", save_dir)

        return TrustHorizonLocalConnectionStrategy(
            tool_runner=tool_runner,
            hook_runner=hook_runner,
            gemini_config=self.gemini_config,
            system_instructions=si,
            capabilities_config=self.capabilities,
            conversation_id=self.conversation_id,
            save_dir=save_dir,
            workspaces=self.workspaces,
            app_data_dir=self.app_data_dir,
            skills_paths=self.skills_paths,
            gemini_base_url=self.litellm_base_url,
        )


def _read_ask_question_from_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        if not value:
            return ""
        try:
            parsed = json.loads(value)
            return _read_ask_question_from_value(parsed)
        except Exception:
            return value
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("question"), str):
        return value["question"]
    for nested_key in ("arguments", "input", "ask_human"):
        nested = value.get(nested_key)
        if isinstance(nested, dict):
            q = _read_ask_question_from_value(nested)
            if q != "":
                return q
    return ""


def _safe_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _normalize_trajectory_steps(steps: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    for raw in steps:
        if not isinstance(raw, dict):
            continue
        thought = str(raw.get("thought", "") or "")
        act = str(raw.get("act", "") or "")
        obs = str(raw.get("obs", "") or "")
        if obs.startswith("[no observation — tool call was denied or interrupted]"):
            obs = obs.replace(
                "[no observation — tool call was denied or interrupted]",
                "[no observation — tool call was interrupted]",
            )
        if not thought.strip() and not act.strip() and not obs.strip():
            continue
        updated = dict(raw)
        updated["thought"] = thought
        updated["act"] = act
        updated["obs"] = obs
        normalized.append(updated)
    return normalized


def _render_tool_obs(result_event: dict) -> str:
    if result_event.get("error"):
        return str(result_event["error"])
    result = result_event.get("result")
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if isinstance(structured, (dict, list)):
            return _safe_json_dumps(structured)
        if isinstance(structured, str):
            return structured
        content = result.get("content")
        if isinstance(content, list):
            text_parts: list[str] = []
            for item in content:
                if isinstance(item, str) and item.strip():
                    text_parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("text"), str) and item.get("text", "").strip():
                    text_parts.append(str(item["text"]))
            if text_parts:
                return "\n".join(text_parts)
        if isinstance(result.get("result"), str):
            return result["result"]
        if isinstance(result.get("output"), str):
            return result["output"]
        if isinstance(result.get("resolution"), str):
            return result["resolution"]
    return _safe_json_dumps(result)


def _extract_sidecar_events_from_tool_result(result: Any) -> list[dict]:
    """
    Best-effort extraction of sidecar human_input_* events from ask_human MCP
    tool results returned by Antigravity.
    """
    if isinstance(result, dict):
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            events = structured.get("events")
            if isinstance(events, list):
                return [ev for ev in events if isinstance(ev, dict)]
        events = result.get("events")
        if isinstance(events, list):
            return [ev for ev in events if isinstance(ev, dict)]
    return []


def extract_trajectory_steps(
    events: list[dict],
    thought_texts: list[str],
    *,
    stop_reason: str = "",
    sdk_error_msg: str = "",
) -> list[dict]:
    steps: list[dict] = []
    pending_by_id: dict[str, dict] = {}
    pending_fifo: list[dict] = []

    for event in events:
        et = str(event.get("type", ""))
        if et == "antigravity_native_ask":
            q = str(event.get("question", ""))
            a = str(event.get("answer", ""))
            steps.append({"thought": "", "act": _cap(f"ask_human [native] {q}", ACT_CAP), "obs": _cap(a, OBS_CAP)})
            continue
        if et == "ask_question_full_info_mode":
            q = str(event.get("question", ""))
            steps.append({"thought": "", "act": _cap(f"ask_human [native] {q}", ACT_CAP), "obs": UNKNOWN_RESOLUTION})
            continue
        if et == "antigravity_tool_call":
            tool_name = str(event.get("tool_name", ""))
            args = event.get("args") if isinstance(event.get("args"), dict) else {}
            if tool_name == "ask_human" or tool_name.endswith(".ask_human"):
                q = _read_ask_question_from_value(args)
                act = _cap(f"ask_human [custom_tool] {q}", ACT_CAP)
            elif tool_name == "run_command":
                act = _cap(str(args.get("command", "")), ACT_CAP)
            elif tool_name in {"edit_file", "create_file", "view_file"}:
                act = _cap(f"{tool_name} {args.get('file_path', '')}".strip(), ACT_CAP)
            else:
                act = _cap(f"{tool_name}: {_safe_json_dumps(args)}", ACT_CAP)
            pending = {
                "call_id": str(event.get("call_id", "")),
                "act": act,
                "tool_name": tool_name,
                "thought": "",
            }
            if pending["call_id"]:
                pending_by_id[pending["call_id"]] = pending
            pending_fifo.append(pending)
            continue
        if et == "antigravity_tool_result":
            call_id = str(event.get("call_id", ""))
            pending = None
            if call_id and call_id in pending_by_id:
                pending = pending_by_id.pop(call_id)
                if pending in pending_fifo:
                    pending_fifo.remove(pending)
            elif pending_fifo:
                pending = pending_fifo.pop(0)
                if pending.get("call_id"):
                    pending_by_id.pop(str(pending["call_id"]), None)
            obs = _cap(_render_tool_obs(event), OBS_CAP)
            if pending:
                act = str(pending.get("act", ""))
                if act.startswith("ask_human"):
                    if not obs or obs.startswith("[error]") or obs.startswith("[no observation"):
                        obs = CANT_ANSWER
                steps.append({"thought": "", "act": act, "obs": obs})
            else:
                steps.append({"thought": "", "act": "", "obs": obs})
            continue

    for pending in pending_fifo:
        act = str(pending.get("act", ""))
        obs = "[no observation — tool call was interrupted]"
        if stop_reason:
            obs += f" (stop_reason={stop_reason})"
        if sdk_error_msg:
            first_line = str(sdk_error_msg).strip().splitlines()[0]
            if first_line:
                obs += f" ({_cap(first_line, 300)})"
        if act.startswith("ask_human"):
            obs = CANT_ANSWER
        steps.append({"thought": "", "act": act, "obs": obs})

    filtered_thoughts = [_cap(t, THOUGHT_CAP) for t in thought_texts if str(t or "").strip()]
    for i, thought in enumerate(filtered_thoughts):
        if i < len(steps):
            existing = str(steps[i].get("thought", "") or "")
            steps[i]["thought"] = thought if not existing else f"{existing}\n\n{thought}"
        else:
            steps.append({"thought": thought, "act": "", "obs": ""})

    return _normalize_trajectory_steps(steps)


def compute_stats(all_events: list[dict], trajectory_steps: list[dict], num_blockers_total: int) -> dict:
    num_questions = 0
    num_questions_approval = 0
    num_questions_full_info = 0
    resolved_blocker_ids: set[str] = set()
    request_status_by_id: dict[str, str] = {}

    for ev in all_events:
        if ev.get("type", "") != "human_input_result":
            continue
        rid = str(ev.get("request_id", "") or "")
        if not rid:
            continue
        status = str(ev.get("result", {}).get("status", "unknown") or "unknown").lower()
        request_status_by_id[rid] = status

    for ev in all_events:
        ev_type = ev.get("type", "")
        if ev_type == "human_input_raw_event":
            rid = str(ev.get("request_id", "") or "")
            if rid and request_status_by_id.get(rid) == "error":
                continue
            rt = ev.get("request_type", "")
            if rt in ASK_HUMAN_REQUEST_TYPES:
                num_questions += 1
            elif rt in ("approval", "permission"):
                num_questions_approval += 1
        elif ev_type == "ask_question_full_info_mode":
            num_questions_full_info += 1
        elif ev_type == "human_input_result":
            bid = ev.get("result", {}).get("blocker_id")
            if (
                bid
                and bid != UNKNOWN_BLOCKER_ID
                and ev.get("result", {}).get("status") == "answered"
            ):
                resolved_blocker_ids.add(str(bid))

    return {
        "num_steps": len(trajectory_steps),
        "num_questions": num_questions,
        "num_questions_approval": num_questions_approval,
        "num_total_questions": num_questions + num_questions_approval,
        "num_questions_full_info": num_questions_full_info,
        "num_blockers_resolved": len(resolved_blocker_ids),
        "num_blockers_total": num_blockers_total,
        "stats_schema_version": 2,
    }


def _usage_totals_from_history(history: list[Any]) -> dict[str, Optional[int]]:
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    usage_records = 0
    for step in history:
        usage = getattr(step, "usage_metadata", None)
        if not usage:
            continue
        usage_records += 1
        input_tokens += int(getattr(usage, "prompt_token_count", 0) or 0)
        output_tokens += int(getattr(usage, "candidates_token_count", 0) or 0)
        total_tokens += int(getattr(usage, "total_token_count", 0) or 0)
    if not usage_records:
        return {
            "num_llm_calls": None,
            "input_tokens": None,
            "output_tokens": None,
            "total_tokens": None,
        }
    return {
        "num_llm_calls": usage_records,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": total_tokens,
    }


def _history_sdk_error(history: list[Any]) -> Optional[str]:
    for step in history:
        error_text = str(getattr(step, "error", "") or "").strip()
        if error_text:
            return error_text
        content_text = str(getattr(step, "content", "") or "").strip()
        if "System step error" in content_text:
            return content_text
    return None


def _history_has_token_limit(history: list[Any]) -> bool:
    for step in history:
        payload = {
            "error": getattr(step, "error", ""),
            "http_code": getattr(step, "http_code", 0),
            "content": getattr(step, "content", ""),
        }
        if _is_token_limit_structured(payload):
            return True
        if _is_token_limit_error(payload.get("error")) or _is_token_limit_error(payload.get("content")):
            return True
    return False


async def _git_diff(workspace: str = WORKSPACE) -> str:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--binary", "HEAD",
            cwd=workspace,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return stdout.decode("utf-8", errors="replace")
    except Exception:
        return ""


async def main() -> None:
    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
    run_started_at = time.monotonic()

    metadata = json.loads(Path(TASK_DIR, "metadata.json").read_text())
    problem_stmt = Path(TASK_DIR, "problem_statement.txt").read_text()
    uid: str = str(metadata.get("uid") or metadata.get("instance_id") or "unknown")

    blockers: list[dict] = []
    num_blockers_total = 0
    registry_path = Path(TASK_DIR, "blocker_registry.json")
    if registry_path.exists():
        registry = json.loads(registry_path.read_text())
        entries = registry.get("entries") or registry.get("blockers") or []
        if isinstance(registry, list):
            entries = registry
        num_blockers_total = len(entries)
        if FULL_INFO_ENABLED:
            blockers = [
                {"description": e.get("description", ""), "resolution": e.get("resolution", "")}
                for e in entries
            ]

    prompt = build_swe_prompt(problem_stmt, MODE, blockers)
    instruction = _build_instruction(MODE)
    _remove_workspace_ask_human_skill_dirs(WORKSPACE)

    with_custom_tool = WITH_CUSTOM_TOOL if ASK_HUMAN_ENABLED else False

    _write_json(Path(OUTPUT_DIR, "attempt.json"), {
        "run_id": RUN_ID,
        "uid": uid,
        "mode": MODE,
        "pass_index": PASS_INDEX,
        "harness": "antigravity",
        "model": ANTIGRAVITY_MODEL,
        "max_steps": MAX_STEPS if MAX_STEPS > 0 else None,
        "timeout_ms": TIMEOUT_MS,
        "workspace": WORKSPACE,
        "task_dir": TASK_DIR,
        "output_dir": OUTPUT_DIR,
        "started_at": _now_iso(),
        "prompt": prompt,
        "ask_human_tool_enabled": ASK_HUMAN_ENABLED,
        "with_custom_tool": with_custom_tool,
        "skill_enabled": SKILL_ENABLED,
        "guidance_enabled": ASK_HUMAN_GUIDANCE_ENABLED,
        "with_skill": SKILL_TEMPLATE_VERSION or "",
        "with_ask_guidance": ASK_HUMAN_GUIDANCE_TEMPLATE_VERSION or "",
        "reasoning_effort_configured": ANTIGRAVITY_REASONING_EFFORT or None,
    })

    all_events: list[dict] = []
    history_steps: list[Any] = []
    sdk_error_msg: Optional[str] = None
    stop_reason = "complete"
    timed_out = False

    if not LITELLM_API_KEY:
        sdk_error_msg = "Missing API key: set LITELLM_API_KEY or LITELLM_PROXY_API_KEY or ANTHROPIC_AUTH_TOKEN"
        stop_reason = "sdk_error"
    elif not LITELLM_BASE_URL:
        sdk_error_msg = "Missing LITELLM_BASE_URL for Antigravity LiteLLM shim routing"
        stop_reason = "sdk_error"

    _run_wall_start = time.monotonic()
    sidecar_proc: Optional[subprocess.Popen] = None
    sidecar_url = ""
    sidecar_event_start_index = 0
    litellm_proxy_server: Optional[ThreadingHTTPServer] = None
    litellm_base_for_agent = _litellm_base_url(LITELLM_BASE_URL)

    if sdk_error_msg is None:
        try:
            litellm_proxy_server, litellm_base_for_agent = _start_litellm_auth_proxy(
                litellm_base_for_agent, LITELLM_API_KEY
            )
        except Exception as exc:
            sdk_error_msg = f"Failed to start LiteLLM auth proxy: {exc}"
            stop_reason = "litellm_proxy_start_failed"

    if sdk_error_msg is None and ASK_HUMAN_ENABLED:
        try:
            sidecar_proc, sidecar_url = _start_sidecar(uid)
        except Exception as exc:
            sdk_error_msg = f"Failed to start ask_human sidecar: {exc}"
            stop_reason = "sidecar_start_failed"

    try:
        if sdk_error_msg is None:
            max_steps_reached = False
            for attempt_idx in range(1, STEP_LITELLM_TRIES + 1):
                all_events.clear()
                history_steps = []
                sdk_error_msg = None
                timed_out = False
                stop_reason = "complete"
                max_steps_reached = False
                tools_seen = 0
                event_seq = 0
                sidecar_event_start_index = 0

                if sidecar_url:
                    # Keep sidecar /events scoped to this retry attempt only.
                    try:
                        _sidecar_events_reset_sync(sidecar_url)
                    except Exception:
                        try:
                            sidecar_event_start_index = len(_sidecar_events_sync(sidecar_url))
                        except Exception:
                            sidecar_event_start_index = 0

                def _push_event(event: dict) -> None:
                    nonlocal event_seq
                    event_seq += 1
                    payload = dict(event)
                    payload.setdefault("timestamp", _now_iso())
                    payload["_seq"] = event_seq
                    all_events.append(payload)

                class _TrackToolCallHook(hooks.PreToolCallDecideHook):
                    async def run(self, context: hooks.HookContext, data: Any):
                        nonlocal tools_seen, max_steps_reached
                        tool_name = str(getattr(data, "name", ""))
                        tool_args = getattr(data, "args", {}) or {}
                        call_id = str(getattr(data, "id", "") or "")
                        if MAX_STEPS > 0 and tools_seen >= MAX_STEPS:
                            if not max_steps_reached:
                                max_steps_reached = True
                                _push_event({
                                    "type": "max_steps_reached",
                                    "max_steps": MAX_STEPS,
                                    "items_done": tools_seen,
                                })
                            return hooks.HookResult(allow=False, message="max steps reached")
                        tools_seen += 1
                        _push_event({
                            "type": "antigravity_tool_call",
                            "tool_name": tool_name,
                            "args": tool_args,
                            "call_id": call_id,
                        })
                        return hooks.HookResult(allow=True)

                class _TrackToolResultHook(hooks.PostToolCallHook):
                    async def run(self, context: hooks.HookContext, data: Any):
                        tool_name = str(getattr(data, "name", ""))
                        result_payload = getattr(data, "result", None)
                        _push_event({
                            "type": "antigravity_tool_result",
                            "tool_name": tool_name,
                            "call_id": str(getattr(data, "id", "") or ""),
                            "result": result_payload,
                            "error": getattr(data, "error", None),
                        })
                        if tool_name == "ask_human" or tool_name.endswith(".ask_human"):
                            for ev in _extract_sidecar_events_from_tool_result(result_payload):
                                _push_event(ev)

                class _AskRouterHook(hooks.OnInteractionHook):
                    async def run(self, context: hooks.HookContext, data: Any):
                        responses: list[QuestionResponse] = []
                        questions = list(getattr(data, "questions", []) or [])
                        for q in questions:
                            prompt_text = str(getattr(q, "question", "") or "")
                            if ASK_HUMAN_ENABLED:
                                payload = {
                                    "question": prompt_text,
                                    "options": [
                                        {"label": str(getattr(opt, "text", ""))}
                                        for opt in (getattr(q, "options", []) or [])
                                    ],
                                    "context": {},
                                    "request_type": "clarification",
                                    "native_event_type": "antigravity.ask_question",
                                    "raw_event": {"question": prompt_text},
                                }
                                try:
                                    result = await asyncio.get_event_loop().run_in_executor(
                                        None, _sidecar_ask_sync, sidecar_url, payload
                                    )
                                    for ev in result.get("events", []):
                                        _push_event(ev)
                                    answer = str(result.get("resolution", UNKNOWN_RESOLUTION))
                                except Exception:
                                    answer = CANT_ANSWER
                            else:
                                answer = UNKNOWN_RESOLUTION
                                _push_event({
                                    "type": "ask_question_full_info_mode",
                                    "question": prompt_text,
                                })
                            _push_event({
                                "type": "antigravity_native_ask",
                                "question": prompt_text,
                                "answer": answer,
                            })
                            responses.append(QuestionResponse(freeform_response=answer))
                        return QuestionHookResult(responses=responses, cancelled=False)

                skills_paths: list[str] = []
                if SKILL_ENABLED:
                    skill_dir = _install_workspace_skill_for_discovery(WORKSPACE, SKILL_TOOL_REF)
                    skills_paths.append(str(skill_dir))

                mcp_servers = []
                if ASK_HUMAN_ENABLED and with_custom_tool:
                    mcp_servers.append(_custom_mcp_stdio_server(sidecar_url))

                thinking_level = _antigravity_thinking_level(ANTIGRAVITY_REASONING_EFFORT)
                model_entry = ModelEntry(
                    name=ANTIGRAVITY_MODEL,
                    api_key=LITELLM_API_KEY,
                    generation=GenerationConfig(thinking_level=thinking_level),
                )
                cfg = TrustHorizonLocalAgentConfig(
                    litellm_base_url=litellm_base_for_agent,
                    system_instructions=instruction,
                    capabilities=CapabilitiesConfig(enable_subagents=False),
                    policies=[policy.allow_all()],
                    workspaces=[WORKSPACE],
                    hooks=[_AskRouterHook(), _TrackToolCallHook(), _TrackToolResultHook()],
                    mcp_servers=mcp_servers,
                    skills_paths=skills_paths,
                    gemini_config=GeminiConfig(
                        api_key=LITELLM_API_KEY,
                        models=ModelConfig(default=model_entry),
                    ),
                )

                async def _run_agent_once() -> list[Any]:
                    async with Agent(cfg) as agent:
                        response = await agent.chat(prompt)
                        await response.text()
                        return list(agent.conversation.history)

                elapsed_ms = (time.monotonic() - _run_wall_start) * 1000
                remaining_ms = max(0.0, TIMEOUT_MS - elapsed_ms)
                attempt_timeout = min(remaining_ms / 1000, LITELLM_CALL_TIMEOUT_S)
                if attempt_timeout <= 0:
                    timed_out = True
                    stop_reason = "timeout"
                else:
                    try:
                        history_steps = await asyncio.wait_for(_run_agent_once(), timeout=attempt_timeout)
                        history_error = _history_sdk_error(history_steps)
                        if history_error:
                            if _history_has_token_limit(history_steps) or _is_token_limit_error(history_error):
                                sdk_error_msg = None
                                stop_reason = "token_limit"
                            else:
                                sdk_error_msg = history_error
                                stop_reason = "sdk_error"
                        if max_steps_reached:
                            stop_reason = "max_steps"
                    except asyncio.TimeoutError:
                        timed_out = True
                        stop_reason = "timeout"
                        sdk_error_msg = f"Timed out after {int(attempt_timeout * 1000)}ms"
                    except Exception as exc:
                        err_text = str(exc)
                        if _is_token_limit_exception(exc):
                            sdk_error_msg = None
                            stop_reason = "token_limit"
                        else:
                            sdk_error_msg = err_text
                            stop_reason = "sdk_error"

                if stop_reason in {"sdk_error", "timeout"} and attempt_idx < STEP_LITELLM_TRIES:
                    print(
                        f"[{uid[:12]}|{MODE}|p{PASS_INDEX}] {stop_reason} on attempt "
                        f"{attempt_idx}/{STEP_LITELLM_TRIES}, retrying: {sdk_error_msg}",
                        file=sys.stderr,
                    )
                    continue
                break
    finally:
        if sidecar_url:
            try:
                sidecar_events = _sidecar_events_sync(sidecar_url)
                _merge_sidecar_events(
                    all_events,
                    _slice_sidecar_events(sidecar_events, sidecar_event_start_index),
                )
            except Exception:
                pass
        _stop_sidecar(sidecar_proc)
        _stop_litellm_auth_proxy(litellm_proxy_server)

    patch_content = await _git_diff(WORKSPACE)
    patch_path = Path(OUTPUT_DIR, "patch.diff")
    patch_path.write_text(patch_content, encoding="utf-8")

    thought_texts = []
    for step in history_steps:
        if bool(getattr(step, "is_complete_response", False)) and str(getattr(step, "content", "") or ""):
            thought_texts.append(str(getattr(step, "content", "")))

    trajectory_steps = extract_trajectory_steps(
        all_events,
        thought_texts,
        stop_reason=stop_reason,
        sdk_error_msg=str(sdk_error_msg or ""),
    )
    _write_json(
        Path(OUTPUT_DIR, "events.json"),
        json.loads(_safe_json_dumps(all_events)),
    )
    stats = {
        **compute_stats(all_events, trajectory_steps, num_blockers_total),
        **_usage_totals_from_history(history_steps),
        "wall_clock_ms": int(max(0.0, (time.monotonic() - run_started_at) * 1000)),
        "num_tool_calls": sum(1 for s in trajectory_steps if str(s.get("act", "")).strip()),
        "num_turns_or_items": len(history_steps),
    }
    _write_json(Path(OUTPUT_DIR, "trajectory.json"), trajectory_steps)
    _write_json(Path(OUTPUT_DIR, "stats.json"), stats)

    _write_json(Path(OUTPUT_DIR, "result.json"), {
        "patch_bytes": len(patch_content.encode("utf-8")),
        "num_steps": stats["num_steps"],
        "completed_at": _now_iso(),
        "sdk_error": sdk_error_msg,
        "timeout": timed_out,
        "stop_reason": stop_reason,
    })

    label = f"[{uid[:12]}|{MODE}|p{PASS_INDEX}]"
    if sdk_error_msg:
        print(f"{label} Antigravity error: {sdk_error_msg}", file=sys.stderr)
        sys.exit(1)
    if timed_out:
        print(f"{label} timed out (max {TIMEOUT_MS // 1000}s)", file=sys.stderr)
        sys.exit(1)
    print(
        f"{label} done  steps={stats['num_steps']}  "
        f"questions={stats['num_questions']}  "
        f"questions_full_info={stats['num_questions_full_info']}  "
        f"patch_bytes={len(patch_content.encode('utf-8'))}"
    )


if __name__ == "__main__":
    asyncio.run(main())
