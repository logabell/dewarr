# MAM proxy setup and diagnostics

In **Settings → Sources → MAM**, the **Proxy** and **MAM account** sections
appear first, with separate test buttons and results. **Advanced settings**
contains the MAM URL, proxy credentials, and direct-fallback preference;
**Account automation** follows below.

Enter a proxy origin such as `http://gluetun:8888` and select **Test proxy**.
Dewarr's backend container must be able to resolve and connect to that address.
This checks the proxy public IP and direct server public IP without contacting
MAM or testing its cookie, even when `mam_id` is already saved. With no proxy
configured, the button reads **Test network** and checks the direct IP only.

Enter `mam_id` and select **Test mam_id** to verify the account through the
configured route. This does not rerun the IP checks. Proxy results remain
visible when a cookie is entered or rejected. Each test saves pending settings
first; **Save connection** saves without testing.

IP probes never send the MAM cookie or substitute a direct request for a failed
proxy request. Account requests follow **Allow direct fallback when the proxy
is unavailable** in **Advanced settings**. Turn that option off when MAM must
only use the proxy route.

Errors appear once next to their test, with a short suggested fix. Expand
**Error details** for the full diagnostic message.

IP checks try three services through the selected route. Failures distinguish
hostname resolution, connection refusal, HTTPS tunnel rejection, timeouts, TLS
negotiation, and invalid IP responses. Raw exception messages, cookies, and
proxy credentials are not returned to the browser.

## Gluetun works in MouseSearch but Dewarr cannot resolve it

If Dewarr reports **The proxy hostname could not be resolved**, check the
services' network membership first. A service with `networks: [dewarr-net]`
does not also join the Compose default network. Gluetun and MouseSearch join
that default network when they have no explicit `networks` setting, so
MouseSearch can reach `gluetun:8888` while Dewarr cannot.

For services in the same Compose stack, add `default` to Dewarr's existing
network list. Merge these entries into your stack, keeping its other settings:

```yaml
services:
  dewarr:
    networks:
      - dewarr-net
      - default

networks:
  dewarr-net:
    driver: bridge
  default: {}
```

Apply the change from the stack directory:

```sh
docker compose up -d --no-deps dewarr
```

This recreates Dewarr with access to both its database network and the network
where Gluetun is already running. Gluetun, MouseSearch, and qBittorrent can keep
their existing settings, including qBittorrent's `network_mode: service:gluetun`.
Port 8888 does not need a host port mapping for communication on a shared
Docker network. For separate Compose projects, attach Dewarr and Gluetun to
the same external network instead; each project's `default` network is distinct.
See [Docker's Compose networking documentation](https://docs.docker.com/compose/how-tos/networking/).

Keep the proxy URL set to `http://gluetun:8888` and turn off **Allow direct
fallback when the proxy is unavailable** for proxy-only MAM access. Select
**Test proxy** again. A successful proxy IP lookup
sets **Proxy health** to healthy even if the MAM cookie is missing or rejected.
**Direct server IP** is a separate diagnostic; displaying it does not mean MAM
requests used the direct route. The proxy and account badges
report their own checks, so a rejected cookie does not mark the proxy unhealthy.

A proxy DNS failure does not verify the cookie. A separate **Test mam_id**
attempt also fails until MAM can be contacted through the required route. Fix network access before replacing the
cookie. These Compose changes address hostname resolution; the next test
determines whether the VPN egress and MAM authentication also succeed.

## MouseSearch comparison

Reviewed [MouseSearch's implementation at b42b0b9](https://github.com/sevenlayercookie/MouseSearch/blob/b42b0b98d1b81145f6bc6410407d03ae48fb100e/app.py).
Both applications use HTTPX's `proxy=` support for HTTP proxy connections to
HTTPS destinations. MouseSearch's public-IP endpoint operates independently
of MAM authentication, tries three IP services, and accepts plain text or JSON
IP responses. It disables direct fallback when explicitly checking the MAM
proxy route.

Dewarr previously required a cookie when saving a new connection and disabled
the test button until one was supplied. It also hid IP lookup exceptions behind
a generic failure message. The updated flow removes that setup dependency,
adds the third IP service and JSON response support, and provides safe error
details shared with MAM requests. Existing explicit proxy routing, TLS
verification, cookie rotation, and the direct-fallback preference are retained.

Local regression coverage includes real HTTP CONNECT tunnels with verified
TLS, both authenticated and unauthenticated proxies, credential separation,
cookie-free setup, later cookie authentication, failed proxies, and browser
settings behavior. This verifies the transport and setup flow; it does not
establish the cause of a failure in a separate deployed Docker stack.
