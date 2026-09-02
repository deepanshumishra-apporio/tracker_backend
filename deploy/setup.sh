#!/usr/bin/env bash
#
# One-time provisioning for an Ubuntu Azure VM (22.04 / 24.04 LTS).
# Installs Google Chrome + Xvfb + a Python venv with the backend deps.
#
# Usage (on the VM, from the repo's backend/ directory):
#     bash deploy/setup.sh
#
# After this, configure deploy/tracker.env and install the systemd service
# (see the "Deploy on an Azure VM" section of README.md).
set -euo pipefail

echo "==> Installing system packages (Chrome deps, Xvfb, Python)…"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates fonts-liberation \
    xvfb xauth python3-tk \
    python3 python3-venv python3-pip

# Google Chrome stable — SeleniumBase UC Mode drives a REAL Chrome (not
# Chromium). SeleniumBase auto-manages the matching chromedriver itself.
if ! command -v google-chrome >/dev/null 2>&1; then
    echo "==> Installing Google Chrome stable…"
    wget -q -O /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    sudo apt-get install -y --no-install-recommends /tmp/chrome.deb
    rm -f /tmp/chrome.deb
else
    echo "==> Google Chrome already installed: $(google-chrome --version)"
fi

# Python venv + backend dependencies. Run from backend/ (the dir above deploy/).
BACKEND_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BACKEND_DIR"

echo "==> Creating Python venv at $BACKEND_DIR/.venv…"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

echo
echo "==> Done."
echo "    Chrome:  $(google-chrome --version)"
echo "    Python:  $(./.venv/bin/python --version)"
echo
echo "Next:"
echo "  1) cp deploy/tracker.env.example deploy/tracker.env   # then set PROXY_URL"
echo "  2) Install the systemd service (see README 'Deploy on an Azure VM')."
