# Thean Scheduler

A lightweight API job scheduler for Raspberry Pi or Docker.
Reads jobs from a JSON config file, POSTs to each URL on a set interval, and logs failures in human-readable format.  
Runs automatically through systemd on a Pi or Docker Compose on a server.

## Hostinger / Docker deployment

The production container owns all Thean and CoCo Cabs schedules. It publishes no
port and joins Thean's private Docker network so cron triggers never traverse the
public internet.

Before starting it, configure Thean with the same random secret and disable its
in-process jobs:

```env
INTERNAL_JOBS_ENABLED=false
CRON_SECRET=<random 32+ character secret>
```

Create `deploy/hostinger/.env` from `.env.example`, using that secret for
`THEAN_CRON_SECRET`, then validate the stack:

```bash
docker compose --env-file deploy/hostinger/.env \
  -f deploy/hostinger/compose.yml config --quiet
```

The stack uses `restart: unless-stopped`, a 256 MB memory limit,
`Asia/Kolkata`, persistent catch-up state, a heartbeat health check, and bounded
Docker logs. Do not install `thean-watchdog.service` on a shared VPS: its Pi-only
policy can reboot the whole host.

---

## Requirements

- Raspberry Pi OS 32-bit (Bullseye or Bookworm)
- Python 3 (pre-installed)
- Internet connection for cloning and API calls

---

## Install

Open a terminal on the Pi and run:

```bash
git clone https://github.com/Arunoyour/Thean2.0-Pi-Scheduler ~/Desktop/Thean_scheduler
cd ~/Desktop/Thean_scheduler
cp .env.example .env
# Edit .env with the reachable Thean API URL and its CRON_SECRET.
bash install.sh
```

The install script will:
- Install dependencies automatically
- Set up the systemd service
- Start the scheduler
- Show service status and recent logs

---

## Configuration

Environment placeholders are expanded before the JSON is parsed. Production uses
them for Thean's private base URL and cron credential:

```json
{
  "project_headers": {
    "THEAN": {"X-Cron-Secret": "${THEAN_CRON_SECRET}"}
  },
  "jobs": [...]
}
```

```json
{
  "jobs": [
    {
      "name": "heartbeat",
      "url": "http://192.168.1.100/api/heartbeat",
      "interval_seconds": 60,
      "body": { "device": "pi", "status": "ok" },
      "connect_timeout": 5,
      "read_timeout": 10,
      "retry_count": 5,
      "retry_delay": 30
    }
  ]
}
```

### Job Fields

| Field | Required | Default | Description |
|-------|----------|---------|-------------|
| `name` | Yes | — | Unique job name (used in logs) |
| `url` | Yes | — | HTTP endpoint to POST to |
| `interval_seconds` | Yes | — | How often to run (seconds) |
| `body` | No | `null` | JSON body to POST (omit for empty body) |
| `headers` | No | `{}` | HTTP headers e.g. `{"Authorization": "Bearer TOKEN"}` |
| `connect_timeout` | No | `5` | Seconds to wait for connection |
| `read_timeout` | No | `10` | Seconds to wait for response |
| `retry_count` | No | `5` | Number of retries before logging failure |
| `retry_delay` | No | `30` | Seconds between retries |
| `run_at` | No | — | Exact daily time to run in `HH:MM` (24h format). Use instead of `interval_seconds` for daily jobs. e.g. `"run_at": "00:00"` fires at midnight every day |
| `project` | No | `UNKNOWN` | Label for filtering logs e.g. `THEAN` or `COCO CABS` |
| `type` | No | `http` | Job type. The current runtime accepts HTTP jobs only. |
| `catch_up` | No | `false` | For daily jobs, run after restart when today's scheduled run was missed. |

To apply config changes without reinstalling:

```bash
sudo systemctl restart thean-scheduler
```

---

## Logs

Failures are written to hourly files under `logs/YYYY-MM-DD/`. Summaries are
written every 30 minutes. Successful requests are silent by default; set
`LOG_SUCCESSES=true` only while diagnosing a problem. Dated log folders older
than `LOG_RETENTION_DAYS` (14 by default) are removed automatically.

### Example Log Entry

```
[2026-06-18 14:32:01]
  JOB    : heartbeat
  URL    : http://192.168.1.100/api/heartbeat
  STATUS : 503
  REASON : Service Unavailable
  BODY   : {"error": "downstream timeout"}
```

### Watch Logs Live

```bash
find ~/Desktop/Thean_scheduler/logs -type f -name '*_error.log' -print0 \
  | xargs -0 tail -F
```

---

## Service Commands

```bash
sudo systemctl status thean-scheduler     # check if running
sudo systemctl restart thean-scheduler    # restart
sudo systemctl stop thean-scheduler       # stop
sudo systemctl disable thean-scheduler    # remove from autostart
```

---

## Reliability Features

| Feature | Behaviour |
|---------|-----------|
| Auto-start | Starts automatically on every boot |
| Auto-restart | systemd restarts the app if it crashes |
| Startup delay | Waits 30s after boot for network to stabilise |
| Retry on failure | Retries up to 5 times with 30s delay between attempts |
| Rate limit (429) | Waits 60s before retrying |
| Thread watchdog | Detects dead job threads and restarts the app |
| Graceful shutdown | Finishes in-flight requests before stopping |
| Log retention | Deletes dated log folders after 14 days by default |
| Daily catch-up | Runs opted-in daily jobs after a restart if today's run was missed |
| Config validation | Validates all jobs on startup, skips invalid entries |
| Disk full handling | Catches write errors without crashing |

---

## Updating

To pull the latest version and reinstall:

```bash
cd ~/Desktop/Thean_scheduler
bash install.sh
```

The script pulls the latest code, updates the service, and restarts automatically.

---

## Folder Structure

```
Thean_scheduler/
├── main.py                  # Scheduler application
├── jobs.json                # Job configuration
├── requirements.txt         # Python dependency pin range
├── Dockerfile               # Production scheduler image
├── install.sh               # Installer script
├── thean-scheduler.service  # Systemd service reference
├── state/                   # Persistent daily-job catch-up state
└── logs/YYYY-MM-DD/         # Hourly failures and daily summaries
```

---

## Estimated Resource Usage

| Resource | Usage |
|----------|-------|
| RAM | Capped at 256 MB in the Docker deployment |
| CPU (idle) | < 1% |
| Disk (logs) | Bounded by 14-day app-log retention and Docker log rotation |

The scheduler is designed to coexist with the application containers on the
2 GB Hostinger VPS.
