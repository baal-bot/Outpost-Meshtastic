# Releases and artifact verification

Tagged releases are created only by `.github/workflows/release.yml`. The release job calls the
complete CI workflow against the tag's exact commit and cannot package until both supported Python
versions pass. Every third-party action is pinned to its full commit SHA; Dependabot proposes
reviewed pin updates each week.

## Published evidence

Each GitHub release contains:

- the Outpost wheel;
- `RELEASE-METADATA.json`, identifying the package/version, exact source commit and tag, maximum
  database schema, supported Python range, tested operating systems, capability-manifest digest,
  and build workflow/time;
- `outpost.spdx.json`, an SPDX JSON software bill of materials; and
- `SHA256SUMS`, covering the wheel, release metadata, and SBOM.

GitHub artifact attestations cover every published file. A second attestation binds the SBOM to
the wheel. These attestations provide build provenance through GitHub's Sigstore-backed service;
they do not replace review of release notes or operational compatibility.

## Verified production update

Install the GitHub CLI, authenticate it for public attestation verification, and start from a clean
repository checkout. Use an immutable release tag:

```sh
./deploy/update.sh v0.2.0
```

Before changing the checkout or running the installer, the updater:

1. downloads every asset from that repository release into a private temporary directory;
2. rejects missing, unexpected, duplicated, path-like, or checksum-mismatched artifacts;
3. validates the package, version, schema, platform, tag, and full source commit in the metadata;
4. verifies a GitHub artifact attestation for every asset; and
5. verifies that the authenticated metadata commit is exactly the commit addressed by the Git
   tag.

Any failed check stops the update before activation. Every tag or development ref must also have a
successful completed `ci.yml` run for its exact commit. The updater passes normalized CI evidence
to the installer, which stores it as `ci-evidence.json` in that release. A direct Git-checkout
upgrade cannot claim evidence or bypass this handoff. `origin/main` and other development refs can
still be installed deliberately after CI, but they do not gain signed-release provenance.
Production nodes should use only `v*` tags.

Fresh field installation does not require GitHub access. For recovery of an existing offline node,
`OUTPOST_ALLOW_UNVERIFIED_CI=1 ./deploy/update.sh <local-ref>` is the explicit emergency override.
When origin cannot be fetched, the ref must already exist locally. The override is prominently
warned and produces no green-CI evidence file; use it only after running whatever local gates the
incident allows.

To inspect a release without installing it:

```sh
release_dir=$(mktemp -d /tmp/outpost-verify.XXXXXXXX)
gh release download v0.2.0 --repo baal-bot/Outpost-Meshtastic --dir "$release_dir"
python3 tools/verify_release.py --directory "$release_dir" --tag v0.2.0
for artifact in "$release_dir"/*; do
  gh attestation verify "$artifact" --repo baal-bot/Outpost-Meshtastic
done
```

Remove the temporary directory after review. Do not activate an artifact merely because its raw
checksum matches: the attestation and source-commit checks establish where that checksum came from.

## Rollback

Before an upgrade, keep a validated off-device backup and retain the previous release. The
installer creates an integrity-checked database snapshot and automatically restores code and data
if the new service fails its health check. Required local-AI readiness is part of that check; an
enabled Hailo model that cannot acquire the accelerator cannot cause an upgrade to be accepted as
healthy. After a successful activation, run:

```sh
sudo outpost-rollback
```

The rollback command verifies its compatibility plan before downtime and restores the matching
pre-upgrade database when the older code cannot read the live schema. See [Installation](INSTALLATION.md#roll-back)
for the full procedure.

## Compromise and revocation

If a release, workflow credential, action pin, or build dependency may be compromised:

1. stop deployments and tell operators which tags, commits, and time window are affected;
2. remove the affected release assets from distribution and publish a GitHub security advisory;
3. rotate exposed credentials, revoke affected access, and review workflow/audit logs;
4. restore trusted action pins and dependencies, then rebuild from a reviewed clean commit through
   the normal CI-dependent release workflow;
5. publish a new version with the investigation scope, replacement instructions, and verified
   source commit; and
6. have affected nodes take an off-device backup, roll back to a known-good release when compatible,
   or install the replacement tag through `deploy/update.sh`.

GitHub attestations are immutable evidence; deleting a release does not make an already-downloaded
artifact disappear. Operator notification and the security advisory are therefore the revocation
channel. Never silently recreate a deleted tag or replace assets under an existing version.
