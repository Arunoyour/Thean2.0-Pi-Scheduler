#!/bin/bash
# Thean Scheduler — Install Script
# Usage: bash install.sh

set -e

# ---------------------------------------------------------------------------
# Colours
# ---------------------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Colour

info()  { echo -e "${CYAN}[INFO ]${NC} $1"; }
ok()    { echo -e "${GREEN}[OK   ]${NC} $1"; }
warn()  { echo -e "${YELLOW}[WARN ]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ---------------------------------------------------------------------------
# Detect user and paths
# ---------------------------------------------------------------------------
CURRENT_USER=$(whoami)
HOME_DIR=$(eval echo "~$CURRENT_USER")
DESKTOP_DIR="$HOME_DIR/Desktop"
INSTALL_DIR="$DESKTOP_DIR/Thean_scheduler"
SERVICE_NAME="thean-scheduler"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
REPO_URL="https://github.com/Arunoyour/Thean2.0-Pi-Scheduler"
PYTHON_BIN=""

echo ""
echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}       Thean Scheduler — Installer              ${NC}"
echo -e "${CYAN}================================================${NC}"
echo ""
info "User        : $CURRENT_USER"
info "Home        : $HOME_DIR"
info "Install dir : $INSTALL_DIR"
echo ""

# ---------------------------------------------------------------------------
# Check Desktop folder exists
# ---------------------------------------------------------------------------
if [ ! -d "$DESKTOP_DIR" ]; then
    warn "Desktop folder not found at $DESKTOP_DIR. Creating it."
    mkdir -p "$DESKTOP_DIR"
fi

# ---------------------------------------------------------------------------
# Check git is available
# ---------------------------------------------------------------------------
if ! command -v git &> /dev/null; then
    info "git not found. Installing..."
    sudo apt-get update -qq && sudo apt-get install -y git
    ok "git installed."
fi

# ---------------------------------------------------------------------------
# Stop existing service if running
# ---------------------------------------------------------------------------
if systemctl is-active --quiet "$SERVICE_NAME" 2>/dev/null; then
    info "Stopping existing service..."
    sudo systemctl stop "$SERVICE_NAME"
    ok "Service stopped."
fi

if systemctl is-enabled --quiet "$SERVICE_NAME" 2>/dev/null; then
    info "Disabling existing service..."
    sudo systemctl disable "$SERVICE_NAME"
fi

# ---------------------------------------------------------------------------
# Clone or update repo
# ---------------------------------------------------------------------------
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Repo already exists. Pulling latest changes..."
    git -C "$INSTALL_DIR" pull
    ok "Repo updated."
else
    if [ -d "$INSTALL_DIR" ]; then
        warn "Folder exists but is not a git repo. Removing and cloning fresh."
        rm -rf "$INSTALL_DIR"
    fi
    info "Cloning repo to $INSTALL_DIR ..."
    git clone "$REPO_URL" "$INSTALL_DIR"
    ok "Repo cloned."
fi

# ---------------------------------------------------------------------------
# Detect Python
# ---------------------------------------------------------------------------
if command -v python3 &> /dev/null; then
    PYTHON_BIN=$(command -v python3)
elif command -v python &> /dev/null; then
    PYTHON_BIN=$(command -v python)
else
    error "Python not found. Please install Python 3."
fi

PYTHON_VERSION=$("$PYTHON_BIN" --version 2>&1)
ok "Python found: $PYTHON_VERSION ($PYTHON_BIN)"

# ---------------------------------------------------------------------------
# Install requests library
# Handles: pip3 vs pip, and --break-system-packages for Bookworm OS
# ---------------------------------------------------------------------------
info "Installing Python 'requests' library..."

PIP_BIN=""
if command -v pip3 &> /dev/null; then
    PIP_BIN="pip3"
elif command -v pip &> /dev/null; then
    PIP_BIN="pip"
else
    error "pip not found. Please install pip3."
fi

# Try normal install first, then fall back to --break-system-packages
if ! $PIP_BIN install requests --quiet 2>/dev/null; then
    warn "Standard pip install failed. Trying --break-system-packages (Bookworm)..."
    if ! $PIP_BIN install requests --break-system-packages --quiet 2>/dev/null; then
        error "Failed to install 'requests'. Please run: pip3 install requests"
    fi
fi

ok "'requests' installed."

# ---------------------------------------------------------------------------
# Create logs folder
# ---------------------------------------------------------------------------
mkdir -p "$INSTALL_DIR/logs"
ok "Logs folder ready."

# ---------------------------------------------------------------------------
# Write systemd service file
# ---------------------------------------------------------------------------
info "Writing systemd service file..."

sudo bash -c "cat > $SERVICE_FILE" <<EOF
[Unit]
Description=Thean Scheduler
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$CURRENT_USER
ExecStart=$PYTHON_BIN $INSTALL_DIR/main.py
WorkingDirectory=$INSTALL_DIR
Restart=always
RestartSec=10
MemoryMax=200M

[Install]
WantedBy=multi-user.target
EOF

ok "Service file written to $SERVICE_FILE"

# ---------------------------------------------------------------------------
# Enable and start service
# ---------------------------------------------------------------------------
info "Enabling service..."
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"

info "Starting service..."
sudo systemctl start "$SERVICE_NAME"

# ---------------------------------------------------------------------------
# Wait a moment for service to initialise
# ---------------------------------------------------------------------------
sleep 3

# ---------------------------------------------------------------------------
# Status check
# ---------------------------------------------------------------------------
echo ""
echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}              Service Status                    ${NC}"
echo -e "${CYAN}================================================${NC}"
sudo systemctl status "$SERVICE_NAME" --no-pager
echo ""

# ---------------------------------------------------------------------------
# Show last 100 log entries
# ---------------------------------------------------------------------------
LOG_FILE="$INSTALL_DIR/logs/errors.log"

echo -e "${CYAN}================================================${NC}"
echo -e "${CYAN}           Last 100 Log Entries                 ${NC}"
echo -e "${CYAN}================================================${NC}"

if [ -f "$LOG_FILE" ]; then
    tail -n 100 "$LOG_FILE"
else
    warn "No log file yet. This is normal on first run — logs only appear on job failures."
fi

echo ""
echo -e "${CYAN}================================================${NC}"
echo -e "${GREEN}  Install complete!${NC}"
echo ""
echo -e "  Useful commands:"
echo -e "  ${CYAN}sudo systemctl status $SERVICE_NAME${NC}     — check status"
echo -e "  ${CYAN}sudo systemctl restart $SERVICE_NAME${NC}    — restart"
echo -e "  ${CYAN}sudo systemctl stop $SERVICE_NAME${NC}       — stop"
echo -e "  ${CYAN}tail -f $INSTALL_DIR/logs/errors.log${NC}"
echo -e "                                              — watch live logs"
echo -e "${CYAN}================================================${NC}"
echo ""
