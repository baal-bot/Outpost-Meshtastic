# Requirement reconciliation — 2026-09-05

The [generated ledger](REQUIREMENT-DISPOSITIONS.md) inventories **508 original REQ definitions**
from the first specification commit, `04e74cad6925f69e0157905967e748b44f318b1f`, and **23 later
identities**. It preserves complete original wording beside a reviewed current snapshot.
The six original product gates are tracked separately. This is a traceability baseline, **not
a declaration that the platform is ready for an unattended outage**.

## Reading the dispositions

- `implemented_tested`: the complete unchanged clause has specifically reviewed automated
  evidence, identified by test node, full tested commit and CI run. This is a software claim.
- `accepted_replacement`: the owner approved an explicit requirement change. Acceptance of the
  design does not imply every implementation or deployment acceptance test has passed.
- `deferred`: full clause-level acceptance is still unproven or outstanding. The original intent
  remains in force unless explicitly replaced. This does **not** mean a feature is absent, work
  is paused, or the owner waived it. Domain-level source/evidence pointers and tracking issues
  are conservative review starting points, not proof of every individual MUST.
- `withdrawn`: an explicit owner-approved removal. None are withdrawn in this review.

The conservative baseline has one fully traced unchanged clause, ten approved replacement
clauses and 520 pending acceptance dispositions. Most functionality has substantially more
evidence in the [capability matrix](FEATURES.md); passing files or closed bug tickets do not prove
complete original-spec compliance. Promote individual clauses only after reviewing their full
wording and evidence. A software-only MUST can be accepted independently of a domain's hardware
tests when it has sufficient direct evidence.

## Approved choices and unresolved conflicts

The owner replied “approved, proceed” to four specified architecture choices. The
[GitHub decision record](https://github.com/baal-bot/Outpost-Meshtastic/issues/154#issuecomment-5555091612)
records that conversational approval; it is not an independently authored GitHub review.

| Topic | Disposition and boundary |
| --- | --- |
| Incident references | Permanent bindings/tombstones replace active-only reuse (WATCH-011, #138). Restore lineage and lost-node recovery remain #146. |
| Clock-skewed reconciliation | Producer revisions on mutually upgraded peers replace a universal cursor claim (FED-042, #134). Legacy links remain clock-sensitive; RTC confidence is #141. |
| Federation frame/carrier | 188 complete application bytes / 170 body bytes and authenticated broadcast replace old 215-byte/direct-carrier assumptions (FED-006/008, TRANSPORT-037/038). New-mesh capacity and physical delivery remain unqualified. |
| Guarded retrieval | Deterministic permission-scoped retrieval, FTS5/BM25 and guarded fallback replace a required model-tool loop/embedding configuration (AI-006/007/008/023). Independent quality/resource acceptance remain #156. |
| Event-driven incidents | The 60-second community/map outcome remains open (#135, #157). Catch-up manifests, remote quarantine receipt and operator acceptance are different stages. |
| Encrypted backups | Database snapshots and external-storage encryption advice are not integrated encrypted backups or fresh-node restoration (FED-015a, SEC-042; #145/#146). |
| Physical sync | Signed, filtered bundle exchange remains missing (FED-041, #143). Full backups and replay fixtures are not substitutes. |
| Durability/boot | WAL/NORMAL remains current original policy, not a power-cut persistence guarantee (DATA-002, #137/#44). Installed-service alignment is #136; the development process is not boot evidence. |
| Migration numbering | Shared monotonic history differs from original reserved module bands (DATA-009). Replacement approval is pending; do not renumber applied migrations. |
| Application cryptography | The original blanket prohibition conflicts with encrypted federation mail/relay (SEC-028). This was not among the approved choices; reconcile the exception explicitly. |

The added operator-policy clause accidentally reused `REQ-FED-015a`. It is now `REQ-FED-015b`;
the original shared-secret requirement keeps its identity. This is an identifier correction,
not changed behavior or permission to skip encrypted recovery.

G1–G6 all remain open with reasons and issue links: fresh offline deployment, unprompted handheld
usability, seven-day airtime, independent AI quality, 30-day recovery, and incident-to-community/map
latency. The hold on unavailable-node and disruptive physical tests remains in effect. No reboot,
live migration, radio test, WAN cut or deployment is authorized by this reconciliation.

## Evidence and maintenance

The dated [adversarial review](ADVERSARIAL-REVIEW-2026-09-05.md) and its linked public synthetic
probe archive and [version-controlled originals](review-artifacts/README.md) are preserved.
Confirmed defects have ordinary regression tests linked from
[resilience hardening](RESILIENCE-HARDENING.md), [burst qualification](EMERGENCY-BURST-QUALIFICATION.md)
and [paging qualification](FEDERATION-PAGING-QUALIFICATION.md), not unmanaged expected failures.
Historical field records and closed #41/#118/#128 work remain dated evidence; their closure and
this ledger do not close #44 or present release gates.

Edit `docs/requirement-dispositions.toml`, then run:

```sh
.venv/bin/python -m tools.check_requirements
.venv/bin/python -m tools.check_requirements --check
.venv/bin/python tools/check_capabilities.py --check
```

For an intentional specification change, first review the original/current text, disposition,
implementation and approval scope. Then explicitly refresh **all and only** changed IDs:

```sh
.venv/bin/python -m tools.check_requirements --refresh-current REQ-AREA-001 REQ-AREA-002
```

The command preserves original definitions. New, removed or changed definitions fail CI until
reviewed; duplicate IDs fail inventory. New identities cannot silently become accepted through a
domain default. Replacement/withdrawal needs a repository approval-comment URL and rationale;
tested claims need collected, non-skipped test nodes. Gate closure needs a field record and exact
revision. Generated output is checked for drift. Normal checks need no GitHub/network access.

These are mechanical guardrails, not an automated semantic auditor: reviewers must verify approval
scope, that linked CI passed on the named revision, whole-clause evidence sufficiency, and claimed
field measurements. Original snapshot edits must be checked against the immutable source commit,
never used to redefine history. Unnumbered constraints/non-goals, phase exit criteria and ADRs
still need human review; the inventory does not parse every normative sentence in prose.
