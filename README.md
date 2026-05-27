# Codex Work With Apps MCP

A local stdio MCP server that gives Codex a controlled "Work with Apps"-style
tool for macOS app context.

Current support:

- Terminal.app via AppleScript for JavaScript
- iTerm2 via AppleScript

The server returns text from the front or selected terminal session and redacts
common secret patterns by default. It does not bypass macOS permissions. The
process that launches this server still needs the right macOS Automation and
Accessibility permissions.

It can also send a visible single-line command to the front Terminal.app tab.
This write path is intentionally narrow and refuses obvious dangerous commands
such as `sudo`, recursive force deletion, disk erase commands, and multi-line
commands.

## Tools

- `list_supported_apps`: list readable macOS apps.
- `get_app_context`: read recent context from `terminal` or `iterm2`.
- `run_terminal_command`: run a new visible single-line shell command in Terminal.app.
- `send_terminal_input`: type text or special keys into the foreground Terminal.app process.

## Terminal Write Modes

This server exposes two different write paths because Terminal has two different
interaction models.

`run_terminal_command` uses Terminal.app's AppleScript `do script` command. This
asks Terminal to execute a new shell command. It is appropriate only when the
front tab is sitting at an idle shell prompt.

`send_terminal_input` uses System Events to simulate real keyboard input. That
input goes through Terminal's normal key handling and into the current pseudo
terminal. Use it for interactive prompts and terminal UI programs, such as:

- answering `Y/n` prompts
- entering passwords or tokens with `sensitive=true`
- sending `ctrl+c`, `ctrl+d`, arrow keys, tab, or escape
- navigating pagers, vim, fzf, REPLs, and other TUI programs

Do not use `run_terminal_command` to answer an interactive prompt. `do script`
does not mean "write this text to stdin"; it starts a new command in Terminal.
For prompt input, use `send_terminal_input`.

## Test Directly

From the repo root:

```bash
python3 macos_work_with_apps.py
```

Then paste one JSON-RPC request per line:

```json
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"manual","version":"0"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}
{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_app_context","arguments":{"app":"terminal","max_chars":4000}}}
{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"run_terminal_command","arguments":{"command":"pwd"}}}
{"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"send_terminal_input","arguments":{"text":"y","press_return":true}}}
{"jsonrpc":"2.0","id":6,"method":"tools/call","params":{"name":"send_terminal_input","arguments":{"text":"secret-value","press_return":true,"sensitive":true}}}
```

Press `Ctrl-D` to exit.

## Codex Config

Add this to `~/.codex/config.toml`:

```toml
[mcp_servers.macos_work_with_apps]
command = "python3"
args = ["/Users/vickers/Documents/MCP_Creator/codex_work_with_apps/macos_work_with_apps.py"]
startup_timeout_sec = 10
```

The same config is included as:

- `config/codex-config-snippet.toml`
- `config/codex-config.patch`

Then restart Codex so it reloads MCP servers.

## macOS Permissions

When macOS prompts for permission, allow the launcher process to control/read
Terminal or iTerm2. Depending on how Codex starts MCP servers, the process may
show up as Codex, Python, or Terminal under:

- System Settings -> Privacy & Security -> Automation
- System Settings -> Privacy & Security -> Accessibility

If no prompt appears, run the direct test once from Terminal to trigger the
permission request, then restart Codex.

## License

MIT
