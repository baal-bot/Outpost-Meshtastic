# Encrypted off-device recovery (#145)

Outpost can export a passphrase-encrypted recovery bundle, verify it without
writing plaintext, and restore into a **fresh directory** on a compatible offline
host. A restored application starts a local, authenticated review workbench. It
does not connect a radio, contact providers, run retention, resume queues, accept
assignments, announce an identity, or import pending federation records.

This implements the passphrase option in the original REQ-SEC-042. It is not a
claim of physical-media, power-loss, fresh-Pi, or peer-replacement qualification.
Those gates remain separate (#137, #146, #147). A restored key is continuity
evidence, not proof that another copy of that identity has stopped transmitting.

## What is captured

The whole transactional SQLite store is one indivisible recovery component:
members and privacy decisions, BBS, private mail/welfare, incidents and local
responsibility, runtime settings, accounts/MFA, audit, pairing secrets and origin
keys, revision lineages, replay/receipt evidence, and durable queues. Partial
table restore is intentionally unsupported: dropping the accompanying receipts
or lineage can resurrect actions or fork identity.

The bundle also includes the configuration selected with the normal config
loader, including this invocation's explicit `OUTPOST__` overrides, and the
configured intent map. Durable runtime overrides remain in SQLite and load on
the recovered app. Run export from the service's working directory and controlled
configuration environment; a different shell cannot discover the running
service's environment automatically. Freeze configuration/credential changes
during capture. SQLite's backup API provides a consistent database snapshot even
while source traffic continues; it does not make multiple external files atomic.

Direct-HTTPS certificate and private key files are included and their public
keys must match. `--include-provider-key` captures **only** the configured active
AI endpoint's named credential from this environment; omission is reported at
verification. It never dumps the environment. Restored provider credentials are
in a protected JSON data file, not executed or loaded as shell/environment code.
They are not automatically activated. `--radio-profile` includes an explicitly
selected operator-supplied radio profile as opaque data; no radio is queried or
programmed by recovery.

Radio-device PKI/channel PSKs that were never exported to the host cannot be
recovered from a host backup. Maps, models, Python/OS packages, systemd/network/AP
configuration, external proxy TLS, device firmware, disk encryption and physical
power equipment are not a host-image archive. Keep compatible offline install
media and these separately managed assets with the recovery kit. The original
configuration is retained as `effective-config.json`; the review configuration
remaps the database/support paths and isolates tiles/releases in the new target,
with loopback-only HTTP. It never silently reuses the original host's paths.

## Operator workflow

These are deliberate operator actions, not commands to run automatically during
an audit. Do not take down a serving node merely to test them.

1. Prepare the same compatible Outpost release and locked runtime on the recovery
   host before an outage. Keep the source wheel, dependency wheels and release
   verification evidence offline. Exact runtime version and migration-content
   fingerprint must match; restore does not migrate unknown/older archives.
2. Choose a strong unique passphrase (for example, six independently random
   words), and establish two authorized custodians. Keep offline recovery key
   instructions apart from the data copy. A lost passphrase has no bypass.
3. Under the intended service configuration/environment, export to a new filename:

   ```sh
   outpost-recovery export --config /etc/outpost/config.yaml --output /media/recovery/station-20260907.opr
   ```

   Add `--include-provider-key` or `--radio-profile /secure/radio-profile` only
   after reviewing that selection. The passphrase is entered twice in a private
   terminal, never through an argument or environment variable. Echo failure
   aborts. Standard output contains only ciphertext length and SHA-256.
4. Copy the encrypted file to independent removable media/off-device storage.
   Verify the actual copy, not just the source filename:

   ```sh
   outpost-recovery verify /media/independent-copy/station-20260907.opr
   ```

   This authenticates/decrypts, checks component hashes and configuration/support
   compatibility, deserializes SQLite in memory, checks schema objects and every
   migration version, checks integrity/foreign keys, and checks for an enabled
   named recovery operator. File creation alone is not restore verification.
5. On the offline recovery host, with the intended target parent already present:

   ```sh
   outpost-recovery restore /media/recovery/station-20260907.opr /srv/outpost-recovered-20260907
   outpost-recovery serve /srv/outpost-recovered-20260907
   ```

   Open `http://127.0.0.1:8080/recovery.html` on that machine (substitute the
   captured web port). The serve command ignores ambient configuration overlays;
   it uses only the validated target configuration. Use an authenticated tunnel
   for another local client; do not expose recovery HTTP to an untrusted LAN.
6. Have the second authorized operator enter their restored named-account
   credentials and MFA if enabled, verify the recovery digest, expected record
   counts and recorded node/signing identity against the source record, and sign
   out. Record the test date, compatible release, off-device copy/digest, operator,
   actual access result and missing external assets. Protect that operational
   record; do not record passwords, MFA seeds, pairing secrets or private data.

Keep at least two independent verified generations, one away from the station;
retain the last proven recoverable generation until its replacement has passed
the second-operator drill. Choose and record a copy interval consistent with the
community's loss tolerance. Recovery point is **the last verified off-device
copy**, not the latest local scheduled backup. This command never automatically
deletes old generations or rotates the passphrase. Re-export to rotate it; old
copies remain decryptable with their old key and require a custody/disposal plan.

## Fencing and return to service

Restoration removes copied web sessions and pending MFA enrollment secrets;
existing named accounts and established MFA remain. It writes a durable
`recovery_fence` row before any recovered database is published, with a redacted
restore audit entry. Source queues, ownership and revision counters are retained
unchanged as evidence, **not** replayed. The startup branch creates only the
recovery-review database heartbeat. Health returns 503
`recovery_review_required`; systemd may supervise this workbench without calling
it a healthy community node.

The web fence is a default-deny API allowlist: operator review, session inspection,
login/logout, self password change and step-up only. Radio configuration, exports,
providers, normal domain mutations, raw backups/restores and future unknown APIs
are blocked. The workbench shows counts and public identity evidence, not private
message bodies or secret keys. OS access to the restored directory necessarily
permits access to its private database; protect the host accordingly.

No reactivation/fence-removal command is provided by #145. #146 owns the reviewed
former-node exclusion, peer/key and revision reconciliation, queue disposition,
privacy/revocation reconciliation, local radio verification and return-to-service
decision. A stale snapshot cannot know later password changes, revocations,
consumed MFA codes, delivered work or newer remote revisions. Do not copy its raw
database over an active station, manually clear the fence, or assume possession
of the old private key establishes exclusive authority.

The existing database-only BackupService and quiesced in-place restore remain
available on ordinary nodes, with their existing safety snapshot/restart behavior.
Encrypted recovery never feeds a clone into that active-store overwrite path.
The serialized Database owner also exposes the same memory-only snapshot routine;
its writer lock remains held through cancellation until SQLite finishes.

## Failure and resource boundaries

Wrong key, altered/truncated header or ciphertext, duplicate/deep metadata,
component checksum/size/name errors, incompatible schema/configuration, corrupt
SQLite, unexpected database objects, bad references, and invalid support material
fail before target creation. Existing targets (including empty directories and
links) are never merged or overwritten. Input devices, FIFOs and symlinks are
rejected; trusted operator-controlled source/target parent directories are a
prerequisite, not protection against a hostile local administrator changing paths.

Database images are limited to 256 MiB; intent/config files to 1 MiB each, TLS
files to 64 KiB each, provider credential JSON to 16 KiB, optional radio profiles
to 2 MiB, and manifest JSON to 8 KiB/16 nesting levels. Snapshot copying has a
60-second bound. Encryption uses bounded memory rather than plaintext scratch
files: budget at least 2 GiB available RAM at the maximum image size. A larger
store needs a separately planned supported workflow; never trim identity/replay
tables to force a backup under the limit. Memory exhaustion can kill a process;
do not promise graceful recovery from the OS OOM killer.

Export creates only a 0600 ciphertext partial and publishes by atomic no-replace
hard link on the same filesystem, then flushes the parent. Choose media supporting
POSIX permissions and hard links (for example ext4); copy a completed encrypted
file to FAT media separately if needed. Restore reserves a fresh 0700 target and
writes fixed-name 0600 working files; only the final `READY` marker certifies
completed publication. File and directory flushes are requested, but flash/USB
flush compliance and power-loss survival remain physical gates.

Ordinary interruption/disk errors clean only the files this operation created;
the original encrypted file and all preexisting nodes remain untouched. A hard
kill/power cut can leave an incomplete protected target. `serve` refuses missing
or mismatched publication/fence evidence. Inspect it locally, retain the encrypted
original, and choose a new target; never treat a partial directory as a working
node. Disk-space preflight includes working-image headroom but cannot reserve
space against concurrent writers. The restored destination is deliberately
plaintext working storage, not a plaintext *export* temporary. Use encrypted
storage/encrypted or disabled swap, disable core dumps, and protect the terminal.
Python/SQLite/crypto memory is not promised to be securely zeroized.

## Format and verification evidence

Version 1 uses fixed magic `OUTPOST-RECOVERY` + NUL + byte 1, fresh 16-byte salt,
fresh 12-byte nonce, and an unsigned big-endian 8-byte ciphertext length. That
entire header is AES-GCM associated data. A fixed Scrypt cost (`N=131072, r=8,
p=1`, 32-byte output, approximately 128 MiB KDF memory) derives the AES-256-GCM
key; the 16-byte authentication tag is included in ciphertext length. Untrusted
files cannot select a more expensive KDF. The encrypted plaintext consists of a
4-byte manifest length, canonical UTF-8 JSON with runtime/schema/time and fixed
component descriptors, then exact component bytes in descriptor order. There
are no archive paths, decompression, external references or executable scripts.
Time is advisory provenance, never proof of freshness or identity exclusivity.

The implementation uses the installed cryptography 50 APIs:
[Scrypt](https://cryptography.io/en/50.0.0/hazmat/primitives/key-derivation-functions/#scrypt)
and [AESGCM](https://cryptography.io/en/50.0.0/hazmat/primitives/aead/#cryptography.hazmat.primitives.ciphers.aead.AESGCM).
[SQLite serialization](https://docs.python.org/3.12/library/sqlite3.html#sqlite3.Connection.serialize)
and the [WAL deserialization restriction](https://sqlite.org/c3ref/deserialize.html)
are handled by memory-only backup and VACUUM, not by exporting a plaintext file
or manually editing serialized WAL headers. Foreign keys, row IDs, producer
lineage and AUTOINCREMENT state survive the tested round trip.

Automated evidence lives in `test_recovery_format.py`,
`test_encrypted_recovery.py`, and `test_recovery_browser.py`: real encryption,
actual SQLite/schema/identity preservation, writer cancellation, malformed
inputs, no-overwrite/disk/interruption tests, secret selection, CLI operations,
fresh complete application startup, repeated fenced restart, and authenticated
offline Chromium at narrow/desktop widths. Simulated radios and synthetic stores
are deliberate. This is not a physical power-cut or second-appliance field drill.
