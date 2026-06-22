import os
import sys
import json
import time
import logging
import threading
import platform
import signal
from pathlib import Path
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta

import requests

# ---------------------------------------------------------------------------
# Paths — all relative to this script's location
# ---------------------------------------------------------------------------
BASE_DIR    = Path(__file__).parent.resolve()
CONFIG_FILE = BASE_DIR / "jobs.json"
LOG_DIR     = BASE_DIR / "logs"
LOG_FILE    = LOG_DIR  / "errors.log"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
STARTUP_DELAY     = 30
RETRY_COUNT       = 5
RETRY_DELAY       = 30
WATCHDOG_INTERVAL = 30
RATE_LIMIT_WAIT   = 60
LOG_MAX_BYTES     = 5 * 1024 * 1024
LOG_BACKUP_COUNT  = 3


# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------
def setup_logger():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("scheduler")
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

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(fmt)
    logger.addHandler(stdout_handler)

    return logger


logger = setup_logger()


def log_failure(project, job_name, reason, status_code=None, body=None):
    lines = [
        f"  PROJECT : {project}",
        f"  JOB     : {job_name}",
    ]
    if status_code is not None:
        lines.append(f"  STATUS  : {status_code}")
    lines.append(f"  REASON  : {reason}")
    if body:
        lines.append(f"  BODY    : {str(body)[:500]}")
    logger.error("\n".join(lines))


def log_info(msg):
    print(f"[INFO ] {msg}", flush=True)


def log_warn(msg):
    print(f"[WARN ] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Firebase — lazy init, one instance shared across all FCM jobs
# ---------------------------------------------------------------------------
_firebase_initialized = {}
_firebase_lock = threading.Lock()


def get_firebase_app(credential_path: str):
    abs_path = str(BASE_DIR / credential_path)
    with _firebase_lock:
        if abs_path not in _firebase_initialized:
            try:
                import firebase_admin
                from firebase_admin import credentials as fb_credentials
                cred = fb_credentials.Certificate(abs_path)
                app = firebase_admin.initialize_app(cred, name=abs_path)
                _firebase_initialized[abs_path] = app
            except Exception as e:
                raise RuntimeError(f"Firebase init failed for {abs_path}: {e}")
    return _firebase_initialized[abs_path]


def send_fcm(credential_path: str, token: str, title: str, body: str, notification_type: str):
    from firebase_admin import messaging
    app = get_firebase_app(credential_path)
    message = messaging.Message(
        token=token,
        notification=messaging.Notification(title=title, body=body),
        data={"Type": notification_type},
        android=messaging.AndroidConfig(priority="high"),
    )
    return messaging.send(message, app=app)


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

    sql_connection = config.get("sql_connection", "")
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

        if jtype == "http":
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

        elif jtype in ("sp", "fcm"):
            if not job.get("procedure"):
                log_warn(f"Job '{name}' has no procedure — skipping.")
                continue
            interval = job.get("interval_seconds", 60)
            if not isinstance(interval, (int, float)) or interval <= 0:
                log_warn(f"Job '{name}' has invalid interval '{interval}' — skipping.")
                continue
            if jtype == "fcm" and not job.get("firebase_credential"):
                log_warn(f"Job '{name}' has no firebase_credential — skipping.")
                continue
        else:
            log_warn(f"Job '{name}' has unknown type '{jtype}' — skipping.")
            continue

        seen_names.add(name)
        validated.append({
            "project":            project,
            "type":               jtype,
            "name":               name,
            # http fields
            "url":                job.get("url"),
            "run_at":             job.get("run_at"),
            "interval_seconds":   float(job.get("interval_seconds", 60)) if jtype != "http" or "interval_seconds" in job else None,
            "headers":            job.get("headers", {}),
            "body":               job.get("body", None),
            "connect_timeout":    job.get("connect_timeout", 5),
            "read_timeout":       job.get("read_timeout", 10),
            # sp / fcm fields
            "procedure":          job.get("procedure"),
            "has_output_params":  job.get("has_output_params", True),
            "firebase_credential":job.get("firebase_credential"),
            # shared
            "retry_count":        job.get("retry_count", RETRY_COUNT),
            "retry_delay":        job.get("retry_delay", RETRY_DELAY),
            # runtime
            "_sql_connection":    sql_connection,
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


def run_http_job(job):
    name        = job["name"]
    project     = job["project"]
    max_retries = job["retry_count"]
    retry_delay = job["retry_delay"]

    for attempt in range(1, max_retries + 1):
        success, reason, status_code, body = http_post_once(job)
        if success:
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
# SP execution
# ---------------------------------------------------------------------------
def run_sp_job(job):
    import pymssql
    name             = job["name"]
    project          = job["project"]
    procedure        = job["procedure"]
    has_output       = job["has_output_params"]
    conn_str         = job["_sql_connection"]
    max_retries      = job["retry_count"]
    retry_delay      = job["retry_delay"]

    for attempt in range(1, max_retries + 1):
        try:
            conn   = pymssql.connect(conn_str)
            cursor = conn.cursor()

            if has_output:
                cursor.callproc(procedure)
                row = cursor.fetchone()
                conn.close()

                if row is None:
                    return  # no output — assume success

                status_cd   = bool(row[0]) if row[0] is not None else True
                status_desc = str(row[1]) if len(row) > 1 and row[1] is not None else ""

                if status_cd:
                    return  # success
                # SP returned failure
                if attempt < max_retries:
                    log_warn(f"[{project}][{name}] Attempt {attempt}/{max_retries} SP failure: {status_desc}. Retrying in {retry_delay}s.")
                    time.sleep(retry_delay)
                else:
                    log_failure(project, name, f"SP returned failure: {status_desc}")
            else:
                cursor.callproc(procedure)
                conn.commit()
                conn.close()
                return  # no output params — assume success if no exception

        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            if attempt < max_retries:
                log_warn(f"[{project}][{name}] Attempt {attempt}/{max_retries} exception: {e}. Retrying in {retry_delay}s.")
                time.sleep(retry_delay)
            else:
                log_failure(project, name, f"Exception after {max_retries} retries: {e}")


# ---------------------------------------------------------------------------
# FCM execution
# ---------------------------------------------------------------------------
def run_fcm_job(job):
    import pymssql
    name        = job["name"]
    project     = job["project"]
    procedure   = job["procedure"]
    credential  = job["firebase_credential"]
    conn_str    = job["_sql_connection"]
    max_retries = job["retry_count"]
    retry_delay = job["retry_delay"]

    for attempt in range(1, max_retries + 1):
        try:
            conn   = pymssql.connect(conn_str)
            cursor = conn.cursor(as_dict=True)
            cursor.callproc(procedure)
            rows = cursor.fetchall()
            conn.close()

            for row in rows:
                token    = row.get("Token", "") or ""
                title    = row.get("MessageType", "") or ""
                message  = row.get("Message", "") or ""
                msg_type = ""

                if not token:
                    log_failure(project, name, "FCM skipped — empty token", body=str(row))
                    continue

                try:
                    send_fcm(credential, token, title, message, msg_type)
                    log_info(f"[{project}][{name}] FCM sent | {token[:20]}... | {title}")
                except Exception as ex:
                    log_failure(project, name, f"FCM send failed for token {token[:20]}...: {ex}")

            return  # done regardless of individual FCM failures

        except Exception as e:
            try:
                conn.close()
            except Exception:
                pass
            if attempt < max_retries:
                log_warn(f"[{project}][{name}] Attempt {attempt}/{max_retries} DB error: {e}. Retrying in {retry_delay}s.")
                time.sleep(retry_delay)
            else:
                log_failure(project, name, f"DB query failed after {max_retries} retries: {e}")


# ---------------------------------------------------------------------------
# Job dispatcher
# ---------------------------------------------------------------------------
def run_job(job):
    jtype = job["type"]
    if jtype == "http":
        run_http_job(job)
    elif jtype == "sp":
        run_sp_job(job)
    elif jtype == "fcm":
        run_fcm_job(job)


# ---------------------------------------------------------------------------
# Interval-based job loop
# ---------------------------------------------------------------------------
def interval_job_loop(job, stop_event):
    interval = job["interval_seconds"]
    name     = job["name"]
    project  = job["project"]
    log_info(f"[{project}] Job '{name}' running every {interval}s")

    while not stop_event.is_set():
        try:
            run_job(job)
        except Exception as e:
            log_failure(job["project"], name, f"Unhandled thread exception: {e}")
        stop_event.wait(interval)


# ---------------------------------------------------------------------------
# Clock-time job loop (exact HH:MM daily)
# ---------------------------------------------------------------------------
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
                logger.error(f"  JOB    : {name}\n  REASON : {msg}")
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
        if job["type"] == "http" and job.get("run_at"):
            target = timed_job_loop
        else:
            target = interval_job_loop

        t = threading.Thread(
            target=target,
            args=(job, stop_event),
            name=job["name"],
            daemon=True
        )
        t.start()
        threads[job["name"]] = t

    wd = threading.Thread(
        target=watchdog,
        args=(threads, stop_event),
        name="watchdog",
        daemon=True
    )
    wd.start()

    stop_event.wait()
    log_info("Scheduler stopped.")


if __name__ == "__main__":
    main()
