# MAM proxy setup and diagnostics

In **Settings → Sources → MAM**, enter your proxy origin under **Proxy options**,
for example `http://gluetun:8888`. If required, enter the proxy username and
password in their separate fields. Dewarr's backend container must be able to
resolve and connect to that address.

You can leave `mam_id` empty and select **Save & test network**. This saves the
route and checks the proxy public IP and direct server public IP independently.
No MAM account request is made until a cookie is provided. A working network
without a cookie shows healthy proxy status, `not-configured` cookie status,
and an overall `degraded` status because MAM access is not yet verified.

Then enter `mam_id` and select **Save & test connection** to verify the account
through the configured route. IP probes never send the MAM cookie or substitute
a direct request for a failed proxy request. Account requests follow the saved
**Allow direct fallback when the proxy is unavailable** preference. Turn that
option off when MAM must only use the proxy route.

IP checks try three services through the selected route. Failures distinguish
hostname resolution, connection refusal, HTTPS tunnel rejection, timeouts, TLS
negotiation, and invalid IP responses. Raw exception messages, cookies, and
proxy credentials are not returned to the browser.

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
