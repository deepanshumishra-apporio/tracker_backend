# Backend image: Python + Google Chrome (SeleniumBase UC Mode needs a real Chrome).
FROM python:3.12-slim

# System deps + Google Chrome stable (apt resolves Chrome's own libs).
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates fonts-liberation xvfb \
    && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
    && apt-get install -y --no-install-recommends /tmp/chrome.deb \
    && rm -f /tmp/chrome.deb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Headless is required on a server (no display). PORT is provided by the host.
ENV HEADLESS=true \
    PYTHONUNBUFFERED=1

# index.py binds 0.0.0.0:$PORT.
CMD ["python", "index.py"]
