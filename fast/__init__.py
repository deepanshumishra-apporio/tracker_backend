"""Browserless fetchers: plain HTTPS with a real Chrome TLS fingerprint
(curl_cffi), many numbers at a time. Each returns the same TrackingResult the
browser scrapers do, so callers can fall back to a scraper per number."""
