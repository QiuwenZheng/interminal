---
name: zellij
description: Guide for using Zellij terminal multiplexer to maintain persistent shell state (cd, env vars) across stateless terminal executions.
---

# Zellij: Persistent Terminal Workflows

When executing terminal commands via an MCP or a stateless runner, each `execute` call runs in an isolated channel. State (like `cd /path`, `export VAR=1`, or active virtual environments) **does not persist** to the next call. 

To solve this, use **Zellij**, a terminal multiplexer. By starting a Zellij daemon, you can maintain persistent state and drive it programmatically via its CLI (`zellij action`).

## 1. Starting a Session

Do **NOT** try to background the TUI with `&` (e.g., `zellij &`). It will crash during TTY initialization. Instead, start it in the foreground and let the execution timeout (or return a "partial" status). The daemon will fork safely.

```bash
# Start a named session (requires TERM to be set)
execute("TERM=xterm-256color zellij --session mysession", total_timeout=4)
```
*Note: Ignore or abandon the partial channel returned. The daemon survives in the background.*

## 2. Driving the Session via CLI

Once the session is running, you can interact with it completely statelessly using `zellij action`. You can chain these with `&&` for efficiency.

### Typing and Running Commands
Use `write-chars` to type text into the active pane, followed by `send-keys Enter` to execute it. This is superior to `new-pane -- cmd` because it preserves shell built-ins, pipes, and variables.

```bash
execute("zellij -s mysession action write-chars 'cd /var/log && ls -la' && zellij -s mysession action send-keys Enter")
```

### Reading Output
Do not use raw channel output (which contains messy ANSI/TUI drawing characters). Instead, ask Zellij to dump the clean screen content:

```bash
# Dump the current viewport
execute("zellij -s mysession action dump-screen")

# Dump with scrollback history
execute("zellij -s mysession action dump-screen --full")
```

## 3. Advanced Window Management

You can manipulate panes and tabs programmatically.

```bash
# Open a new pane or tab
execute("zellij -s mysession action new-pane")
execute("zellij -s mysession action new-tab")

# List panes/tabs (Useful for finding IDs)
execute("zellij -s mysession action list-panes")
execute("zellij -s mysession action list-tabs")
```

You can target specific panes or tabs without changing focus by passing `--pane-id` or `--tab-id`:
```bash
execute("zellij -s mysession action write-chars 'echo hello' --pane-id 3")
```

## 4. Teardown

When the task is complete, clean up the session:

```bash
execute("zellij delete-session mysession --force")
```
