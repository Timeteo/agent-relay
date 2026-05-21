# Agent Relay — Build Spec
**Handoff document for Claude Code implementation**

---

## Overview

Build a file-based inter-agent relay system that allows Claude Code (CC) to delegate tasks to sub-agents running cheaper models via OpenRouter, without hitting CC's rate limits or cooldown. The system is model-agnostic, compaction-proof, cross-platform (macOS + Debian Linux), and requires no API routing proxies.

---

## Architecture

```
CC (interactive, subscription)
  │
  ├── relay-drop ──→ ~/.agent-relay/queue/{task_id}.json
  │                                       {task_id}.ready  ← sentinel
  │
  │             relay-worker (daemon)
  │               watches queue/
  │               moves to processing/ (atomic claim)
  │               calls OpenRouter API
  │               writes done/{task_id}.json
  │               writes done/{task_id}.ready ← sentinel
  │               moves task to archive/
  │
  └── relay-pickup ←── ~/.agent-relay/done/{task_id}.json
```

---

## Directory Structure

```
~/.agent-relay/
  config.json          # main config (chmod 600)
  queue/               # CC writes tasks here
  processing/          # worker moves task here on pickup (atomic claim)
  done/                # worker writes responses here
  archive/             # completed tasks moved here (audit trail)
  logs/
    relay.log          # operational log
    costs.log          # per-task token/cost tracking
  relay-worker.pid     # daemon PID file
  pending.log          # human-readable outstanding task summary
```

---

## Config Schema

File: `~/.agent-relay/config.json`
Permissions: `chmod 600`

```json
{
  "version": "1.0",
  "relay_dir": "~/.agent-relay",
  "default_model": "qwen/qwen3-coder-480b-a35b",
  "fallback_model": "google/gemini-flash-lite",
  "openrouter": {
    "api_key": "$OPENROUTER_API_KEY",
    "base_url": "https://openrouter.ai/api/v1"
  },
  "watcher": {
    "method": "inotify",
    "poll_interval_seconds": 2
  },
  "timeouts": {
    "worker_response_seconds": 120,
    "task_expiry_seconds": 600
  },
  "retry": {
    "max_attempts": 3,
    "backoff_seconds": [2, 5, 15]
  },
  "logging": {
    "level": "info",
    "file": "~/.agent-relay/logs/relay.log"
  }
}
```

---

## File Schemas

### Task File — `queue/{task_id}.json`

```json
{
  "task_id": "uuid-v4",
  "created_at": "ISO8601",
  "model": "qwen/qwen3-coder-480b-a35b",
  "prompt": "...",
  "context": "...",
  "output_format": "code|explanation|json|markdown",
  "originator": "claude-code"
}
```

- `model` is optional — if omitted, worker uses `config.default_model`
- CC should specify model based on task type (see Model Selection Guide below)
- Sentinel file `{task_id}.ready` written **after** JSON is fully written (prevents partial reads)

### Response File — `done/{task_id}.json`

```json
{
  "task_id": "uuid-v4",
  "completed_at": "ISO8601",
  "model_used": "...",
  "status": "success|error|timeout",
  "response": "...",
  "tokens_used": {
    "input": 0,
    "output": 0
  },
  "estimated_cost_usd": 0.0,
  "error": null
}
```

---

## Components

### 1. `relay-init` (shell script, run once at setup)

- Creates full directory structure under `~/.agent-relay/`
- Writes default `config.json` with `chmod 600`
- Installs Python dependencies (`pip install requests watchdog`)
- Copies component scripts to `~/.agent-relay/bin/` and adds to PATH
- Detects platform:
  - **macOS**: writes LaunchAgent plist to `~/Library/LaunchAgents/com.agent-relay.worker.plist`, loads it
  - **Linux**: writes systemd user service to `~/.config/systemd/user/agent-relay.service`, enables it
- Wires CC hooks by appending to `~/.claude/settings.json`:
  ```json
  {
    "hooks": {
      "post-compact": "relay-status",
      "post-start": "relay-status"
    }
  }
  ```
- Writes CLAUDE.md instructions (see Claude Instructions section below)
- Prints setup summary when complete

---

### 2. `relay-drop` (shell script CC calls)

**Usage:**
```bash
relay-drop \
  --prompt "refactor this for async/await" \
  --context "$(cat myfile.swift)" \
  --format "code" \
  --model "qwen/qwen3-coder-480b-a35b"
```

**Behavior:**
1. Check daemon is running (PID file exists and process is alive) — exit with clear error if not
2. Generate UUID v4 task_id
3. Write `queue/{task_id}.json`
4. Write `queue/{task_id}.ready` sentinel
5. Append to `pending.log`: `{task_id} | {timestamp} | {prompt first 80 chars}`
6. Print to stdout: `RELAY_TASK_ID={task_id}` — CC captures this

**Arguments:**
- `--prompt` (required)
- `--context` (optional, file content or additional context)
- `--format` (optional, default: `markdown`)
- `--model` (optional, overrides config default)

---

### 3. `relay-worker` (Python daemon)

**Responsibilities:**
- Watch `queue/` for `.ready` sentinel files (inotify on Linux, FSEvents/polling on macOS)
- On sentinel detected:
  1. Atomically move `{task_id}.json` + `{task_id}.ready` to `processing/` (prevents double-pickup)
  2. Parse task JSON
  3. Resolve model: task `model` field → `config.default_model`
  4. Call OpenRouter API with retry/backoff (3 attempts: 2s, 5s, 15s delays)
  5. On success: write `done/{task_id}.json`, write `done/{task_id}.ready` sentinel
  6. On all retries exhausted: write `done/{task_id}.json` with `status: error`
  7. Move task files from `processing/` to `archive/`
  8. Append to `costs.log`: `{task_id} | {model} | {input_tokens} | {output_tokens} | {cost_usd}`
  9. Remove task from `pending.log`

**Watchdog (runs every 60 seconds):**
- Scan `processing/` for tasks older than `config.timeouts.task_expiry_seconds`
- Return stale tasks to `queue/` (re-write sentinel) and log the recovery

**OpenRouter call:**
```python
POST https://openrouter.ai/api/v1/chat/completions
Headers:
  Authorization: Bearer {api_key}
  HTTP-Referer: agent-relay
  X-Title: agent-relay
Body:
  model: {model}
  messages: [
    {"role": "system", "content": "You are a coding assistant. Respond in the requested output format only."},
    {"role": "user", "content": "{context}\n\n{prompt}"}
  ]
```

---

### 4. `relay-pickup` (shell script CC calls)

**Usage:**
```bash
relay-pickup --task-id abc-123-def-456
```

**Output:**
- `PENDING` — sentinel not yet present
- `TIMEOUT` — task age exceeds `task_expiry_seconds`
- `ERROR: {error message}` — worker reported error
- Full response content — on success (also cleans up `done/` sentinel files)

**CC should call this when it reaches a natural pause or needs the result. If PENDING, note it and continue other work.**

---

### 5. `relay-status` (shell script)

**Usage:**
```bash
relay-status
```

**Output example:**
```
=== Agent Relay Status ===
Daemon:  RUNNING (pid 12345)
Queue:   0 pending
Processing: 1 in-flight
Done:    2 ready for pickup

READY FOR PICKUP:
  abc-123  [12m ago]  refactor payment handler for async...
  def-456  [3m ago]   write unit tests for AuthService...

IN FLIGHT:
  ghi-789  [1m ago]   generate OpenAPI spec from routes...

Monthly cost (May 2026): $0.42
==========================
```

**Always run at session start and post-compaction (wired automatically by relay-init).**

---

### 6. `relay-daemon` (shell script)

**Usage:**
```bash
relay-daemon start|stop|restart|status
```

**Behavior:**
- `start`: launch relay-worker as background process, write PID file
- `stop`: kill process from PID file, remove PID file
- `restart`: stop + start
- `status`: check PID file, verify process alive, print status

Note: On macOS and Linux, the daemon is managed by LaunchAgent/systemd respectively (set up by relay-init). `relay-daemon` is a manual override for development/debugging.

---

## Autostart

### macOS — LaunchAgent

File: `~/Library/LaunchAgents/com.agent-relay.worker.plist`

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "...">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.agent-relay.worker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>~/.agent-relay/bin/relay-worker.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>~/.agent-relay/logs/relay.log</string>
  <key>StandardErrorPath</key>
  <string>~/.agent-relay/logs/relay.log</string>
</dict>
</plist>
```

### Linux — systemd user service

File: `~/.config/systemd/user/agent-relay.service`

```ini
[Unit]
Description=Agent Relay Worker
After=network.target

[Service]
ExecStart=/usr/bin/python3 ~/.agent-relay/bin/relay-worker.py
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

Enable: `systemctl --user enable --now agent-relay`

---

## Claude Code Hooks

Add to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "post-compact": "relay-status",
    "post-start": "relay-status"
  }
}
```

These are wired automatically by `relay-init`.

---

## CLAUDE.md Instructions

`relay-init` appends this block to `~/.claude/CLAUDE.md` (global):

```markdown
## Agent Relay

You have access to an agent relay system for delegating tasks to sub-agents running 
cheaper models via OpenRouter. Use it to offload work and stay under rate limits.

### Session Start
Always check relay-status at session start (wired via hook, but verify output).

### When to Delegate
DELEGATE these task types:
- Boilerplate code generation (CRUD, tests, stubs)
- Large refactors with clear instructions
- Documentation generation
- Format conversion
- Research/lookup tasks that don't need your full project context

KEEP LOCALLY:
- Architecture decisions
- Anything requiring full project context
- Security-sensitive logic
- Tasks where you'd need to verify the output carefully anyway

### Model Selection Guide
| Task Type | Recommended Model |
|-----------|-------------------|
| Complex code generation | qwen/qwen3-coder-480b-a35b |
| Reasoning/debugging | deepseek/deepseek-r1 |
| Fast/cheap boilerplate | google/gemini-flash-lite (free) |
| General coding | deepseek/deepseek-v3 |

### Workflow
1. Drop task: `relay-drop --prompt "..." --context "..." --format code --model "..."`
2. Capture task ID from output: `RELAY_TASK_ID=abc-123`
3. Continue other work
4. At natural pause: `relay-pickup --task-id abc-123`
5. If PENDING: note it, keep working, check again later
6. If ready: ingest response and continue

### Writing Good Sub-Agent Prompts
- Be specific and self-contained — the sub-agent has no project context beyond what you provide
- Include relevant code snippets in --context
- Specify the exact output format you need
- Don't delegate tasks that require back-and-forth clarification
```

---

## Security

- `config.json` must be `chmod 600` (enforced by `relay-init`)
- API key read from environment variable `$OPENROUTER_API_KEY` at runtime, not hardcoded
- `relay-init` validates key is set before completing setup

---

## Implementation Order

Build in this sequence:

1. Create GitHub repo `agent-relay`, initialize with README placeholder and `.gitignore`
2. Set up repo directory structure (`bin/`, `autostart/`, `claude/`)
3. `relay-init` — directory structure + config only (no hooks/autostart yet)
2. `relay-drop` — task file writer
3. `relay-worker` — core daemon (basic, no watchdog yet)
4. `relay-pickup` — response reader
5. `relay-status` — status reporter
6. Test end-to-end manually
7. Add watchdog to `relay-worker`
8. Add retry/backoff to `relay-worker`
9. Add cost tracking
10. `relay-daemon` — manual control script
11. Wire autostart (LaunchAgent / systemd)
12. Wire CC hooks and CLAUDE.md via `relay-init`
13. Full end-to-end test on macOS
14. Test on Debian LXC

---

## GitHub Repository

### Repo Structure

```
agent-relay/                        # GitHub repo root
  bin/
    relay-drop                      # shell script
    relay-pickup                    # shell script
    relay-daemon                    # shell script
    relay-status                    # shell script
    relay-worker.py                 # Python daemon
    relay-init                      # shell script (first-time setup)
    relay-install                   # shell script (deploy from repo)
    relay-update                    # shell script (pull + redeploy)
  autostart/
    com.agent-relay.worker.plist    # macOS LaunchAgent template
    agent-relay.service             # Linux systemd template
  claude/
    CLAUDE.md.append                # block relay-init appends to ~/.claude/CLAUDE.md
  .env.example                      # API key template
  .gitignore
  README.md
```

### .gitignore

```
# Never commit local data or secrets
config.json
.env
queue/
processing/
done/
archive/
logs/
*.pid
pending.log
__pycache__/
*.pyc
```

### .env.example

```bash
# Copy to ~/.agent-relay/.env and fill in your key
# relay-worker loads this automatically at startup
OPENROUTER_API_KEY=your_key_here
```

### relay-install (deploy script)

Run after `git clone` or `git pull`. Idempotent — safe to run multiple times.

**Behavior:**
1. Create `~/.agent-relay/bin/` if not exists
2. Copy all `bin/` scripts to `~/.agent-relay/bin/`
3. `chmod +x` all scripts
4. Add `~/.agent-relay/bin` to PATH in `~/.zshrc` / `~/.bashrc` if not already present
5. Create data dirs (`queue/`, `processing/`, `done/`, `archive/`, `logs/`) if not exist
6. Write default `config.json` if not exists (never overwrite existing)
7. Write `.env` template if not exists (never overwrite existing)
8. Detect platform and install autostart:
   - macOS: copy plist template → `~/Library/LaunchAgents/`, `launchctl load`
   - Linux: copy service template → `~/.config/systemd/user/`, `systemctl --user enable --now`
9. Wire CC hooks into `~/.claude/settings.json` if not already present
10. Append CLAUDE.md block to `~/.claude/CLAUDE.md` if not already present
11. Set `chmod 600` on `config.json` and `.env`
12. Print next steps (edit `.env`, add OpenRouter key, run `relay-daemon status`)

### relay-update (update script)

```bash
#!/bin/bash
# Run from repo root or any location
cd "$(dirname "$0")/.." || exit 1
git pull
./bin/relay-install
relay-daemon restart
echo "Agent relay updated and restarted."
```

### README.md (outline — CC writes the full content)

Sections to include:
- What this is and why
- Prerequisites (Python 3.8+, OpenRouter account, Claude Code)
- Quick start (clone → relay-install → edit .env → relay-daemon start)
- Usage guide (relay-drop, relay-pickup, relay-status)
- Model selection reference
- Cross-platform notes (Mac vs Linux)
- Updating (relay-update)
- Troubleshooting

### Suggested GitHub repo name

`agent-relay`

---

## Dependencies

- Python 3.8+
- `pip install requests watchdog`
- OpenRouter API key set as `$OPENROUTER_API_KEY`
- Claude Code installed and logged in

---

## Notes

- Tasks persist in the filesystem — safe across CC restarts, compaction, and session interruptions
- `relay-status` is the source of truth for outstanding work after any context loss
- The system is intentionally simple — no database, no message broker, just files
- Adding a new model requires no code changes — just change the model string in the task or config
