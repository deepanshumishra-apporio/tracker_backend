#!/bin/sh
# Start a virtual display for headed Chrome (UC Mode evades anti-bot far better
# headed than headless), then run the app. `exec` so Python is PID 1 and gets
# signals/logs correctly.
set -e

Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/dev/null 2>&1 &
export DISPLAY=:99

# Give Xvfb a moment to come up.
sleep 1

exec python index.py
