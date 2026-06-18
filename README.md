# Thean Scheduler

A lightweight API job scheduler for Raspberry Pi 3 (32-bit).  
Reads jobs from a JSON config file, POSTs to each URL on a set interval, and logs failures in human-readable format.  
Runs automatically on boot via systemd.

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
bash install.sh
```

The install script will:
- Install dependencies automatically
- Set up the systemd service
- Start the scheduler
- Show service status and recent logs

---

## Configuration

Edit `jobs.json` to define your jobs:

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

To apply config changes without reinstalling:

```bash
sudo systemctl restart thean-scheduler
```

---

## Logs

Failures are written to `logs/errors.log` next to `main.py`.  
Log files rotate automatically at 5MB (3 backups kept).  
Successful requests are silent — nothing is logged.

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
tail -f ~/Desktop/Thean_scheduler/logs/errors.log
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
| Log rotation | Caps log file at 5MB, keeps 3 backups |
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
├── install.sh               # Installer script
├── thean-scheduler.service  # Systemd service reference
└── logs/
    └── errors.log           # Failure log
```

---

## Estimated Resource Usage

| Resource | Usage |
|----------|-------|
| RAM (10 jobs) | ~220 MB |
| CPU (idle) | < 1% |
| Disk (logs) | Max 15 MB (3 × 5MB files) |

Well within the Raspberry Pi 3's 1GB RAM limit.
