# Web transport and network boundary

Outpost's normal topology is an operator-only browser management plane and a community-accessible
Meshtastic service. Mesh participation never requires dashboard access. HTTPS is optional: an
offline Pi on an isolated relief-site LAN or temporary Outpost hotspot must remain deployable
without DNS, internet, a public CA, or certificate setup.

Choose one explicit `web.transport.mode`. Changing modes requires a service restart.

## Trusted local HTTP (field default)

`trusted_http` is supported on an isolated operator LAN, a direct cable, the temporary setup
hotspot, or an encrypted VPN interface whose firewall excludes other interfaces:

```yaml
web:
  bind: "0.0.0.0"
  port: 8080
  auth: {mode: password, session_hours: 12}
  transport:
    mode: trusted_http
    certificate_file: null
    private_key_file: null
    trusted_proxies: []
    public_port: 443
    hsts_seconds: 31536000
```

This mode has application authentication, CSRF protection, and rate limits, but browser traffic is
not encrypted. Anyone able to observe the network can see credentials and operational data. Keep
the dashboard operator-only, isolate it from guest/community networks, and do not route port 8080
from a satellite hotspot or WAN. Operators receive one concise, non-blocking warning in the console;
read-only wallboards are not repeatedly alarmed.

The setup hotspot intentionally uses this mode. It has WPA2, AP client isolation, no forwarding,
an input allowlist, and a 5–60 minute expiry, but it is still bootstrap access rather than a general
network. Stop it after setup. If the configured mode is HTTPS or proxy, switch temporarily to
`trusted_http`, validate and restart, then start the hotspot.

## Direct HTTPS with operator certificates

Use `direct_https` when Outpost itself should terminate TLS. Certificates may come from a private
CA, an offline site PKI, or any other operator-managed source; Outpost never enrolls online or makes
boot depend on a public CA.

```sh
sudo install -d -m 0750 -o root -g outpost /etc/outpost/tls
sudo install -m 0640 -o root -g outpost outpost-fullchain.pem /etc/outpost/tls/fullchain.pem
sudo install -m 0640 -o root -g outpost outpost-key.pem /etc/outpost/tls/key.pem
```

```yaml
web:
  bind: "0.0.0.0"
  port: 8443
  auth: {mode: password, session_hours: 12}
  transport:
    mode: direct_https
    certificate_file: /etc/outpost/tls/fullchain.pem
    private_key_file: /etc/outpost/tls/key.pem
    trusted_proxies: []
    public_port: 443
    hsts_seconds: 31536000
```

Validate as the service account before restarting:

```sh
sudo -u outpost env OUTPOST_CONFIG=/etc/outpost/config.yaml \
  /opt/outpost/current/bin/python -c \
  'from outpost.config import load_config; from outpost.web.transport import uvicorn_options; uvicorn_options(load_config().web); print("TLS configuration valid")'
sudo systemctl restart outpost
curl --cacert site-ca.pem https://OUTPOST-NAME:8443/api/v1/health
```

Startup rejects unreadable, malformed, not-yet-valid, expired, or key-mismatched material. For
rotation, stage both files under temporary names, validate a temporary config that references the
pair, atomically replace both final files, validate the live config, and restart. Existing
connections are interrupted by the restart; the database is unaffected.

If a certificate expires or a rotation fails, use local console access. Restore the previous pair,
or change the mode to `trusted_http` and bind to `127.0.0.1` (or an isolated recovery interface),
validate the config, and restart. A browser that cached HSTS for the old hostname may continue to
require HTTPS; use the recovery IP/alternate local hostname or clear that browser policy. Setting
`hsts_seconds: 0` disables new HSTS headers during a deliberately staged migration.

## HTTPS through an explicitly trusted proxy

Use `trusted_proxy` when Nginx, Caddy, a VPN gateway, or another controlled terminator owns the
certificate. A same-host proxy should keep Outpost on loopback:

```yaml
web:
  bind: "127.0.0.1"
  port: 8080
  auth: {mode: password, session_hours: 12}
  transport:
    mode: trusted_proxy
    certificate_file: null
    private_key_file: null
    trusted_proxies: ["127.0.0.1/32", "::1/128"]
    public_port: 443
    hsts_seconds: 31536000
```

Minimal Nginx location:

```nginx
location / {
    proxy_pass http://127.0.0.1:8080;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

The proxy must overwrite—not append—both forwarded headers. Outpost accepts one protocol and one
client IP only when the TCP peer belongs to `trusted_proxies`. It ignores forwarded values from
every other peer and rejects all-address allowlists such as `0.0.0.0/0`; Uvicorn's independent
proxy-header processing is disabled. If the proxy is on another machine, bind to the management
interface, list only the proxy address, and firewall the backend port to that address. A console
warning remains visible until that network-reachable HTTP backend is intentionally contained.

Certificate validation and rotation occur at the proxy in this mode. Confirm that the browser sees
the new certificate and that `/api/v1/web/transport` reports `request_encrypted: true`.

## Cookies and HSTS

The session cookie is `HttpOnly` and `SameSite=Lax` in every mode. `Secure` and the HSTS response
header are added only when the request is directly TLS or a forwarded HTTPS request arrived from a
configured trusted proxy. Spoofed `X-Forwarded-Proto: https` on a trusted HTTP LAN cannot turn an
HTTP session into an apparently secure one.

## Firewall examples

Adapt interface names and subnets before applying rules, and keep an existing console/SSH recovery
path. These examples do not open a WAN route.

Direct isolated LAN HTTP, limited to the operator subnet:

```sh
sudo ufw allow in on eth0 from 192.168.50.0/24 to any port 8080 proto tcp
sudo ufw deny in to any port 8080 proto tcp
```

Direct HTTPS, with the HTTP port closed:

```sh
sudo ufw allow in on eth0 from 192.168.50.0/24 to any port 8443 proto tcp
sudo ufw deny in to any port 8080 proto tcp
```

Remote reverse proxy at `192.168.50.10`, allowing only its backend connection:

```sh
sudo ufw allow in on eth0 from 192.168.50.10 to any port 8080 proto tcp
sudo ufw deny in to any port 8080 proto tcp
```

WireGuard operator access, with HTTP carried only inside the encrypted overlay:

```sh
sudo ufw allow in on wg0 to any port 8080 proto tcp
sudo ufw deny in to any port 8080 proto tcp
```

The optional Outpost hotspot installs and removes its own narrow nftables table. Do not add a broad
permanent Wi-Fi allow rule for it. Verify effective exposure from a device on each relevant VLAN or
interface, not only from the Pi itself.

## Mode recovery checks

After any change:

```sh
sudo systemctl status outpost --no-pager
sudo journalctl -u outpost -n 50 --no-pager
curl -fsS http://127.0.0.1:8080/api/v1/health        # HTTP or proxy backend
curl -fkSs https://127.0.0.1:8443/api/v1/health      # direct-TLS liveness only
```

The installer and diagnostic bundle select the correct loopback scheme automatically. Their
direct-TLS loopback probe deliberately ignores hostname trust after startup has verified certificate
dates and key pairing; operator/browser trust is still verified with the site CA and real hostname.
