#!/usr/bin/env python3
"""relay-worker: daemon that processes tasks from the agent-relay queue."""

import json
import logging
import os
import shutil
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("Error: 'requests' not installed. Run: pip install requests watchdog", file=sys.stderr)
    sys.exit(1)


def load_env(relay_dir: Path):
    """Load .env file into environment (does not overwrite existing vars)."""
    env_path = relay_dir / ".env"
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


def load_config(relay_dir: Path) -> dict:
    load_env(relay_dir)
    config_path = relay_dir / "config.json"
    with open(config_path) as f:
        return json.load(f)


class RelayWorker:
    def __init__(self, relay_dir: Path):
        self.relay_dir = relay_dir
        self.queue_dir = relay_dir / "queue"
        self.processing_dir = relay_dir / "processing"
        self.done_dir = relay_dir / "done"
        self.archive_dir = relay_dir / "archive"
        self.pid_file = relay_dir / "relay-worker.pid"
        self.pending_log = relay_dir / "pending.log"
        self.costs_log = relay_dir / "logs" / "costs.log"

        self.config = load_config(relay_dir)
        self._setup_logging()
        self._write_pid()
        self.running = True
        self._lock = threading.Lock()

    def _setup_logging(self):
        log_cfg = self.config.get("logging", {})
        level_name = log_cfg.get("level", "info").upper()
        log_file = os.path.expanduser(
            log_cfg.get("file", str(self.relay_dir / "logs" / "relay.log"))
        )
        logging.basicConfig(
            level=getattr(logging, level_name, logging.INFO),
            format="%(asctime)s [%(levelname)s] %(message)s",
            handlers=[
                logging.FileHandler(log_file),
                logging.StreamHandler(sys.stdout),
            ],
        )
        self.log = logging.getLogger("relay-worker")

    def _write_pid(self):
        with open(self.pid_file, "w") as f:
            f.write(str(os.getpid()))
        self.log.info(f"relay-worker started (pid {os.getpid()})")

    def _cleanup_pid(self):
        try:
            self.pid_file.unlink()
        except FileNotFoundError:
            pass

    def _remove_from_pending(self, task_id: str):
        if not self.pending_log.exists():
            return
        try:
            with open(self.pending_log) as f:
                lines = f.readlines()
            filtered = [l for l in lines if not l.startswith(task_id + " |")]
            tmp = self.pending_log.with_suffix(".tmp")
            with open(tmp, "w") as f:
                f.writelines(filtered)
            tmp.replace(self.pending_log)
        except Exception as e:
            self.log.warning(f"Failed to remove {task_id} from pending.log: {e}")

    def _resolve_api_key(self) -> str:
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raw = self.config.get("openrouter", {}).get("api_key", "")
            if raw.startswith("$"):
                key = os.environ.get(raw[1:], "")
            else:
                key = raw
        return key

    def _call_api_with_retry(self, task: dict) -> dict:
        cfg = self.config
        model = task.get("model") or cfg.get("default_model", "qwen/qwen3-coder-480b-a35b")
        backoff = cfg.get("retry", {}).get("backoff_seconds", [2, 5, 15])
        max_attempts = cfg.get("retry", {}).get("max_attempts", 3)
        timeout = cfg.get("timeouts", {}).get("worker_response_seconds", 120)
        base_url = cfg.get("openrouter", {}).get("base_url", "https://openrouter.ai/api/v1")
        api_key = self._resolve_api_key()

        user_content = task.get("prompt", "")
        if task.get("context"):
            user_content = f"{task['context']}\n\n{user_content}"

        last_error = None
        for attempt in range(max_attempts):
            if attempt > 0:
                delay = backoff[min(attempt - 1, len(backoff) - 1)]
                self.log.info(f"Retry {attempt}/{max_attempts - 1} for {task['task_id']} (delay {delay}s)")
                time.sleep(delay)

            try:
                resp = requests.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "HTTP-Referer": "agent-relay",
                        "X-Title": "agent-relay",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {
                                "role": "system",
                                "content": "You are a coding assistant. Respond in the requested output format only.",
                            },
                            {"role": "user", "content": user_content},
                        ],
                    },
                    timeout=timeout,
                )

                if resp.status_code == 200:
                    data = resp.json()
                    response_text = data["choices"][0]["message"]["content"]
                    usage = data.get("usage", {})
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)
                    # Use actual cost if OpenRouter provides it, otherwise estimate
                    cost = float(usage.get("cost", 0) or 0)
                    if cost == 0:
                        cost = (input_tokens * 0.000001) + (output_tokens * 0.000002)

                    return {
                        "task_id": task["task_id"],
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        "model_used": model,
                        "status": "success",
                        "response": response_text,
                        "tokens_used": {"input": input_tokens, "output": output_tokens},
                        "estimated_cost_usd": cost,
                        "error": None,
                    }

                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
                self.log.warning(f"API error for {task['task_id']}: {last_error}")
                # Don't retry on auth errors
                if resp.status_code in (401, 403):
                    break

            except requests.Timeout:
                last_error = "Request timed out"
                self.log.warning(f"Timeout for task {task['task_id']}")
            except Exception as e:
                last_error = str(e)
                self.log.warning(f"Request error for task {task['task_id']}: {e}")

        return {
            "task_id": task["task_id"],
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "model_used": model,
            "status": "error",
            "response": "",
            "tokens_used": {"input": 0, "output": 0},
            "estimated_cost_usd": 0.0,
            "error": last_error or "Unknown error",
        }

    def process_task(self, task_id: str):
        with self._lock:
            queue_json = self.queue_dir / f"{task_id}.json"
            queue_sentinel = self.queue_dir / f"{task_id}.ready"
            proc_json = self.processing_dir / f"{task_id}.json"
            proc_sentinel = self.processing_dir / f"{task_id}.ready"

            if not queue_json.exists() or not queue_sentinel.exists():
                return  # Already claimed

            try:
                shutil.move(str(queue_json), str(proc_json))
                shutil.move(str(queue_sentinel), str(proc_sentinel))
            except Exception as e:
                self.log.error(f"Failed to claim task {task_id}: {e}")
                return

        self.log.info(f"Processing task {task_id}")

        try:
            with open(proc_json) as f:
                task = json.load(f)
        except Exception as e:
            self.log.error(f"Failed to read task {task_id}: {e}")
            return

        result = self._call_api_with_retry(task)

        done_json = self.done_dir / f"{task_id}.json"
        done_sentinel = self.done_dir / f"{task_id}.ready"

        with open(done_json, "w") as f:
            json.dump(result, f, indent=2)
        with open(done_sentinel, "w") as f:
            pass

        # Log cost
        if result["status"] == "success":
            tokens = result["tokens_used"]
            ts = result["completed_at"]
            cost = result["estimated_cost_usd"]
            with open(self.costs_log, "a") as f:
                f.write(
                    f"{task_id} | {ts} | {result['model_used']} | "
                    f"{tokens['input']} | {tokens['output']} | {cost:.6f}\n"
                )

        # Archive task
        archive_json = self.archive_dir / f"{task_id}.json"
        try:
            shutil.move(str(proc_json), str(archive_json))
            proc_sentinel.unlink(missing_ok=True)
        except Exception as e:
            self.log.warning(f"Failed to archive {task_id}: {e}")

        self._remove_from_pending(task_id)
        self.log.info(f"Task {task_id} done: {result['status']}")

    def _scan_queue(self):
        """Process all sentinel files currently in queue."""
        if not self.queue_dir.exists():
            return
        for fname in list(self.queue_dir.iterdir()):
            if fname.name.endswith(".ready") and self.running:
                task_id = fname.stem
                try:
                    self.process_task(task_id)
                except Exception as e:
                    self.log.error(f"Error processing task {task_id}: {e}")

    def _watchdog_loop(self):
        """Recover stale tasks stuck in processing/."""
        expiry = self.config.get("timeouts", {}).get("task_expiry_seconds", 600)
        while self.running:
            time.sleep(60)
            if not self.running:
                break
            now = datetime.now(timezone.utc)
            if not self.processing_dir.exists():
                continue
            for fname in list(self.processing_dir.iterdir()):
                if not fname.name.endswith(".json"):
                    continue
                task_id = fname.stem
                try:
                    with open(fname) as f:
                        task = json.load(f)
                    created = datetime.fromisoformat(task["created_at"].replace("Z", "+00:00"))
                    age = (now - created).total_seconds()
                    if age > expiry:
                        self.log.warning(f"Recovering stale task {task_id} (age {age:.0f}s)")
                        q_json = self.queue_dir / f"{task_id}.json"
                        q_sentinel = self.queue_dir / f"{task_id}.ready"
                        p_sentinel = self.processing_dir / f"{task_id}.ready"
                        shutil.move(str(fname), str(q_json))
                        if p_sentinel.exists():
                            shutil.move(str(p_sentinel), str(q_sentinel))
                        else:
                            q_sentinel.touch()
                except Exception as e:
                    self.log.error(f"Watchdog error for {task_id}: {e}")

    def run(self):
        # Process tasks already in queue before we started
        self._scan_queue()

        # Watchdog thread
        threading.Thread(target=self._watchdog_loop, daemon=True).start()

        poll_interval = self.config.get("watcher", {}).get("poll_interval_seconds", 2)

        try:
            from watchdog.observers import Observer
            from watchdog.events import FileSystemEventHandler

            worker = self

            class QueueHandler(FileSystemEventHandler):
                def on_created(self, event):
                    if not event.is_directory and event.src_path.endswith(".ready"):
                        task_id = os.path.basename(event.src_path)[:-6]
                        try:
                            worker.process_task(task_id)
                        except Exception as e:
                            worker.log.error(f"Error processing task {task_id}: {e}")

            observer = Observer()
            observer.schedule(QueueHandler(), str(self.queue_dir), recursive=False)
            observer.start()
            self.log.info(f"Watching {self.queue_dir} (watchdog)")

            while self.running:
                time.sleep(1)

            observer.stop()
            observer.join()

        except ImportError:
            self.log.info(f"watchdog not available, polling every {poll_interval}s")
            while self.running:
                self._scan_queue()
                time.sleep(poll_interval)

        self._cleanup_pid()
        self.log.info("relay-worker stopped")


def main():
    relay_dir = Path(os.environ.get("AGENT_RELAY_DIR", os.path.expanduser("~/.agent-relay")))

    worker = RelayWorker(relay_dir)

    def handle_signal(signum, frame):
        worker.log.info(f"Signal {signum} received, shutting down...")
        worker.running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        worker.run()
    except Exception as e:
        logging.error(f"Fatal: {e}", exc_info=True)
        worker._cleanup_pid()
        sys.exit(1)


if __name__ == "__main__":
    main()
