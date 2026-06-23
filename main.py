import os
import sys
import json
import time
import threading
import platform
import signal
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

import requests

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR    = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "jobs.json"
LOG_DIR     = BASE_DIR / "logs"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
STARTUP_DELAY      = 30
RETRY_COUNT        = 5
RETRY_DELAY        = 30
WATCHDOG_INTERVAL  = 30
RATE_LIMIT_WAIT    = 60
SUMMARY_INTERVAL   = 30 * 60   # 30 minutes
SUMMARY_HOLD       = 10        # seconds to show summary before clearing

# ---------------------------------------------------------------------------
# Terminal colours
# ---------------------------------------------------------------------------
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
CYAN   = "\033[96m"
BOLD   = "\033[1m"
RESET  = "\033[0m"

# ---------------------------------------------------------------------------
# Job stats — thread-safe counters per job name
# ---------------------------------------------------------------------------
_stats_lock = threading.Lock()
_stats: dict = defaultdict(lambda: {"runs": 0, "success": 0, "failure": 0})


def record_stat(job_name: str, success: bool):
    with _stats_lock:
        _stats[job_name]["runs"] += 1
        if success:
            _stats[job_name]["success"] += 1
        else:
            _stats[job_name]["failure"] += 1


# ---------------------------------------------------------------------------
# Logging — date/hour based files
# ---------------------------------------------------------------------------
_log_lock = threading.Lock()


def _get_log_file(log_type: str) -> Path:
    now      = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    hour_str = now.strftime("%Y-%m-%d_%H")
    folder   = LOG_DIR / date_str
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{hour_str}_{log_type}.log"


def _write_log(log_type: str, lines: list):
    entry = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}]\n" + "\n".join(lines) + "\n\n"
    with _log_lock:
        with open(_get_log_file(log_type), "a", encoding="utf-8") as f:
            f.write(entry)


def _write_summary_file(content: str):
    now      = datetime.now()
    date_str = now.strftime("%Y-%m-%d")
    folder   = LOG_DIR / date_str
    folder.mkdir(parents=True, exist_ok=True)
    summary_file = folder / f"{date_str}_summary.log"
    with _log_lock:
        with open(summary_file, "a", encoding="utf-8") as f:
            f.write(content)


# ---------------------------------------------------------------------------
# Terminal output
# ---------------------------------------------------------------------------
def print_job_line(project: str, job_name: str, status_code, success: bool, reason: str = None):
    ts      = datetime.now().strftime("%H:%M:%S")
    project_col = f"{project:<12}"
    job_col     = f"{job_name:<42}"

    if success:
        result = f"{GREEN}{status_code} Success{RESET}"
    else:
        detail = reason or ""
        result = f"{RED}{status_code or 'ERR'} {detail[:50]}{RESET}"

    print(f"[{ts}] {CYAN}{project_col}{RESET}| {job_col}| {result}", flush=True)


def log_info(msg: str):
    print(f"{YELLOW}[INFO ] {msg}{RESET}", flush=True)


def log_warn(msg: str):
    print(f"{YELLOW}[WARN ] {msg}{RESET}", flush=True)


# ---------------------------------------------------------------------------
# File loggers
# ---------------------------------------------------------------------------
def log_success(project: str, job_name: str, status_code):
    record_stat(job_name, True)
    print_job_line(project, job_name, status_code, True)
    _write_log("success", [
        f"  PROJECT : {project}",
        f"  JOB     : {job_name}",
        f"  STATUS  : {status_code}",
        f"  RESULT  : Success",
    ])


def log_failure(project: str, job_name: str, reason: str, status_code=None, body=None):
    record_stat(job_name, False)
    print_job_line(project, job_name, status_code, False, reason)
    lines = [
        f"  PROJECT : {project}",
        f"  JOB     : {job_name}",
    ]
    if status_code is not None:
        lines.append(f"  STATUS  : {status_code}")
    lines.append(f"  REASON  : {reason}")
    if body:
        lines.append(f"  BODY    : {str(body)[:5000]}")
    _write_log("error", lines)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
def build_summary_text(ts: str, stats_snapshot: dict) -> str:
    total_runs    = sum(v["runs"]    for v in stats_snapshot.values())
    total_success = sum(v["success"] for v in stats_snapshot.values())
    total_failure = sum(v["failure"] for v in stats_snapshot.values())

    divider = "─" * 60
    lines = [
        f"\n{divider}",
        f" Summary @ {ts}",
        f"{divider}",
        f" Total runs : {total_runs:<6} Success : {total_success:<6} Failed : {total_failure}",
        f"{divider}",
    ]
    for job_name, v in sorted(stats_snapshot.items()):
        lines.append(
            f"  {job_name:<42}| runs: {v['runs']:<5} ok: {v['success']:<5} fail: {v['failure']}"
        )
    lines.append(divider)
    return "\n".join(lines) + "\n"


def print_summary_and_clear():
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _stats_lock:
        stats_snapshot = {k: dict(v) for k, v in _stats.items()}

    summary_text = build_summary_text(ts, stats_snapshot)

    # Print to terminal with colours
    print(f"\n{BOLD}{CYAN}{summary_text}{RESET}", flush=True)
    print(f"{YELLOW}Clearing terminal in {SUMMARY_HOLD} seconds...{RESET}", flush=True)

    # Write to daily summary file
    _write_summary_file(summary_text)

    time.sleep(SUMMARY_HOLD)
    os.system("clear" if platform.system() != "Windows" else "cls")
    log_info(f"Scheduler running — next summary in {SUMMARY_INTERVAL // 60} min")


def summary_loop(stop_event):
    while not stop_event.is_set():
        stop_event.wait(SUMMARY_INTERVAL)
        if stop_event.is_set():
            break
        print_summary_and_clear()


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
def load_config():
    if not CONFIG_FILE.exists():
        print(f"[ERROR] Config file not found: {CONFIG_FILE}")
        sys.exit(1)

    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            config = json.load(f)
    except json.JSONDecodeError as e:
        print(f"[ERROR] Malformed JSON in jobs.json: {e}")
        sys.exit(1)

    raw_jobs = config.get("jobs", [])
    if not raw_jobs:
        print("[ERROR] No jobs found in jobs.json.")
        sys.exit(1)

    validated = []
    seen_names = set()

    for i, job in enumerate(raw_jobs):
        name    = job.get("name", f"job_{i}")
        project = job.get("project", "UNKNOWN")
        jtype   = job.get("type", "http")

        if name in seen_names:
            log_warn(f"Duplicate job name '{name}' — skipping.")
            continue

        if jtype != "http":
            log_warn(f"Job '{name}' has unknown type '{jtype}' — skipping.")
            continue

        if not job.get("url"):
            log_warn(f"Job '{name}' has no URL — skipping.")
            continue

        has_interval = "interval_seconds" in job
        has_run_at   = "run_at" in job

        if not has_interval and not has_run_at:
            log_warn(f"Job '{name}' has no interval_seconds or run_at — skipping.")
            continue

        if has_run_at:
            try:
                datetime.strptime(job["run_at"], "%H:%M")
            except ValueError:
                log_warn(f"Job '{name}' has invalid run_at '{job['run_at']}' — skipping.")
                continue

        if has_interval:
            interval = job["interval_seconds"]
            if not isinstance(interval, (int, float)) or interval <= 0:
                log_warn(f"Job '{name}' has invalid interval '{interval}' — skipping.")
                continue

        seen_names.add(name)
        validated.append({
            "project":          project,
            "type":             jtype,
            "name":             name,
            "url":              job["url"],
            "run_at":           job.get("run_at"),
            "interval_seconds": float(job["interval_seconds"]) if has_interval else None,
            "headers":          job.get("headers", {}),
            "body":             job.get("body", None),
            "connect_timeout":  job.get("connect_timeout", 5),
            "read_timeout":     job.get("read_timeout", 10),
            "retry_count":      job.get("retry_count", RETRY_COUNT),
            "retry_delay":      job.get("retry_delay", RETRY_DELAY),
        })

    if not validated:
        print("[ERROR] No valid jobs to run. Exiting.")
        sys.exit(1)

    return validated


# ---------------------------------------------------------------------------
# Time helper
# ---------------------------------------------------------------------------
def seconds_until_next(run_at: str) -> float:
    now = datetime.now()
    h, m = map(int, run_at.split(":"))
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


# ---------------------------------------------------------------------------
# HTTP execution
# ---------------------------------------------------------------------------
def http_post_once(job):
    try:
        response = requests.post(
            job["url"],
            json=job["body"] if job["body"] is not None else None,
            headers=job["headers"],
            timeout=(job["connect_timeout"], job["read_timeout"])
        )
        if response.status_code == 429:
            return False, "Rate limited (429)", 429, response.text
        if 200 <= response.status_code < 300:
            return True, None, response.status_code, None
        return False, response.reason, response.status_code, response.text
    except requests.exceptions.ConnectTimeout:
        return False, "Connection timed out", None, None
    except requests.exceptions.ReadTimeout:
        return False, "Read timed out waiting for response", None, None
    except requests.exceptions.ConnectionError as e:
        return False, f"Connection error: {e}", None, None
    except requests.exceptions.RequestException as e:
        return False, f"Request error: {e}", None, None
    except Exception as e:
        return False, f"Unexpected error: {e}", None, None


def run_job(job):
    name        = job["name"]
    project     = job["project"]
    max_retries = job["retry_count"]
    retry_delay = job["retry_delay"]

    for attempt in range(1, max_retries + 1):
        success, reason, status_code, body = http_post_once(job)

        if success:
            log_success(project, name, status_code)
            return

        is_last = attempt == max_retries

        if status_code == 429:
            log_warn(f"[{project}][{name}] Rate limited. Waiting {RATE_LIMIT_WAIT}s (attempt {attempt}/{max_retries})")
            time.sleep(RATE_LIMIT_WAIT)
            continue

        if not is_last:
            log_warn(f"[{project}][{name}] Attempt {attempt}/{max_retries} failed: {reason}. Retrying in {retry_delay}s.")
            time.sleep(retry_delay)
        else:
            log_failure(project, name, reason, status_code, body)


# ---------------------------------------------------------------------------
# Job loops
# ---------------------------------------------------------------------------
def interval_job_loop(job, stop_event):
    interval = job["interval_seconds"]
    log_info(f"[{job['project']}] Job '{job['name']}' running every {interval}s")

    while not stop_event.is_set():
        try:
            run_job(job)
        except Exception as e:
            log_failure(job["project"], job["name"], f"Unhandled thread exception: {e}")
        stop_event.wait(interval)


def timed_job_loop(job, stop_event):
    name    = job["name"]
    project = job["project"]
    run_at  = job["run_at"]
    log_info(f"[{project}] Job '{name}' scheduled daily at {run_at}")

    while not stop_event.is_set():
        wait = seconds_until_next(run_at)
        log_info(f"[{project}] Job '{name}' next run in {int(wait)}s (at {run_at})")
        stop_event.wait(wait)
        if stop_event.is_set():
            break
        try:
            run_job(job)
        except Exception as e:
            log_failure(project, name, f"Unhandled thread exception: {e}")
        stop_event.wait(61)


# ---------------------------------------------------------------------------
# Watchdog
# ---------------------------------------------------------------------------
def watchdog(threads, stop_event):
    while not stop_event.is_set():
        stop_event.wait(WATCHDOG_INTERVAL)
        if stop_event.is_set():
            break
        for name, thread in threads.items():
            if not thread.is_alive():
                msg = f"Thread for job '{name}' died. Restarting app."
                log_warn(msg)
                log_failure("SCHEDULER", name, msg)
                time.sleep(2)
                os.execv(sys.executable, [sys.executable] + sys.argv)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    log_info("Scheduler starting up")
    log_info(f"Base dir : {BASE_DIR}")
    log_info(f"Config   : {CONFIG_FILE}")
    log_info(f"Logs     : {LOG_DIR}")

    log_info(f"Waiting {STARTUP_DELAY}s for network to stabilize...")
    time.sleep(STARTUP_DELAY)

    jobs = load_config()
    log_info(f"Loaded {len(jobs)} job(s)")

    stop_event = threading.Event()
    threads    = {}

    def shutdown(signum, frame):
        log_info("Shutdown signal received. Stopping...")
        stop_event.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    if platform.system() != "Windows":
        def reload_config_signal(signum, frame):
            log_info("SIGHUP received — reloading config and restarting...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        signal.signal(signal.SIGHUP, reload_config_signal)

    for job in jobs:
        target = timed_job_loop if job.get("run_at") else interval_job_loop
        t = threading.Thread(
            target=target,
            args=(job, stop_event),
            name=job["name"],
            daemon=True
        )
        t.start()
        threads[job["name"]] = t

    # Watchdog thread
    threading.Thread(
        target=watchdog,
        args=(threads, stop_event),
        name="watchdog",
        daemon=True
    ).start()

    # Summary thread
    threading.Thread(
        target=summary_loop,
        args=(stop_event,),
        name="summary",
        daemon=True
    ).start()

    stop_event.wait()
    log_info("Scheduler stopped.")


if __name__ == "__main__":
    main()
