#!/usr/bin/env python3
"""Minimal MCP server for reading selected macOS app context.

This is a local-only stdio MCP server. It exposes a small tool surface that reads
terminal context through macOS automation APIs after the user has granted the
launcher process permission in System Settings.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Server metadata
# ---------------------------------------------------------------------------

SERVER_NAME = "macos-work-with-apps"
SERVER_VERSION = "0.5.0"

# ---------------------------------------------------------------------------
# Security patterns
# ---------------------------------------------------------------------------

SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{20,})"),
    re.compile(r"(ghp_[A-Za-z0-9_]{20,})"),
    re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"((?:AKIA|ASIA)[A-Z0-9]{16})"),
    re.compile(r"(?i)\b(password|passwd|token|api[_-]?key|secret)\s*=\s*([^\s]+)"),
]

DANGEROUS_COMMAND_PATTERNS = [
    re.compile(r"(^|[;&|]\s*)sudo\b"),
    re.compile(r"\brm\s+.*-[^\n]*r[^\n]*f"),
    re.compile(r"\brm\s+.*-[^\n]*f[^\n]*r"),
    re.compile(r"\bchmod\s+.*-R\s+777\b"),
    re.compile(r"\bchown\s+.*-R\b"),
    re.compile(r"\bdd\s+.*\bof=/dev/"),
    re.compile(r"\bmkfs\b"),
    re.compile(r"\bdiskutil\s+(erase|partition|apfs\s+delete)", re.IGNORECASE),
    re.compile(r":\s*\(\s*\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;?\s*:"),
]

# ---------------------------------------------------------------------------
# Special keys that send_terminal_input supports via their AppleScript key code.
# These bypass the normal keystroke path and send a raw key event.
# ---------------------------------------------------------------------------

_SPECIAL_KEYS: dict[str, str] = {
    "return":  "key code 36",
    "enter":   "key code 36",
    "escape":  "key code 53",
    "esc":     "key code 53",
    "tab":     "key code 48",
    "up":      "key code 126",
    "down":    "key code 125",
    "left":    "key code 123",
    "right":   "key code 124",
    "ctrl+c":  "key code 8 using control down",
    "ctrl+d":  "key code 2 using control down",
    "ctrl+z":  "key code 6 using control down",
    "ctrl+l":  "key code 37 using control down",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _escape_for_applescript(s: str) -> str:
    """Escape a string for safe embedding inside an AppleScript double-quoted literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def redact(text: str) -> str:
    """Redact common secret / token patterns from *text*."""
    redacted = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(r"\1=[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def run_osascript(source: str, *, language: str | None = None) -> str:
    """Execute an AppleScript (or JXA) snippet and return its stdout."""
    command = ["osascript"]
    if language:
        command.extend(["-l", language])
    command.extend(["-e", source])
    proc = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
        timeout=5,
    )
    if proc.returncode != 0:
        message = proc.stderr.strip() or proc.stdout.strip() or "osascript failed"
        raise RuntimeError(message)
    return proc.stdout


# ---------------------------------------------------------------------------
# App readers
# ---------------------------------------------------------------------------


def terminal_context() -> str:
    return run_osascript(
        """
        const app = Application("/System/Applications/Utilities/Terminal.app");
        if (!app.running() || app.windows.length === 0) "";
        else app.windows[0].selectedTab.history();
        """,
        language="JavaScript",
    )


def iterm_context() -> str:
    return run_osascript(
        """
        tell application "iTerm2"
          if not (exists current window) then return ""
          return contents of current session of current window
        end tell
        """
    )


SUPPORTED_APPS = {
    "terminal": {
        "display_name": "Terminal",
        "bundle_id": "com.apple.Terminal",
        "reader": terminal_context,
    },
    "iterm2": {
        "display_name": "iTerm2",
        "bundle_id": "com.googlecode.iterm2",
        "reader": iterm_context,
    },
}

# ---------------------------------------------------------------------------
# Terminal command execution
# ---------------------------------------------------------------------------


def validate_terminal_command(command: str) -> str:
    """Validate a shell command string for safety before execution."""
    command = command.strip()
    if not command:
        raise ValueError("Command cannot be empty.")
    if "\n" in command or "\r" in command:
        raise ValueError("Only single-line commands are allowed.")
    if len(command) > 2000:
        raise ValueError("Command is too long; limit is 2000 characters.")
    for pattern in DANGEROUS_COMMAND_PATTERNS:
        if pattern.search(command):
            raise ValueError(f"Refusing potentially dangerous command: {command}")
    return command


def run_terminal_command(command: str, app: str = "terminal") -> str:
    """Send command to the front terminal tab.

    Supports both Terminal.app (`do script`) and iTerm2 (`write text`).
    No Accessibility permission is required for either approach.
    The command appears verbatim in the terminal window and executes immediately.
    """
    command = validate_terminal_command(command)
    escaped = _escape_for_applescript(command)
    if app == "iterm2":
        script = f"""
            tell application "iTerm2"
                if not (exists current window) then
                    create window with default profile command "{escaped}"
                else
                    tell current session of current window
                        write text "{escaped}"
                    end tell
                end if
            end tell
            return "sent"
        """
    else:
        script = f"""
            tell application "Terminal"
                if not (exists window 1) then
                    do script "{escaped}"
                else
                    do script "{escaped}" in (selected tab of front window)
                end if
            end tell
            return "sent"
        """
    display = SUPPORTED_APPS.get(app, {}).get("display_name", app)
    run_osascript(script)
    return f"Sent to {display}: {command}"


def send_terminal_input(
    text: str,
    press_return: bool = True,
    sensitive: bool = False,
    app: str = "terminal",
) -> str:
    """Send raw stdin input to whatever is currently running in a terminal app.

    Unlike run_terminal_command (which uses `do script` / `write text` to start
    a *new* shell command), this function uses System Events keystroke to type
    directly into the foreground process.  It works for interactive prompts
    (Y/n, passwords, pager navigation, vim, fzf, etc.) as well as special
    control sequences.  Supports both Terminal.app and iTerm2.

    Requires the calling process to have Accessibility permission in
    System Settings -> Privacy & Security -> Accessibility.

    Args:
        text:         Text to type, or a special key name (see _SPECIAL_KEYS).
        press_return: Press Return after typing ordinary text (default True).
        sensitive:    When True the response message redacts the content so
                      passwords / tokens are not echoed into conversation logs.
        app:          Target app key ("terminal" or "iterm2").

    Special keys (case-insensitive):
        return / enter, escape / esc, tab,
        up, down, left, right,
        ctrl+c, ctrl+d, ctrl+z, ctrl+l
    """
    # Strip only leading/trailing newlines — preserve intentional spaces so
    # TUI/REPL inputs like " " (space-bar in pagers) work correctly.
    raw = text.strip("\n\r")
    if not raw:
        raise ValueError("Input text cannot be empty.")
    if len(raw) > 500:
        raise ValueError("Input too long; limit is 500 characters.")

    display = SUPPORTED_APPS.get(app, {}).get("display_name", app)
    activate_app = "iTerm2" if app == "iterm2" else "Terminal"

    lower = raw.lower()
    if lower in _SPECIAL_KEYS:
        # Send a raw key event — no keystroke string needed.
        key_action = _SPECIAL_KEYS[lower]
        script = f"""
            tell application "{activate_app}" to activate
            tell application "System Events"
                {key_action}
            end tell
        """
        run_osascript(script)
        return f"Sent special key to {display}: {raw}"

    # Ordinary text: escape for AppleScript, then optionally press Return.
    if "\n" in raw or "\r" in raw:
        raise ValueError("Embedded newlines not allowed; use press_return=true for Enter.")
    escaped = _escape_for_applescript(raw)
    return_action = "key code 36" if press_return else ""
    script = f"""
        tell application "{activate_app}" to activate
        tell application "System Events"
            keystroke "{escaped}"
            {return_action}
        end tell
    """
    run_osascript(script)
    suffix = " + Return" if press_return else ""
    if sensitive:
        return f"Sent sensitive input to {display}{suffix}"  # content intentionally redacted
    return f"Sent input to {display}: {raw}{suffix}"


# ---------------------------------------------------------------------------
# MCP tool definitions (JSON Schema)
# ---------------------------------------------------------------------------

_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_supported_apps",
        "description": "List macOS apps this MCP server can read context from.",
        "inputSchema": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "get_app_context",
        "description": "Read recent context from an allowed macOS app such as Terminal or iTerm2.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "app": {
                    "type": "string",
                    "enum": sorted(SUPPORTED_APPS.keys()),
                    "description": "App key to read.",
                },
                "max_chars": {
                    "type": "integer",
                    "minimum": 200,
                    "maximum": 50000,
                    "default": 12000,
                    "description": "Maximum trailing characters to return.",
                },
                "redact_secrets": {
                    "type": "boolean",
                    "default": True,
                    "description": "Redact common token and secret patterns before returning text.",
                },
            },
            "required": ["app"],
            "additionalProperties": False,
        },
    },
    {
        "name": "run_terminal_command",
        "description": (
            "Run a new single-line shell command in the front Terminal.app or iTerm2 tab. "
            "Uses AppleScript `do script` (Terminal) or `write text` (iTerm2). "
            "Use this ONLY when the shell prompt is idle (not inside an interactive program). "
            "To answer an interactive prompt (Y/n, password, vim, pager, etc.) use "
            "`send_terminal_input` instead."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Single-line shell command to run visibly in the terminal.",
                },
                "app": {
                    "type": "string",
                    "enum": ["terminal", "iterm2"],
                    "default": "terminal",
                    "description": "Target terminal app. Defaults to Terminal.app.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_terminal_input",
        "description": (
            "Type text (or a special key) into whatever program is currently running "
            "in the front Terminal.app or iTerm2 tab, via System Events keystroke. "
            "Use this to answer interactive prompts (Y/n, passwords, confirmations), "
            "navigate TUI apps (vim :q, pager q, fzf arrow keys), or send control "
            "sequences (ctrl+c, ctrl+d, ctrl+z). "
            "Supported special keys: return, escape, tab, up, down, left, right, "
            "ctrl+c, ctrl+d, ctrl+z, ctrl+l."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": (
                        "Text to type, or a special key name "
                        "(return, escape, tab, up, down, left, right, "
                        "ctrl+c, ctrl+d, ctrl+z, ctrl+l)."
                    ),
                },
                "press_return": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Press Return after typing the text. "
                        "Set to false for partial input or password fields "
                        "that confirm with a different key."
                    ),
                },
                "sensitive": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When true, the response will NOT echo the typed text back "
                        "(use for passwords, tokens, or any secret input). "
                        "The input is still sent to the terminal; only the MCP response is redacted."
                    ),
                },
                "app": {
                    "type": "string",
                    "enum": ["terminal", "iterm2"],
                    "default": "terminal",
                    "description": "Target terminal app. Defaults to Terminal.app.",
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
]

# ---------------------------------------------------------------------------
# Tool handlers (dispatch-table pattern)
# ---------------------------------------------------------------------------


def _make_text_content(text: str) -> dict[str, Any]:
    """Build a standard MCP text content response."""
    return {"content": [{"type": "text", "text": text}]}


def _handle_list_apps(_arguments: dict[str, Any]) -> dict[str, Any]:
    return _make_text_content(
        json.dumps(
            [
                {"key": key, "display_name": v["display_name"], "bundle_id": v["bundle_id"]}
                for key, v in SUPPORTED_APPS.items()
            ],
            ensure_ascii=False,
            indent=2,
        )
    )


def _handle_run_command(arguments: dict[str, Any]) -> dict[str, Any]:
    return _make_text_content(
        run_terminal_command(arguments.get("command", ""), app=arguments.get("app", "terminal"))
    )


def _handle_send_input(arguments: dict[str, Any]) -> dict[str, Any]:
    result_msg = send_terminal_input(
        arguments.get("text", ""),
        press_return=bool(arguments.get("press_return", True)),
        sensitive=bool(arguments.get("sensitive", False)),
        app=arguments.get("app", "terminal"),
    )
    return _make_text_content(result_msg)


def _handle_get_context(arguments: dict[str, Any]) -> dict[str, Any]:
    app = arguments.get("app")
    if app not in SUPPORTED_APPS:
        raise ValueError(f"Unsupported app: {app}")

    max_chars = min(max(int(arguments.get("max_chars", 12000)), 200), 50000)
    should_redact = bool(arguments.get("redact_secrets", True))

    text = SUPPORTED_APPS[app]["reader"]()
    text = text[-max_chars:]
    if should_redact:
        text = redact(text)

    if not text.strip():
        text = f"No readable context returned from {SUPPORTED_APPS[app]['display_name']}."

    return _make_text_content(text)


_TOOL_HANDLERS: dict[str, Callable[..., dict[str, Any]]] = {
    "list_supported_apps": _handle_list_apps,
    "get_app_context": _handle_get_context,
    "run_terminal_command": _handle_run_command,
    "send_terminal_input": _handle_send_input,
}


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Dispatch a tool call by name."""
    handler = _TOOL_HANDLERS.get(name)
    if not handler:
        raise ValueError(f"Unknown tool: {name}")
    return handler(arguments)


# ---------------------------------------------------------------------------
# JSON-RPC helpers
# ---------------------------------------------------------------------------


def make_error(code: int, message: str, request_id: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def make_result(result: Any, request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


# ---------------------------------------------------------------------------
# Method handlers (dispatch-table pattern)
# ---------------------------------------------------------------------------


def _method_initialize(params: dict[str, Any], _request_id: Any) -> dict[str, Any]:
    return {
        "protocolVersion": params.get("protocolVersion", "2024-11-05"),
        "capabilities": {"tools": {}},
        "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
    }


def _method_tools_list(_params: dict[str, Any], _request_id: Any) -> dict[str, Any]:
    return {"tools": _TOOL_DEFINITIONS}


def _method_tools_call(params: dict[str, Any], _request_id: Any) -> dict[str, Any]:
    return call_tool(params.get("name", ""), params.get("arguments") or {})


_METHOD_HANDLERS: dict[str, Callable[..., dict[str, Any] | None]] = {
    "initialize": _method_initialize,
    "notifications/initialized": lambda _p, _r: None,
    "tools/list": _method_tools_list,
    "tools/call": _method_tools_call,
}


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    try:
        handler = _METHOD_HANDLERS.get(method)  # type: ignore[arg-type]
        if handler is None:
            return make_error(-32601, f"Method not found: {method}", request_id)
        result = handler(params, request_id)
        if result is None:
            return None
        return make_result(result, request_id)
    except Exception as exc:  # noqa: BLE001 - MCP should report tool errors to client.
        return make_error(-32000, str(exc), request_id)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = make_error(-32700, f"Parse error: {exc}")
        else:
            response = handle_request(request)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
