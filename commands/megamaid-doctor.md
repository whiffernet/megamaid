---
description: Report megamaid install health — venv state, MCP connectivity, and the last bootstrap failure.
---

Diagnose the megamaid installation and report what you find. Do not change anything.

1. Read the plugin version:

   ```bash
   cat "${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json"
   ```

2. Check the bootstrapped venv and whether its stamp matches that version:

   ```bash
   ls -la "${MEGAMAID_STATE_DIR:-$HOME/.local/state/megamaid}/venv/bin" 2>&1 | head
   cat "${MEGAMAID_STATE_DIR:-$HOME/.local/state/megamaid}/venv/.plugin-version" 2>&1
   ```

3. Read the last 20 lines of the launch log. This is the only durable record of a
   bootstrap that Claude Code killed on MCP timeout:

   ```bash
   tail -20 "${MEGAMAID_STATE_DIR:-$HOME/.local/state/megamaid}/launch.log" 2>&1
   ```

4. Confirm the CLI runs through the launcher:

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/launch.py" --cli --help
   ```

5. Check whether Claude Code currently sees the MCP server:

   ```bash
   claude mcp list 2>&1 | grep -i megamaid
   ```

Report a short table: plugin version, venv present, stamp matches, CLI runs, MCP connected.

If the log's last entry is a `FAILED [MM-xx]` line, explain that code in plain language and
give the fix from the message. If the failure was a timeout during venv build, tell the user
that `MCP_TIMEOUT` defaults to 30000 ms, is read from Claude Code's own environment (a
plugin cannot raise it), and that running step 4 once builds the venv outside that budget —
after which the MCP server connects immediately.
