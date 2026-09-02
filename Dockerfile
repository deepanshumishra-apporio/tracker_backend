# Multi-carrier tracker backend — single container.
#
# SeleniumBase UC Mode launches a REAL Google Chrome *inside this container*
# (not a remote browser container). To beat Akamai/DataDome/Cloudflare the
# browser runs HEADED inside a virtual display (Xvfb) — so both Chrome and Xvfb
# must live here alongside the Python code.
#
# Build:  docker build -t tracker-backend .
# Run:    docker run --rm -p 8000:8000 --env-file deploy/tracker.env tracker-backend
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# --- System deps: Google Chrome + Xvfb + fonts (mirrors deploy/setup.sh) ------
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates fonts-liberation \
        xvfb xauth python3-tk \
    && wget -q -O /tmp/chrome.deb \
        https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*

# --- gost: local proxy forwarder that injects Scrape.do's residential proxy ---
# auth for Chrome (which can't send the "super=true" proxy password itself).
RUN wget -q -O /tmp/gost.gz \
        https://github.com/ginuerzh/gost/releases/download/v2.11.5/gost-linux-amd64-2.11.5.gz \
    && gunzip /tmp/gost.gz \
    && mv /tmp/gost /usr/local/bin/gost \
    && chmod +x /usr/local/bin/gost

WORKDIR /app

# Install Python deps first for better layer caching.
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# App source.
COPY . .

# Chrome inside a container must run headed-in-Xvfb, never real headless.
# base.py already passes --no-sandbox / --disable-dev-shm-usage.
ENV HOST=0.0.0.0 \
    PORT=8000 \
    HEADLESS=false \
    USE_XVFB=true \
    SOLVE_CAPTCHA=false

EXPOSE 8000

# entrypoint.sh starts the gost forwarder (if using Scrape.do proxy mode),
# then launches index.py (FastAPI).
RUN chmod +x /app/entrypoint.sh
CMD ["/app/entrypoint.sh"]
