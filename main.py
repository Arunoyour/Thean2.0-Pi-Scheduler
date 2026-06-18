import os
import sys
import json
import time
import logging
import threading
import platform
import signal
import requests
from pathlib import Path
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------------------
# Paths — all relative to this script's location
# ---------------------------------------------------------------------------
BASE_DIR    = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "jobs.json"
LOG_DIR     = BASE_DIR / "logs"
LOG_FILE    = LOG_DIR  / "errors.log"

# ---------------------------------------------------------------------------
# Defaults (overridable per-job in jobs.json)
# ---------------------------------------------------------------------------
STARTUP_DELAY     = 30   # seconds to wait after boot before first run
RETRY_COUNT       = 5    # default retry attempts per job
RETRY_DELAY       = 30   # seconds between retries
WATCHDOG_INTERVAL = 30   # seconds between watchdog checks
RATE_LIMIT_WAIT   = 60   # seconds to wait on 429 before retry
LOG_MAX_BYTES     = 5 * 1024 * 1024  # 5 MB per log file
LOG_BACKUP_COUNT  = 3    # keep 3 rotated files


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
def setup_logger():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("thean_scheduler")
    logger.setLevel(logging.ERROR)

    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8"
    )
    fmt = logging.Formatter("[%(asctime)s]\n%(message)s\n", datefmt="%Y-%m-%d %H:%M:%S")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Mirror to stdout so systemd journal captures it too
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    logger.addHandler(stdout_handler)

    return logger


logger = setup_logger()


def log_failure(job_name, url, reason, status_code=None, body=None):
    lines = [
        f"  JOB    : {job_name}",
        f"  URL    : {url}",
    ]
    if status_code is not None:
        lines.append(f"  STATUS : {status_code}")
    lines.append(f"  REASON : {reason}")
    if body:
        lines.append(f"  BODY   : {str(body)[:500]}")
    logger.error("\n".join(lines))


def log_info(msg):
    print(f"[INFO ] {msg}", flush=True)


def log_warn(msg):
    print(f"[WARN ] {msg}", flush=True)


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
        name = job.get("name", f"job_{i}")

        if not job.get("url"):
            log_warn(f"Job '{name}' has no URL — skipping.")
            continue

        if name in seen_names:
            log_warn(f"Duplicate job name '{name}' — skipping duplicate.")
            continue

        interval = job.get("interval_seconds", 60)
        if not isinstance(interval, (int, float)) or interval <= 0:
            log_warn(f"Job '{name}' has invalid interval '{interval}' — skipping.")
            continue

        seen_names.add(name)
        validated.append({
            "name":            name,
            "url":             job["url"],
            "interval_seconds": float(interval),
            "headers":         job.get("headers", {}),
            "body":            job.get("body", None),
            "connect_timeout": job.get("connect_timeout", 5),
            "read_timeout":    job.get("read_timeout", 10),
            "retry_count":     job.get("retry_count", RETRY_COUNT),
            "retry_delay":     job.get("retry_delay", RETRY_DELAY),
        })

    if not validated:
        print("[ERROR] No valid jobs to run. Exiting.")
        sys.exit(1)

    return validated


# ---------------------------------------------------------------------------
# Single POST attempt
# ---------------------------------------------------------------------------
def post_once(job):
    """Returns (success: bool, reason: str, status_code: int|None, body: str|None)"""
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


# ---------------------------------------------------------------------------
# Job runner (retries included)
# ---------------------------------------------------------------------------
def run_job(job):
    name          = job["name"]
    url           = job["url"]
    max_retries   = job["retry_count"]
    retry_delay   = job["retry_delay"]

    for attempt in range(1, max_retries + 1):
        success, reason, status_code, body = post_once(job)

        if success:
            return  # silent on success

        is_last = attempt == max_retries

        if status_code == 429:
            log_warn(f"[{name}] Rate limited. Waiting {RATE_LIMIT_WAIT}s (attempt {attempt}/{max_retries})")
            time.sleep(RATE_LIMIT_WAIT)
            continue

        if not is_last:
            log_warn(f"[{name}] Attempt {attempt}/{max_retries} failed: {reason}. Retrying in {retry_delay}s.")
            time.sleep(retry_delay)
        else:
            log_failure(name, url, reason, status_code, body)


# ---------------------------------------------------------------------------
# Job loop — runs in its own thread
# ---------------------------------------------------------------------------
def job_loop(job, stop_event):
    interval = job["interval_seconds"]
    name     = job["name"]
    log_info(f"Job '{name}' running every {interval}s")

    while not stop_event.is_set():
        try:
            run_job(job)
        except Exception as e:
            log_failure(name, job["url"], f"Unhandled thread exception: {e}")

        stop_event.wait(interval)


# ---------------------------------------------------------------------------
# Watchdog — restarts the entire app if any job thread dies
# ---------------------------------------------------------------------------
def watchdog(threads, stop_event):
    while not stop_event.is_set():
        stop_event.wait(WATCHDOG_INTERVAL)
        if stop_event.is_set():
            break

        for name, thread in threads.items():
            if not thread.is_alive():
                msg = f"Thread for job '{name}' died unexpectedly. Restarting app."
                log_warn(msg)
                logger.error(f"  JOB    : {name}\n  REASON : {msg}")
                time.sleep(2)
                os.execv(sys.executable, [sys.executable] + sys.argv)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    log_info("Thean Scheduler starting up")
    log_info(f"Base dir : {BASE_DIR}")
    log_info(f"Config   : {CONFIG_FILE}")
    log_info(f"Logs     : {LOG_DIR}")

    log_info(f"Waiting {STARTUP_DELAY}s for network to stabilize...")
    time.sleep(STARTUP_DELAY)

    jobs = load_config()
    log_info(f"Loaded {len(jobs)} job(s)")

    stop_event = threading.Event()
    threads    = {}

    # Graceful shutdown on SIGTERM / Ctrl+C
    def shutdown(signum, frame):
        log_info("Shutdown signal received. Stopping...")
        stop_event.set()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT,  shutdown)

    # Config reload on SIGHUP (Linux only)
    if platform.system() != "Windows":
        def reload_config_signal(signum, frame):
            log_info("SIGHUP received — reloading config and restarting...")
            os.execv(sys.executable, [sys.executable] + sys.argv)
        signal.signal(signal.SIGHUP, reload_config_signal)

    # Start one thread per job
    for job in jobs:
        t = threading.Thread(
            target=job_loop,
            args=(job, stop_event),
            name=job["name"],
            daemon=True
        )
        t.start()
        threads[job["name"]] = t

    # Start watchdog
    wd = threading.Thread(
        target=watchdog,
        args=(threads, stop_event),
        name="watchdog",
        daemon=True
    )
    wd.start()

    stop_event.wait()
    log_info("Thean Scheduler stopped.")


if __name__ == "__main__":
    main()
