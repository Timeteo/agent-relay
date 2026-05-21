# agent-relay

A file-based inter-agent relay system for Claude Code. Delegate tasks to sub-agents running cheaper models via OpenRouter — without hitting Claude Code's rate limits or burning subscription quota on boilerplate work.

**How it works:** CC drops a task file to a watched queue directory. A background Python daemon picks it up, calls the OpenRouter API, and writes the result. CC picks up the response at its next natural pause. All state lives in plain JSON files — survives restarts, compaction, and context loss.

---

## Prerequisites

- Python 3.8+
- [OpenRouter](https://openrouter.ai) account and API key
- Claude Code installed and configured

---

## Quick Start

```bash
git clone https://github.com/yourusername/agent-relay
cd agent-relay
./bin/relay-install
```

Then:

```bash
# Edit your API key
nano ~/.agent-relay/.env   # set OPENROUTER_API_KEY=sk-or-...

# Start the daemon
relay-daemon start

# Verify
relay-status
```

---

## Usage

### Drop a task

```bash
relay-drop \
  --prompt "write unit tests for this function" \
  --context "$(cat myfile.py)" \
  --format code \
  --model "qwen/qwen3-coder-480b-a35b"
# → RELAY_TASK_ID=abc-123-def-456
```

### Pick up the result

```bash
relay-pickup --task-id abc-123-def-456
# → PENDING  (still running — come back later)
# → <response content>  (done)
# → ERROR: <message>
# → TIMEOUT
```

### Check status

```bash
relay-status
```

```
=== Agent Relay Status ===
Daemon:     RUNNING (pid 12345)
Queue:      0 pending
Processing: 1 in-flight
Done:       2 ready for pickup

READY FOR PICKUP:
  abc-123  [12m ago]  write unit tests for AuthService...
  def-456  [3m ago]   generate OpenAPI spec from routes...

IN FLIGHT:
  ghi-789  [1m ago]   refactor payment handler for async...

Monthly cost (May 2026): $0.42
==========================
```

---

## Model Selection

| Task Type | Recommended Model |
|-----------|-------------------|
| Complex code generation | `qwen/qwen3-coder-480b-a35b` |
| Reasoning / debugging | `deepseek/deepseek-r1` |
| Fast / cheap boilerplate | `google/gemini-2.0-flash-lite-001` (free tier) |
| General coding | `deepseek/deepseek-v3` |

Pass `--model` to override the default. Default is set in `~/.agent-relay/config.json`.

---

## Daemon Management

```bash
relay-daemon start
relay-daemon stop
relay-daemon restart
relay-daemon status
```

On macOS, the daemon is managed by a LaunchAgent (auto-starts on login).
On Linux, it's a systemd user service (`systemctl --user status agent-relay`).

`relay-daemon` is a manual override for development or if the autostart is not set up.

---

## Cross-Platform Notes

**macOS**
- LaunchAgent plist: `~/Library/LaunchAgents/com.agent-relay.worker.plist`
- Logs: `~/.agent-relay/logs/relay.log`
- `relay-install` loads the agent automatically via `launchctl`

**Linux (Debian/Ubuntu)**
- systemd user service: `~/.config/systemd/user/agent-relay.service`
- Enable: `systemctl --user enable --now agent-relay`
- Requires systemd lingering if you want it to run without a user session:
  `loginctl enable-linger $USER`

---

## Updating

```bash
relay-update
```

This runs `git pull` then `relay-install` and restarts the daemon.

---

## Troubleshooting

**Daemon won't start**
```bash
cat ~/.agent-relay/logs/relay.log
relay-daemon status
```

**Tasks stuck in processing/**
The watchdog recovers stale tasks automatically (default: after 600s). Check logs for `Recovering stale task`.

**API errors**
- Verify your key: `echo $OPENROUTER_API_KEY`
- Check `~/.agent-relay/.env` has the correct key
- Try a quick test: `relay-drop --prompt "say hello" --format markdown --model google/gemini-flash-lite`

**relay-status shows wrong PATH**
```bash
export PATH="$HOME/.agent-relay/bin:$PATH"
# or reload your shell: source ~/.zshrc
```

---

## Directory Layout

```
~/.agent-relay/
  config.json          # main config (chmod 600)
  .env                 # API key (chmod 600)
  queue/               # CC writes tasks here
  processing/          # worker claims tasks here
  done/                # worker writes responses here
  archive/             # completed tasks (audit trail)
  logs/
    relay.log          # operational log
    costs.log          # per-task token/cost tracking
  relay-worker.pid     # daemon PID
  pending.log          # outstanding task summary
```

---

## Security

- `config.json` and `.env` are `chmod 600` — not readable by other users
- API key is read from `$OPENROUTER_API_KEY` at runtime, never hardcoded
- Task files contain your prompts and context — stored only locally in `~/.agent-relay/`
