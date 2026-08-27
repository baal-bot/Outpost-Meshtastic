# SAME decoder fixture

The hardware acceptance check uses `npt.22050.s16le.bin` from
[`cbs228/sameold`](https://github.com/cbs228/sameold/tree/samedec-0.4.2/sample) at the
immutable `samedec-0.4.2` tag. It is mono, signed 16-bit little-endian audio sampled at
22,050 Hz.

The fixture is fetched on demand instead of adding a 293 KB binary to the repository.
`tools/verify_same_audio.py` requires SHA-256
`65c58a6c3e34fa5ed68f7288b6f10369bfb73034e5f12da5e8e63671dcf15b88` before passing it
to `samedec`. Expected decoder output is:

```text
ZCZC-PEP-NPT-000000+0030-2771820-TEST    -
```

The upstream project is dual-licensed Apache-2.0/MIT. The test message is a National
Periodic Test and Outpost's policy always records it as log-only.
