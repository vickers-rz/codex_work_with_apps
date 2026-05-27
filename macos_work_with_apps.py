#!/usr/bin/env python3
"""Minimal MCP server for reading selected macOS app context.

This is a local-only stdio MCP server. It exposes a small tool surface that reads
terminal context through macOS automation APIs after the user has granted the
launcher process permission in System Settings.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
import sys
from typing import Any


SERVER_NAME = "macos-work-with-apps"
SERVER_VERSION = "0.2.0"

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
    command = validate_terminal_command(command)
    quoted = shlex.quote(command)
    return run_osascript(
        f"""
        const app = Application("/System/Applications/Utilities/Terminal.app");
        app.activate();
        if (!app.running() || app.windows.length === 0) {{
          app.doScript({quoted});
        }} else {{
          app.doScript({quoted}, {{ in: app.windows[0].selectedTab }});
        }}
        "sent";
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
            "description": "Send a visible single-line command to the front Terminal.app tab.",
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
        run_terminal_command(command)
        return {"content": [{"type": "text", "text": f"Sent to Terminal: {command}"}]}

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
