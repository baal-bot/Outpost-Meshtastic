# Offline replacement kit and permanent local access (#147)

Prepare this kit while a trusted network and the intended host platform are
available. It supplies a verified **inactive Python runtime**, public reference
files and an inventory for the remaining appliance assets. It does not supply
boot media, install OS packages, configure a router, activate a service or radio,
or remove an encrypted-recovery identity fence.

The normal `deploy/install.sh` and `deploy/update.sh` still have online paths,
including pip/bootstrap, optional SAME downloads and map preparation. Do not
represent an ordinary installation with a warm package cache as an offline drill.

## Prepare and record each component

| Component | Required independent record and verification |
| --- | --- |
| Boot media and spare host | Image/version, host model, architecture, image checksum, boot test, Python 3.12/3.13 with venv/ensurepip, tzdata, device drivers, service account/groups/permissions, configured systemd unit and recovery console access. Keep a tested spare medium, not only an image file. |
| Outpost runtime | Exact reviewed source commit, saved green CI result, wheel bytes matching that source, complete pinned wheelhouse, host Python minor/platform/libc, maximum schema and migration-content fingerprint. The tool below records these. |
| Local network and power | Router/AP and switch models, offline admin access, supply/UPS and cables, saved configuration and checksums, private SSID credential custody, DHCP reservation, address/port and named-client access test. Router, AP and host all need outage power. |
| Radio and current clients | Region/preset-compatible firmware and approved client installers, vendor checksums/signatures and versions, host driver/USB cable, offline pairing instructions, distinct replacement radio identity and key review. Do not assume a phone app can be installed offline without its platform's prerequisites. |
| Maps | A verified local tile pack covering the intended area at the actual configured service path, manifest/attribution, bytes and digest. Check it using the real client with external map access blocked. A cached online image is not a pack. |
| Optional SAME/AI/vendor assets | Exact binaries, OS/driver packages, model files, dependency versions, device compatibility and hashes. Identify each omitted optional capability. The standard kit excludes these and will not download them during installation. |
| Private recovery material | At least two independently verified encrypted generations with copy dates/digests, protected configuration/device exports where authorized, and separately held passphrase/MFA/recovery instructions. Apply the [node-loss contract](NODE-LOSS-AND-MOBILITY.md). |
| Distribution and custody | Retain license/notice files and required attribution. Check distribution rights for each OS image, firmware, app, model, map and vendor asset before copying outside the authorized custody arrangement. Missing license metadata is unresolved, not permission. |

Wheel metadata and embedded license-file names are recorded in the manifest for
review; the tool does not make a licensing determination. This repository's
project metadata currently has no license declaration. Keep any required notices
with an authorized copy and resolve redistribution before publishing a kit.

## Build on the same host platform as the replacement

Use a clean checkout of the exact intended application revision and a prepared
Python environment. The builder's Python minor, Linux platform and libc must
match the replacement; it deliberately refuses a cross-platform claim. Use a
separate wheelhouse for each supported host, even when the application version
string is unchanged. The source-byte comparison and migration fingerprint bind
recovery to the particular revision.

First obtain and verify that revision's completed successful `ci` run while
online. Save the output of the existing CI verifier:

```sh
git -C /path/to/clean/source rev-parse HEAD > /media/preparation/source-commit.txt
gh run list --repo baal-bot/Outpost-Meshtastic --workflow ci.yml \
  --commit "$(cat /media/preparation/source-commit.txt)" \
  --json headSha,status,conclusion,databaseId,url \
  | python3 tools/check_ci_evidence.py --commit "$(cat /media/preparation/source-commit.txt)" \
  > /media/preparation/ci-evidence.json
```

Keep these files outside the source checkout. Verify release attestations while
online when using a published release, as described in [Releases](RELEASES.md).
Saved CI JSON is an operator-custodied observation, not a signed attestation.

Build the application wheel, then download all exact runtime/radio pins into a
new wheelhouse. These are the intentionally online preparation commands:

```sh
python3 -m pip wheel --no-deps --wheel-dir /media/preparation/wheels /path/to/clean/source
python3 -m pip download --only-binary=:all: \
  --requirement /path/to/clean/source/requirements.lock \
  --dest /media/preparation/wheels
python3 deploy/offline_kit.py build \
  --source /path/to/clean/source \
  --wheelhouse /media/preparation/wheels \
  --ci-evidence /media/preparation/ci-evidence.json \
  --output /media/preparation/outpost-kit
```

The builder accepts one application wheel plus exactly the locked runtime/radio
wheels, checks the application's files byte-for-byte against the clean Git
revision, and asks pip to validate local wheel compatibility without installing.
It copies only explicit public files. It never reads a live configuration,
database, environment credential or radio. Build failure removes only its own
new output directory; an existing output is refused.

Record the printed manifest SHA-256 on independently held trusted media. Copy
the whole kit to independent media, then verify **that copy** using a verifier
from the trusted preparation checkout/boot image:

```sh
python3 /trusted/outpost/deploy/offline_kit.py verify \
  --kit /media/independent/outpost-kit --expected-sha256 RECORDED_MANIFEST_SHA256
```

Use the independently recorded digest, not a digest recomputed from an untrusted
copy. The tool embedded in a kit is a convenience copy: authenticate it against
the trusted preparation copy before executing it. Manifest hashes detect altered,
missing or additional files but do not establish who created a kit on their own.

The manifest covers the guide, verifier, saved CI evidence, package metadata,
public source reference files and hash-pinned wheel requirements. Symlinks,
nonregular inputs and invalid inventory paths fail verification. The kit limit
is 512 files/2 GiB, with 512 MiB per file; large OS/map/model assets belong in the
separate equipment inventory. Record their hashes separately.

Open the kit's `OFFLINE-REPLACEMENT.md` entry page to reach the complete guide in
`reference/docs/`. The guide's local linked references retain their directory
layout and are included for use without the source checkout or Internet access.

## Install without package indexes or a cache

On the prepared replacement host, copy the kit and retain the trusted verifier
and independent digest. Verify before installing. Choose a **new** destination
whose parent already exists and that is outside the immutable kit:

```sh
python3 /trusted/outpost/deploy/offline_kit.py install \
  --kit /media/independent/outpost-kit --expected-sha256 RECORDED_MANIFEST_SHA256 \
  --destination /srv/outpost-runtime-NEW
```

The tool checks host compatibility and free space before creating the directory.
It creates a fresh venv using the prepared OS's ensurepip and installs only local
hash-pinned wheel paths, with indexes, caches, dependency fetching and source
builds disabled. It clears ambient pip/Python configuration, checks installed
dependencies and the complete version inventory, and re-verifies the kit before
writing `offline-kit.json` with `state: inactive`. It never upgrades pip online.
Failure removes its own fresh staging directory; existing destinations are
refused. Allow at least four times the kit size plus 256 MiB free for runtime
staging, **in addition** to the separately recorded OS, restore, maps and model
requirements. This estimate is not a guarantee against disk exhaustion.

These flags follow pip's documented [offline wheelhouse
workflow](https://pip.pypa.io/en/stable/cli/pip_download/) and [hash-checked binary
installation](https://pip.pypa.io/en/stable/topics/secure-installs/).

Use the new runtime's `bin/outpost-recovery` for the [encrypted archive
verification and restoration procedure](ENCRYPTED-RECOVERY.md). The recovered
workbench remains fenced and local. Verify named-operator login, record counts,
identity and digest using separately protected recovery material. A new serving
station requires fresh configuration, accounts, radio identity and peer decisions;
an archive is not a serving clone.

For persistent service, use the preprovisioned image's reviewed systemd/service
account setup and the [installation commissioning checks](INSTALLATION.md).
`reference/deploy/outpost.service` documents the expected `/opt/outpost/current`,
`/etc/outpost/config.yaml` and `/var/lib/outpost` layout; an inactive venv under
`/srv` alone does not satisfy it. Commission and record the new station's paths,
permissions, health and identity before enabling boot activation. Do not run the
online installer to fill an unrecorded prerequisite during an offline drill.

## Permanent powered operator LAN

Use a dedicated, preconfigured local router/AP with WAN disconnected and its
own outage power. Connect Outpost by Ethernet where possible. Enable a local
DHCP server, reserve the station address, and configure the AP to retain its
private operator SSID and credentials across reboot. Keep guest/public clients
off this LAN; AP isolation or guest VLAN rules must not block the authorized
phone-to-Outpost path. Do not place the mesh/backhaul radio on a temporary setup
interface just to obtain dashboard access.

Record the actual private address and configured port in the protected kit
record. Use the router's offline lease/reservation display or the host console's
`ip -br address` for recovery. A numeric LAN URL works without public DNS;
`HOSTNAME.local` is optional convenience when Avahi and the clients support mDNS.
Use the configured HTTPS name/trust material when TLS is enabled. Do not disable
TLS or reuse expired/unverified certificates to pass the drill. Trusted HTTP is
appropriate only under the existing [operator-LAN policy](WEB-TRANSPORT.md).

Join the private WLAN from the phone and verify the dashboard with cellular data
and automatic alternate-network switching disabled for the exercise. Sign in
with a named account and confirm an authorized operation. Check mesh access
separately with the intended current handheld client. Internet availability is
not part of either success criterion.

The 5–60 minute setup hotspot remains temporary. After permanent LAN access is
working, stop it with `sudo outpost-setup-hotspot stop`, confirm it is inactive,
and test the permanent address again. Also record behavior after its natural
expiry, after host/router/AP reboot, and after a controlled power interruption
when separately authorized. Never extend or remove its expiry to claim permanent
access. Local console access is the recovery path if LAN discovery fails.

## Second-operator exercise and refresh record

The acceptance record must identify: exercise date; kit/source/CI/manifest and
boot-image digests; target OS/Python/libc; prepared assets and omissions; operators
by locally appropriate identifiers; start/end time; network isolation method;
runtime install result; verified archive generation; named-account access result;
actual permanent URL; hotspot expiry/stop and reboot results; mesh-client result;
and every missing prerequisite or unexpected network request. Keep private
addresses, identifiers and credentials out of public issue evidence.

A second operator must work from the kit instructions on a fresh target with
WAN unavailable. Remove access to old package caches and shared mounts; use
network isolation/observation to detect accidental dependence on Internet or the
original source host. Record a failed dependency as a failed step. Do not fetch it
online and retain the earlier start time as a successful offline result.

Refresh after source/schema/lock, OS/Python/libc, firmware/client, map coverage,
credential custody or local-network changes. Prepare and verify a new generation,
run the isolated installation and operator drill, and retain the last proven
generation until its replacement passes. Keep secrets and recovery keys outside
the public runtime kit; apply the community's retention and destruction rules.

The automated kit tests establish preparation, integrity and fresh-directory
runtime installation behavior. #147 remains open until the actual fresh-appliance,
permanent-client, reboot and second-operator results above exist. #136/#139/#145
and the power/RF gates keep their separate qualification requirements.
