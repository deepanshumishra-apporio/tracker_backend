# Backend image: Python + Google Chrome (SeleniumBase UC Mode needs a real Chrome).
FROM python:3.12-slim

# System deps + Google Chrome stable (apt resolves Chrome's own libs).
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates fonts-liberation xvfb xauth python3-tk \
    && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
# Normalize line endings (Windows checkouts may be CRLF) and make executable.
RUN sed -i 's/\r$//' /app/entrypoint.sh && chmod +x /app/entrypoint.sh

# Run headed inside a virtual display — UC Mode is far harder for anti-bot to
# detect than headless (needed for Akamai-protected FedEx). The whole process is
# wrapped in `xvfb-run`, which provides a real $DISPLAY for headed Chrome.
# SOLVE_CAPTCHA off: GUI captcha-clicking needs pyautogui/tkinter; UC Mode's
# reconnect handles most Cloudflare challenges on its own.
ENV HEADLESS=false \
    USE_XVFB=false \
    SOLVE_CAPTCHA=false \
    PYTHONUNBUFFERED=1

# entrypoint.sh starts Xvfb, exports $DISPLAY, then runs the app (binds 0.0.0.0:$PORT).
CMD ["/app/entrypoint.sh"]
