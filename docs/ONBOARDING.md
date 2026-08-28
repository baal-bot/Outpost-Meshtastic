# Field-appliance onboarding

Outpost keeps a resumable first-run checklist at `/var/lib/outpost/onboarding.json`. The file
contains only step state and timestamps, never passwords, radio keys, locations, or operator notes.
The interactive installer records identity setup; completed dashboard credentials are detected from
the database without copying their hashes into the checklist.

```sh
sudo outpost-onboarding status
sudo outpost-onboarding complete radio_connection
sudo outpost-onboarding defer federation
sudo outpost-onboarding reopen backups
```

Each status entry explains the action, a verification check, and separately states whether it needs
internet, radio access, a restart, or another operator. An interrupted install or field shift can
rerun `status` and continue from the remaining items. `defer` is an explicit operator decision, not
a successful verification; use it for an optional capability or a step that cannot yet be completed.

The checklist covers:

1. permanent operator credentials, named accounts, and administrator MFA/recovery;
2. Outpost identity, radio name, contact, timezone, units, and location/privacy decision;
3. the serial, TCP, or BLE radio connection and a mesh `PING`;
4. legal radio region, modem preset, channel indices/keys, and MQTT policy confirmed with the
   channel owner;
5. offline maps and weather, alert, earthquake, SAME, and AI provider choices;
6. a validated encrypted off-device backup and understood rollback path;
7. deliberately disabled/deferred or out-of-band-verified federation; and
8. an optional dedicated read-only wallboard account.

## Local discovery

The installer writes an Avahi `_http._tcp` service for the configured dashboard port and enables it
when `avahi-daemon` is installed. On a LAN with mDNS support, open:

```text
http://HOSTNAME.local:8080/
```

Use the host's actual static hostname and configured port. If the installer reports that Avahi is
absent, installing `avahi-daemon` requires package-network access; then run
`sudo systemctl enable --now avahi-daemon`. Set `OUTPOST_MDNS=0` only when site policy provides a
different discovery mechanism. mDNS announces the local service—it does not add WAN exposure,
authentication, or TLS.

## Temporary setup hotspot

When no suitable LAN exists, a root operator can deliberately create a short-lived WPA2 setup
network on an otherwise unused NetworkManager Wi-Fi interface:

```sh
sudo outpost-setup-hotspot start wlan0 30
sudo outpost-setup-hotspot status
sudo outpost-setup-hotspot stop
```

The duration must be 5–60 minutes. The command refuses an interface carrying another connection,
generates a one-time Wi-Fi password, disables autoconnect, enables AP client isolation, drops all
forwarded traffic, and limits host input to DHCP, DNS/mDNS, and the configured dashboard port.
Plain HTTP port 80 redirects to the dashboard for a captive-style setup entry point. A transient
systemd timer removes the NetworkManager connection and its password at expiry; `stop` removes it
early. The script requires NetworkManager (`nmcli`), nftables, and systemd.

This is a local bootstrap path, not a general guest network or an internet-sharing feature. Do not
run it on a Wi-Fi interface needed for the node's backhaul. Stop it as soon as permanent LAN access
and operator credentials work.

## Read-only wallboard

In **Access**, create a separate account with the **Read-only / wallboard** role and sign the kiosk
in with that account. The wallboard receives aggregate node/radio health, traffic/member counts,
and explicitly public board/channel labels only. It cannot retrieve identities, exact locations,
individual activity, messages, mail metadata, welfare or operator notes, configuration, audit,
backups, or AI review data. This role is optional; most Outposts should keep web access limited to
the operator and serve everyone else over the mesh. Never leave an Administrator session on a wall
display. Revoke the wallboard session from Access if the display is lost or reassigned.

## Diagnostic bundle

Create the default support bundle with:

```sh
sudo outpost-diagnostics --output /var/lib/outpost/outpost-diagnostics.zip
```

The mode-0600 archive includes a non-secret configuration summary, Outpost/Python/OS versions,
database schema and quick check, storage totals, systemd state/restarts, a loopback-only live
snapshot of task/radio/AI/SAME/provider health, and a bounded warning/error journal. It does not
query message tables. Credential-shaped fields and content/body/payload fields are redacted.

The broader 500-line journal is excluded by default because ordinary operational text can be
sensitive. Add `--include-journal` only when required, then inspect every archive member before
sharing. The live diagnostic endpoint accepts loopback clients only and returns a fixed field
allowlist; it is not a remote support API.
