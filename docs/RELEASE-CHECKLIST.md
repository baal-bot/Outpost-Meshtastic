# Release evidence checklist

Use this checklist before creating a release tag. A green build is necessary but does not by
itself promote any capability to production-ready.

## Capability evidence

- [ ] Update `docs/capabilities.toml` for every behavior, limitation, field result, or maturity
  change in the release.
- [ ] Every evidence path exists, describes the claimed result, and contains no credentials,
  private messages, exact member locations, channel keys, or pairing secrets.
- [ ] Field and hardware claims identify the relevant date, target, boundary, and remaining gate.
- [ ] Replace an affected capability's `HEAD` verification with the release commit/tag when the
  evidence is frozen; retain its original introduction commit.
- [ ] Run `python tools/check_capabilities.py` and review the generated README summary and complete
  `docs/FEATURES.md` diff.
- [ ] Run `python tools/check_capabilities.py --check`; CI and the release workflow must pass the
  same check on the exact release revision.
- [ ] Run `python tools/check_commands.py` after command metadata changes, review the generated
  `docs/COMMANDS.md` table, and confirm `python tools/check_commands.py --check` passes.

## Quality and recovery

- [ ] Both supported Python versions pass the complete CI matrix, browser suite, critical coverage
  gates, package smoke test, and dependency audit.
- [ ] `deploy/update.sh` selects that successful exact-commit run, and the activated release's
  `ci-evidence.json` identifies the same commit and run URL.
- [ ] Schema/version compatibility and migration behavior are documented for this release.
- [ ] A verified off-device backup exists before upgrading field nodes.
- [ ] Upgrade, health-check failure, and rollback procedures match the release artifacts.
- [ ] Known safety, power-loss, radio, thermal, provider, and multi-node gaps remain visible in the
  capability matrix and release notes.

## Supply-chain evidence

- [ ] Every third-party workflow action uses a reviewed full commit SHA; action-pin updates arrive
  through Dependabot rather than unreviewed tag movement.
- [ ] `RELEASE-METADATA.json` names the package/version, exact tag and commit, highest database
  schema, Python range, supported/tested operating systems, and capability-manifest digest.
- [ ] The wheel, metadata, SPDX SBOM, and `SHA256SUMS` are present, and GitHub artifact attestations
  verify for every file; the wheel also has an SBOM attestation.
- [ ] A clean-checkout `deploy/update.sh v<version>` rehearsal rejects a tampered asset and confirms
  the authenticated metadata commit matches the release tag before activation.
- [ ] Release notes link to the rollback procedure and identify the security-advisory channel for
  compromise notification and revocation.

## Release decision

- [ ] Any `production_ready` promotion has an explicit operator/reviewer decision backed by the
  required field, recovery, security, compatibility, and soak evidence.
- [ ] The release notes distinguish implemented behavior from automated, simulated, field,
  hardware-gated, and production-ready evidence.
- [ ] The tagged commit is the exact commit that passed required CI; no independently rebuilt or
  unverified working-tree artifact is published.
