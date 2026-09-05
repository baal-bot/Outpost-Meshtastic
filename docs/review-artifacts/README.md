# Historical adversarial probes

`adversarial-probes-4975680.py.txt` preserves the exact synthetic review probes run against
`49756805513ca3fe3e15cfe9c30569d49f4a63f5` on 2026-09-05: ten probes, nine failures and one
positive control. The corresponding [review](../ADVERSARIAL-REVIEW-2026-09-05.md) and public
GitHub archive record the observations. These are synthetic temporary databases and simulated
radios, not captures of live user traffic.

The `.txt` suffix deliberately keeps this immutable historical artifact out of test collection.
It is not an expected-failure suite, acceptance evidence for current code, or a supported test
API. For historical reproduction, use a separate disposable checkout of the named revision,
copy this artifact to a `.py` file there, and run pytest with that revision's dependencies.
Do not check out the old source in the running appliance's working directory.

Maintained regressions live under `tests/` and are linked from
[resilience hardening](../RESILIENCE-HARDENING.md). Their production paths and semantics supersede
the old probes as fixes are implemented. The legacy manifest probe intentionally does not prove
the modern producer-revision protocol (#134). Event-driven propagation (#135) and physical
durability (#137/#44) remain open, without unmanaged xfails concealing them.
