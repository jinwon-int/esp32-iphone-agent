# Claude CLI adapter (`claude_adapter.py`)

An OpenAI-compatible `POST /v1/chat/completions` in front of the Claude Code CLI (`claude -p`), for the phone app
and the server when the model should come from a node that is signed in to Claude Code (subscription OAuth) rather
than from an API key (#67 P3d). One file, standard library only. **This directory is the canonical copy**; the node
runs a byte-for-byte copy of it (see Deployment).

What it does, per request:

- Takes the `system` message as `--system-prompt`, the other messages as one user turn (`text` and
  `data:image/...;base64,` images → Claude content blocks), and runs `claude -p` with **tools off**,
  `--strict-mcp-config` (no claude.ai connector tools or instructions), `--setting-sources project` in an empty
  working directory (no user hooks or settings), `--no-session-persistence`. The OAuth token is never read or
  copied: the CLI uses its own login.
- Replies with the last `result` event as `choices[0].message.content` and the usage counts.
- Logs timing and counts only — never prompt text or images.

## Model and effort

| Request | Effect |
|---|---|
| `model: sonnet` / `opus` / `haiku` / `claude-…` | passed to `--model`; anything else → `ADAPTER_DEFAULT_MODEL` (`sonnet`) |
| `model: claude-sonnet-5-5@low` | the suffix names the effort (`low`, `medium`, `high`, `xhigh`, `max`) → `--effort low`. The app has no effort field, so this is how an A/B is run from its model settings |
| `reasoning_effort: low` (body) | same, when the model has no suffix |
| `ADAPTER_EFFORT=low` (env) | the default when neither names one; unset = the CLI's own default |

Precedence: model suffix → body → env. An unknown level is ignored, not sent. The log line carries
`effort=<level|default>`.

Measured 2026-10-22 (#67): `low` cuts the model's think time about 25 % on text-only steps and not at all on
screenshot-judging steps, and made the planner ignore "Plain text only, in English" once on a replan — the fleet runs
the default.

## Settings (environment)

| Variable | Default | Meaning |
|---|---|---|
| `ADAPTER_TOKEN_FILE` | (required) | file with the bearer token the app sends (`Authorization: Bearer …`); mode 0600 |
| `ADAPTER_HOST` / `ADAPTER_PORT` | `127.0.0.1` / `18765` | bind address — the tailnet address on the node, never a public one |
| `CLAUDE_BIN` | `claude` | the CLI to run |
| `ADAPTER_WORKDIR` | `./empty` next to the script | the CLI's cwd: keep it empty so no project settings apply |
| `ADAPTER_TIMEOUT` | `110` | seconds per request |
| `ADAPTER_DEFAULT_MODEL` | `sonnet` | fallback model |
| `ADAPTER_CONCURRENCY` | `2` | parallel CLI runs |
| `ADAPTER_EFFORT` | (unset) | default effort, see above |

`GET /v1/health` → `{"ok": true}` without a token.

## Deployment

The node that is signed in to Claude Code runs the adapter as a systemd unit for the phone app over Tailscale:

| | |
|---|---|
| unit | `hidagent-claude-adapter` (`hidagent-claude-adapter.service` here is the deployed unit) |
| user / dir | an unprivileged user / `~/.local/share/hidagent-claude/` |
| script | `…/adapter.py` (0600) = this `claude_adapter.py` |
| token | `…/token` (0600) — the same value the phone app and the task inbox (`hidagent-inbox`) use; never in the repo or the Wiki |
| bind | the node's Tailscale address, port 18765; UFW allows it on `tailscale0` only |
| CLI | `/usr/bin/claude` |

Deploying a change (a service restart: owner approval, like any node mutation):

```sh
scp server/adapter/claude_adapter.py <node>:/tmp/adapter.py.new
ssh <node> 'D=/home/<user>/.local/share/hidagent-claude; cp -p $D/adapter.py $D/adapter.py.bak-$(date -u +%Y%m%dT%H%M%SZ)
  && install -o <user> -g <user> -m 600 /tmp/adapter.py.new $D/adapter.py && rm /tmp/adapter.py.new
  && python3 -m py_compile $D/adapter.py && systemctl restart hidagent-claude-adapter && systemctl is-active hidagent-claude-adapter'
curl -s http://<tailnet-address>:18765/v1/health
```

Then one real request from a client (the app, or `curl` with the token) and check the journal line
(`journalctl -u hidagent-claude-adapter -n 1`) shows `ok model=… effort=…`. Roll back by restoring the newest
`adapter.py.bak-*` and restarting. Compare the deployed file with this one by `sha256sum` on both sides.

Before `--effort` support the deployed file was `ND-4035` in the family Wiki (2026-10-17, `--strict-mcp-config`);
the 2026-10-22 deployment added the effort rules above. Wiki: the node's CHANGES page.

## Tests

`tests/test_claude_adapter.py` pins the model/effort rules, the content blocks and the exact `claude -p` command
(no CLI is run: `subprocess.run` is replaced).
