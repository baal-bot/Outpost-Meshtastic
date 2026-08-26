# Security and privacy

Outpost handles identities, messages, positions, incidents, and operational state. Deploy it as a
trusted local service, not an anonymous public web application.

## Baseline

- Maintain the OS, Python, Meshtastic firmware, and Outpost revision.
- Complete the short-lived local setup-token flow and set a permanent dashboard password.
- Limit port 8080 and `/metrics` to a LAN, VPN, or authenticated TLS proxy.
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
session secret. Local root can recover with `outpost-setup-token reset`. State changes use CSRF
tokens and login failures are limited by source. `auth.mode: none` works only on loopback and must
not be placed behind an unauthenticated public proxy.

`sudo outpost-diagnostics --output /path/to/outpost-diagnostics.zip` creates a mode-0600 support
bundle containing a bounded journal excerpt and non-secret configuration summary. The exporter
redacts legacy bootstrap passwords, current setup values, password hashes, session cookies, bearer
tokens, and CSRF values. Review any bundle before sharing because ordinary operational messages can
still contain community-sensitive information.

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
