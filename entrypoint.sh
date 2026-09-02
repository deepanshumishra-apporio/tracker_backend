#!/bin/sh
# Container entrypoint.
#
# FedEx is Akamai-protected and blocks datacenter IPs. The winning combo is our
# real UC Mode browser (passes Akamai's bot sensor) + a RESIDENTIAL exit IP.
#
# When only a Scrape.do token is configured (no explicit residential PROXY_URL),
# we route the browser through Scrape.do proxy mode. Chrome can't send Scrape.do's
# "super=true" proxy password via its auth prompt (and UC Mode avoids auth
# extensions for stealth), so we run a local `gost` forwarder that injects the
# credentials: Chrome -> 127.0.0.1:8899 (no auth) -> proxy.scrape.do (super=true).
set -e

USE_PROXIES_LC=$(printf '%s' "${USE_PROXIES:-false}" | tr '[:upper:]' '[:lower:]')

if [ -n "$SCRAPEDO_TOKEN" ] && [ "$USE_PROXIES_LC" != "true" ]; then
  echo "[entrypoint] starting gost forwarder :8899 -> proxy.scrape.do (super=true residential)"
  gost -L=http://:8899 \
       -F="http://${SCRAPEDO_TOKEN}:super=true@proxy.scrape.do:8080" \
       > /tmp/gost.log 2>&1 &
fi

exec python index.py
