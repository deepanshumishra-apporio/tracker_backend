# Office proxy: give the VM a non-datacenter exit IP

FedEx (Akamai) blocks the Azure VM because its IP belongs to a datacenter. A
proxy on any other cloud host has the same problem. What gets through is a
machine on an ordinary office or home broadband line, so the browser on the VM
sends its traffic out through that machine:

```
Azure VM (UC Chrome) --Tailscale--> office machine (gost :8080) --> fedex.com
                                     office broadband IP
```

Tailscale is a private network between the two machines: nothing is opened on
the office router, and the proxy is reachable only from your own devices, so it
needs no password. That matters because UC Mode Chrome cannot answer a proxy
password prompt (the same reason entrypoint.sh puts gost in front of Scrape.do).

## 1. Office machine (always on, on the office network)

Install Tailscale (https://tailscale.com/download) and sign in. Note its
Tailscale IP:

```
tailscale ip -4        # e.g. 100.101.102.103
```

Install gost v2 (https://github.com/ginuerzh/gost/releases, the same tool the
backend already uses) and start an HTTP proxy bound to the Tailscale IP only:

```
gost -L=http://100.101.102.103:8080
```

Binding to the Tailscale IP (not 0.0.0.0) keeps the proxy off the office LAN
and the internet. Keep it running across reboots:

- Windows: Task Scheduler -> "At startup" task running the command above
  (or wrap it with NSSM as a service). Allow gost.exe through the Windows
  firewall for the Tailscale network when prompted.
- Linux: a systemd unit with `ExecStart=/usr/local/bin/gost -L=http://<ts-ip>:8080`
  and `Restart=always`.

Also turn off sleep on this machine; when it sleeps, FedEx lookups fail.

## 2. Azure VM

Install Tailscale on the VM and sign in to the same account:

```
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

Check the proxy is reachable and exits from the office IP:

```
curl -x http://100.101.102.103:8080 https://api.ipify.org   # office IP
curl https://api.ipify.org                                   # Azure IP
```

The two must differ. Then in `/etc/tracker.env`:

```
USE_PROXIES=true
PROXY_URL=100.101.102.103:8080
SCRAPEDO_TOKEN=
```

and restart:

```
sudo systemctl restart tracker
```

`PROXY_URL` has no `http://` prefix (SeleniumBase format). It applies to every
carrier that drives the browser (FedEx, DHL); Aramex and UPS go over plain HTTP
and are unaffected.

## Staying unblocked

One office IP is still one IP. Akamai rate-limits it if it looks busy:

- Keep `BATCH_CONCURRENCY=1` and the default `MIN_DELAY`/`MAX_DELAY` (5-20s).
- Don't run big FedEx batches back to back; spread them out.
- If it starts failing, more exit IPs help: run the same gost + Tailscale setup
  on a second machine on a different broadband line and switch `PROXY_URL`.
