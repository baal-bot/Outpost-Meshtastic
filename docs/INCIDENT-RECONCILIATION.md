# Incident identity and reconciliation

Outpost keeps each incident origin as an immutable identity. A human merge changes which incident
is shown as canonical; it does not replace an origin UID, erase the source incident, or rewrite the
provenance timeline.

## Match and merge workflow

Watch suggests a match only when both incidents have the same type, were created within two hours,
are within 1 km, and have at least 15% title-token overlap. The detail card explains every bound.
An operator must choose **Merge** or **Not a match**. Federation import never makes this decision.

A merge retains all `incident_origin` rows on the selected canonical incident. The hidden source
row remains available for later source updates and unmerge. **Restore incident** moves that source's
origin cluster back without deleting either timeline. Later field corrections are separate,
attributed operator actions.

## Field authority

| Field | Merge rule | Later federated update |
|---|---|---|
| Status | Canonical target wins | Origin-owner update applies after inbox approval, except a terminal state cannot close a locally monitored incident |
| Severity | Highest value wins | Origin-owner update applies to an unmerged origin; merged-origin value is advisory |
| Location | Canonical target wins; source fills a missing target location | Merged-origin location is advisory |
| Description/title | Canonical target wins | Merged-origin description is advisory |
| Expiration | Latest known timestamp wins | Merged-origin expiration is advisory |
| Resolution | Canonical target wins | Remote resolution is withheld while local status is `monitoring` |

“Advisory” means the payload is appended to provenance, the canonical incident receives a review
flag, and its operational fields do not change silently.

## Partition and conflict behavior

Each origin records its last accepted source timestamp and digest. An older update is recorded as
`stale_update_ignored`. A different payload at the same source timestamp is recorded as
`concurrent_update_conflict`. A newer update to a merged source refreshes only its hidden source row
and is recorded as `merged_origin_update`; unmerge then reveals that latest source state.

Canonical federation exports carry every contributing origin UID. Receiving Outposts adopt those
identities without deriving identity from mutable titles, locations, or Outpost display names.
Origins that already belong to another local incident are not moved automatically and both records
are flagged for review.

The append-only `incident_provenance` timeline records imports, local reactions and updates,
operator corrections, merges, unmerges, rejected suggestions, conflicts, and withheld resolutions.
The regular audit log separately records which authenticated web operator invoked each action.

Automated partition, reconnect, concurrent-edit, identity-adoption, merge, and unmerge coverage is
in `tests/integration/test_incident_reconciliation.py`.
