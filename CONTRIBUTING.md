# Contributing to Outpost

Thank you for helping improve Outpost. Read the [development guide](docs/DEVELOPMENT.md), open a
focused issue or pull request, and include tests and documentation for behavior changes.

For radio-related work, state the host, radio model, connection type, Meshtastic firmware, region,
and modem preset without disclosing channel keys. For UI work, test phone and desktop layouts. For
protocol or schema work, describe compatibility, migration, privacy, and airtime consequences.
Behavior, limitation, field-result, or maturity changes must update `docs/capabilities.toml` and
regenerate the capability documentation with `python tools/check_capabilities.py`.
Requirement changes also need an explicit [disposition review](docs/REQUIREMENT-RECONCILIATION.md)
and `python -m tools.check_requirements --check`. Preserve the original snapshot and obtain owner
approval for replacements or withdrawals; passing software tests do not close field gates.

Never submit real databases, credentials, precise member locations, private messages, or pairing
secrets. Use `outpost-replay export` for the smallest relevant pseudonymized, position-coarsened
bundle, review it manually, and use `--strip-bodies` when command text is unnecessary. Follow
[SECURITY.md](SECURITY.md) for vulnerability reports.
