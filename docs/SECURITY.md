# Security and privacy

Outpost handles identities, messages, positions, incidents, and operational state. Deploy it as a
trusted local service, not an anonymous public web application.

## Baseline

- Maintain the OS, Python, Meshtastic firmware, and Outpost revision.
- Complete the short-lived local setup-token flow, set a permanent dashboard password, and create
  a named account for each person who operates the node.
- Enable TOTP for administrator accounts and store their one-use recovery codes offline.
- Limit the dashboard to the operator LAN, VPN, or authenticated TLS proxy; keep `/metrics`
  authenticated, loopback-only, or disabled.
- Protect `/etc/outpost`, `/var/lib/outpost`, backups, and radio keys.
- Retain the dedicated non-login service account installed by the project.
- Keep encrypted off-device backups and test recovery.
- Review audit and failed-login activity.

The systemd unit applies filesystem, privilege, kernel, device, and syscall restrictions while
allowing supported radio/accelerator devices.

## Authentication

First startup stores a short-lived setup token in a mode-0600 local file and only its Argon2 hash in
the database. The token never enters normal logs, expires after 60 minutes, and is consumed by its
first successful login. Setting the permanent password removes every bootstrap and dashboard
session secret. Local root can recover the original `operator` administrator with
`outpost-setup-token reset`. This invalidates all sessions and clears MFA only for that bootstrap
account; other named accounts and audit history remain intact.

The Access workspace manages three local web roles:

- **Administrator** can manage accounts and perform database restore in addition to operations.
- **Operator** can perform day-to-day radio, member, watch, and federation operations but cannot
  manage accounts or restore a database.
- **Read-only / wallboard** can view ordinary operational state but cannot mutate it or read
  private mail detail, audit records, backups, identity exports, or exact member detail.

These web accounts are intentionally separate from Meshtastic member handles and mesh trust. Audit
records use the signed-in web username, while member-originated actions keep their mesh identity.
The migrated account is `operator`; create personal accounts rather than sharing it.

Passwords are Argon2-hashed. TOTP uses the standard 30-second, six-digit format and remains usable
without internet. Enrollment produces eight recovery codes that are displayed once; only their
hashes are stored, and each is consumed on use. A successful login establishes a 10-minute strong
authentication window. After it expires, trust changes, federation trust/policy changes, restore,
emergency escalation, and other protected actions require password confirmation plus TOTP or a
recovery code when enabled. The original action is retried only after confirmation.

Sessions have an absolute configured expiry and record creation time, last activity, source, and
client description. Operators can revoke one session or all sessions from Access. Password reset,
password change, and disabling an account revoke that account's sessions. State changes use CSRF
tokens and login failures are limited by source and username. Authentication cannot be disabled
through YAML, including on loopback; isolate access with the bind address and host firewall while
retaining named accounts and audit attribution.

The unauthenticated HTTP surface is intentionally small: minimal health; loopback-gated diagnostic
status/readiness; login and initial setup; capability-like restore progress URLs; offline map tiles;
the static login shell; and captive-portal redirects. Every operational API requires a session.
OpenAPI/Swagger/ReDoc routes are disabled on the field appliance. Metrics require an Operator or
Administrator session by default, may be restricted to the effective loopback client address for
a same-host Prometheus process, or may be disabled. Read-only wallboard sessions cannot read them.

`sudo outpost-diagnostics --output /path/to/outpost-diagnostics.zip` creates a mode-0600 support
bundle containing recent warning/error lines, live health, versions/storage, and a non-secret
configuration summary. The broader journal requires `--include-journal`. The exporter
redacts legacy bootstrap passwords, current setup values, account password hashes, TOTP secrets,
recovery-code hashes, session cookies, bearer tokens, CSRF values, and content-shaped fields. It
does not query message tables. Review any bundle before sharing because ordinary operational errors
can still contain community-sensitive information.

The optional setup hotspot is an expiring bootstrap boundary, not trusted infrastructure. It uses a
generated WPA2 password, AP client isolation, no forwarding, and a host-input allowlist. Stop it as
soon as LAN access works, and use a dedicated read-only account for unattended wall displays.

Trusted local HTTP is a supported offline field mode, not a claim of encryption. Direct HTTPS and
trusted-proxy HTTPS are optional. `Secure` cookies and HSTS follow the effective request scheme;
forwarded scheme/client data is ignored unless its TCP peer is explicitly allowlisted. Deployment,
firewall, certificate rotation, and recovery guidance is in
[Web transport and network boundary](WEB-TRANSPORT.md).

The audit API and dashboard redact credential-shaped keys and assignments before displaying or
copying structured detail. This is a defense in depth, not permission to write secrets into audit
records: the database and its backups remain sensitive and must retain their filesystem controls.

## Location privacy

Member positions may be full, coarse, or off for member-facing queries; authenticated operators can
see an unexpired exact share. Exact shares carry a configurable scheduled deletion time and are
excluded everywhere once past due. Operators can delete one current position or purge all expired
positions with audit evidence. Public incident, welfare, and waypoint coordinates are separate
records with different lifecycles. Avoid publishing homes, shelters, vulnerable people, or resource
locations without authorization. Database backups can contain unexpired exact shares, and welfare
CSV exports can contain coordinates recorded at check-in; handle both as sensitive data.

## Radio limitations

Meshtastic security depends on channel-key and device management. A private channel is not audited
end-to-end application security. Metadata, timing, compromised clients, screenshots, and recipient
devices can disclose information.

## Federation and providers

Discovery is unauthenticated; pairing and active traffic are authenticated. Confirm codes out of
band, grant least privilege, monitor quotas, and revoke promptly. MQTT is infrastructure, not trust.

Weather, CAP, seismic, maps, MQTT, and AI are external boundaries. Data may be unavailable, stale,
malformed, or wrong. AI must not autonomously alert, change policy, disclose private data, or
execute arbitrary peer requests.

## Vulnerability reports

Do not put secrets, personal data, positions, working exploits, or vulnerable node addresses in a
public issue. Contact the repository owner privately with revision, impact, safe reproduction, and
remediation. Rotate exposed radio, dashboard, provider, or federation credentials immediately.
