#!/bin/bash
# Thean Scheduler Watchdog
# - Checks scheduler health every 60s; reboots after 5 consecutive failures
# - Reboots daily at 02:00

SERVICE_NAME="thean-scheduler"
CHECK_INTERVAL=60       # seconds between health checks
FAILURE_THRESHOLD=5     # consecutive failures before reboot
REBOOT_HOUR=2           # 24h hour for daily reboot

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${CYAN}[$(date '+%Y-%m-%d %H:%M:%S')] [INFO ]${NC} $1"; }
ok()    { echo -e "${GREEN}[$(date '+%Y-%m-%d %H:%M:%S')] [OK   ]${NC} $1"; }
warn()  { echo -e "${YELLOW}[$(date '+%Y-%m-%d %H:%M:%S')] [WARN ]${NC} $1"; }
error() { echo -e "${RED}[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR]${NC} $1"; }

consecutive_failures=0
daily_reboot_done=""

info "Watchdog started. Monitoring '$SERVICE_NAME'."

while true; do
    now_hour=$(date '+%H')
    now_date=$(date '+%Y-%m-%d')

    # Daily 2 AM reboot
    if [ "$now_hour" = "$(printf '%02d' $REBOOT_HOUR)" ] && [ "$daily_reboot_done" != "$now_date" ]; then
        daily_reboot_done="$now_date"
        info "Daily 2 AM reboot triggered."
        sleep 5
        sudo reboot
    fi

    # Health check
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        if [ "$consecutive_failures" -gt 0 ]; then
            ok "Service recovered after $consecutive_failures failure(s)."
        fi
        consecutive_failures=0
    else
        consecutive_failures=$((consecutive_failures + 1))
        warn "Service not running. Consecutive failures: $consecutive_failures / $FAILURE_THRESHOLD"

        if [ "$consecutive_failures" -ge "$FAILURE_THRESHOLD" ]; then
            error "Service failed $FAILURE_THRESHOLD consecutive checks. Rebooting Pi..."
            sleep 5
            sudo reboot
        fi
    fi

    sleep $CHECK_INTERVAL
done
