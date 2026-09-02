#!/usr/bin/env bash
#
# Redeploy the backend after a code change.
#
# Usage (on the VM, from the repo's backend/ directory):
#     bash deploy/update.sh
#
# Why this exists rather than "git pull && systemctl restart": a release that
# adds a Python dependency will import-crash on restart if the venv was not
# updated first, which takes the WHOLE API down, not just the new endpoints.
# This pulls, installs, verifies the app can be imported, and only then
# restarts — and if the service fails to come up it rolls back to the previous
# commit so the box is never left serving nothing.
set -euo pipefail

SERVICE="${SERVICE:-tracker}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8000/api/health}"

BACKEND_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$BACKEND_DIR"

VENV_PY="$BACKEND_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "!! No venv at $BACKEND_DIR/.venv — run 'bash deploy/setup.sh' first." >&2
    exit 1
fi

PREVIOUS="$(git rev-parse HEAD)"
echo "==> Current commit: $PREVIOUS"

echo "==> Pulling…"
git pull --ff-only

NEW="$(git rev-parse HEAD)"
if [ "$NEW" = "$PREVIOUS" ]; then
    echo "==> Already up to date. Nothing to deploy."
    exit 0
fi
echo "==> New commit: $NEW"

# Always run this: a release may have added a dependency, and pip is a no-op
# when everything is already satisfied.
echo "==> Installing dependencies…"
"$BACKEND_DIR/.venv/bin/pip" install -q -r requirements.txt

# Catch an import-time break (missing dep, syntax error) BEFORE we take the
# running service down.
echo "==> Verifying the app imports…"
if ! "$VENV_PY" -c "import index; index.create_app()" >/dev/null; then
    echo "!! The new code fails to import. Rolling back to $PREVIOUS." >&2
    git reset --hard "$PREVIOUS"
    "$BACKEND_DIR/.venv/bin/pip" install -q -r requirements.txt
    echo "!! Service left running on the previous version." >&2
    exit 1
fi

echo "==> Restarting $SERVICE…"
sudo systemctl restart "$SERVICE"

# Give it a moment to bind, then confirm it actually answers.
echo "==> Waiting for health…"
for i in $(seq 1 20); do
    if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
        echo "==> Healthy after ${i}s."
        echo
        echo "Deployed $PREVIOUS -> $NEW"
        git --no-pager log --oneline "$PREVIOUS..$NEW" | sed 's/^/    /'
        exit 0
    fi
    sleep 1
done

echo "!! $SERVICE did not become healthy within 20s. Rolling back." >&2
git reset --hard "$PREVIOUS"
"$BACKEND_DIR/.venv/bin/pip" install -q -r requirements.txt
sudo systemctl restart "$SERVICE"
echo "!! Rolled back to $PREVIOUS. Recent logs:" >&2
sudo journalctl -u "$SERVICE" -n 40 --no-pager >&2
exit 1
