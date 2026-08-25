# Security and privacy

Outpost handles identities, messages, positions, incidents, and operational state. Deploy it as a
trusted local service, not an anonymous public web application.

## Baseline

- Maintain the OS, Python, Meshtastic firmware, and Outpost revision.
- Replace the generated dashboard password immediately.
- Limit port 8080 and `/metrics` to a LAN, VPN, or authenticated TLS proxy.
- Protect `/etc/outpost`, `/var/lib/outpost`, backups, and radio keys.
- Retain the dedicated non-login service account installed by the project.
- Keep encrypted off-device backups and test recovery.
- Review audit and failed-login activity.

The systemd unit applies filesystem, privilege, kernel, device, and syscall restrictions while
allowing supported radio/accelerator devices.

## Authentication

The initial random password appears once in the journal. Passwords use Argon2. Sessions use random
tokens stored as hashes, and state changes use CSRF tokens. Login failures are limited by source;
password change invalidates other sessions. `auth.mode: none` works only on loopback and must not be
placed behind an unauthenticated public proxy.

## Location privacy

Member positions may be full, coarse, or off. Public incident/waypoint coordinates have different
visibility. Avoid publishing homes, shelters, vulnerable people, or resource locations without
authorization. Backups and exports may retain historical information.

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
