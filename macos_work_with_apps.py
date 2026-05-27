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
from typing import Any


SERVER_NAME = "macos-work-with-apps"
SERVER_VERSION = "0.4.0"

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


def redact(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(r"\1=[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def run_osascript(source: str, *, language: str | None = None) -> str:
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


def terminal_context() -> str:
    return run_osascript(
        """
        const app = Application("/System/Applications/Utilities/Terminal.app");
        if (!app.running() || app.windows.length === 0) "";
        else app.windows[0].selectedTab.history();
        """,
        language="JavaScript",
    )


def validate_terminal_command(command: str) -> str:
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


def run_terminal_command(command: str) -> str:
    """Send command to the front Terminal.app tab using `do script`.

    Uses Terminal.app's native AppleScript dictionary instead of
    System Events keystrokes, so no Accessibility permission is required.
    The command appears verbatim in the Terminal window and executes immediately.
    """
    command = validate_terminal_command(command)
    # Escape backslashes and double-quotes so the string is safe inside
    # the AppleScript double-quoted literal we're about to build.
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
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
    run_osascript(script)
    return f"Sent to Terminal: {command}"


# ---------------------------------------------------------------------------
# Special keys that send_terminal_input supports via their AppleScript key code.
# These bypass the normal keystroke path and send a raw key event.
# ---------------------------------------------------------------------------
_SPECIAL_KEYS: dict[str, str] = {
    "return":    "key code 36",
    "enter":     "key code 36",
    "escape":    "key code 53",
    "esc":       "key code 53",
    "tab":       "key code 48",
    "up":        "key code 126",
    "down":      "key code 125",
    "left":      "key code 123",
    "right":     "key code 124",
    "ctrl+c":    "key code 8 using control down",
    "ctrl+d":    "key code 2 using control down",
    "ctrl+z":    "key code 6 using control down",
    "ctrl+l":    "key code 37 using control down",
}


def send_terminal_input(text: str, press_return: bool = True) -> str:
    """Send raw stdin input to whatever is currently running in Terminal.app.

    Unlike run_terminal_command (which uses `do script` to start a *new* shell
    command), this function uses System Events keystroke to type directly into
    the foreground process.  It works for interactive prompts (Y/n, passwords,
    pager navigation, vim, fzf, etc.) as well as special control sequences.

    Requires the calling process to have Accessibility permission in
    System Settings → Privacy & Security → Accessibility.

    Special keys (case-insensitive):
        return / enter, escape / esc, tab,
        up, down, left, right,
        ctrl+c, ctrl+d, ctrl+z, ctrl+l
    """
    raw = text.strip()
    if not raw:
        raise ValueError("Input text cannot be empty.")
    if len(raw) > 500:
        raise ValueError("Input too long; limit is 500 characters.")

    lower = raw.lower()
    if lower in _SPECIAL_KEYS:
        # Send a raw key event — no keystroke string needed.
        key_action = _SPECIAL_KEYS[lower]
        script = f"""
            tell application "Terminal" to activate
            tell application "System Events"
                {key_action}
            end tell
        """
        run_osascript(script)
        return f"Sent special key to Terminal: {raw}"

    # Ordinary text: escape backslashes and double-quotes for the AppleScript
    # string literal, then optionally press Return afterwards.
    if "\n" in raw or "\r" in raw:
        raise ValueError("Embedded newlines not allowed; use press_return=true for Enter.")
    escaped = raw.replace("\\", "\\\\").replace('"', '\\"')
    return_action = "key code 36" if press_return else ""
    script = f"""
        tell application "Terminal" to activate
        tell application "System Events"
            keystroke "{escaped}"
            {return_action}
        end tell
    """
    run_osascript(script)
    suffix = " + Return" if press_return else ""
    return f"Sent input to Terminal: {raw}{suffix}"


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


def make_error(code: int, message: str, request_id: Any = None) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def make_result(result: Any, request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def tool_list() -> list[dict[str, Any]]:
    return [
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
                "Run a new single-line shell command in the front Terminal.app tab "
                "using AppleScript `do script`. "
                "Use this ONLY when the shell prompt is idle (not inside an interactive program). "
                "To answer an interactive prompt (Y/n, password, vim, pager, etc.) use "
                "`send_terminal_input` instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Single-line shell command to run visibly in Terminal.app.",
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
                "in the front Terminal.app tab, via System Events keystroke. "
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
                },
                "required": ["text"],
                "additionalProperties": False,
            },
        },
    ]


def call_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "list_supported_apps":
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        [
                            {
                                "key": key,
                                "display_name": value["display_name"],
                                "bundle_id": value["bundle_id"],
                            }
                            for key, value in SUPPORTED_APPS.items()
                        ],
                        ensure_ascii=False,
                        indent=2,
                    ),
                }
            ]
        }

    if name == "run_terminal_command":
        command = arguments.get("command", "")
        result_msg = run_terminal_command(command)
        return {"content": [{"type": "text", "text": result_msg}]}

    if name == "send_terminal_input":
        text = arguments.get("text", "")
        press_return = bool(arguments.get("press_return", True))
        result_msg = send_terminal_input(text, press_return=press_return)
        return {"content": [{"type": "text", "text": result_msg}]}

    if name != "get_app_context":
        raise ValueError(f"Unknown tool: {name}")

    app = arguments.get("app")
    if app not in SUPPORTED_APPS:
        raise ValueError(f"Unsupported app: {app}")

    max_chars = int(arguments.get("max_chars", 12000))
    max_chars = min(max(max_chars, 200), 50000)
    should_redact = bool(arguments.get("redact_secrets", True))

    text = SUPPORTED_APPS[app]["reader"]()
    text = text[-max_chars:]
    if should_redact:
        text = redact(text)

    if not text.strip():
        text = f"No readable context returned from {SUPPORTED_APPS[app]['display_name']}."

    return {"content": [{"type": "text", "text": text}]}


def handle_request(request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    try:
        if method == "initialize":
            return make_result(
                {
                    "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
                request_id,
            )

        if method == "notifications/initialized":
            return None

        if method == "tools/list":
            return make_result({"tools": tool_list()}, request_id)

        if method == "tools/call":
            return make_result(
                call_tool(params.get("name", ""), params.get("arguments") or {}),
                request_id,
            )

        return make_error(-32601, f"Method not found: {method}", request_id)
    except Exception as exc:  # noqa: BLE001 - MCP should report tool errors to client.
        return make_error(-32000, str(exc), request_id)


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
