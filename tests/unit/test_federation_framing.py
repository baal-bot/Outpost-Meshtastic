import os

import pytest

from outpost.fed import FrameCodec, FrameError, MessageType, Reassembler

SECRET = bytes(range(32))


def test_authenticated_frame_round_trip() -> None:
    codec = FrameCodec()
    frames = codec.encode(MessageType.SERVICE_QUERY, {"service": "weather"}, 7, SECRET)
    fragment = codec.decode_fragment(frames[0], SECRET)

    assert Reassembler().add("!peer", fragment) == {"service": "weather"}


def test_fragmented_message_reassembles_out_of_order() -> None:
    codec = FrameCodec()
    value = {"data": os.urandom(700)}
    frames = codec.encode(MessageType.ITEM, value, 8, SECRET)
    assert len(frames) > 1
    reassembler = Reassembler()
    result = None

    for frame in reversed(frames):
        result = reassembler.add("!peer", codec.decode_fragment(frame, SECRET))

    assert result == value


def test_tampered_frame_is_rejected() -> None:
    codec = FrameCodec()
    frame = bytearray(codec.encode(MessageType.PING, {"at": 1}, 9, SECRET)[0])
    frame[-1] ^= 1

    with pytest.raises(FrameError, match="HMAC"):
        codec.decode_fragment(bytes(frame), SECRET)


def test_only_hello_may_be_unsigned() -> None:
    codec = FrameCodec()
    hello = codec.encode(MessageType.HELLO, {"name": "North"}, 0, None)[0]
    assert codec.decode_fragment(hello, None).msg_type is MessageType.HELLO

    with pytest.raises(FrameError, match="requires a peer secret"):
        codec.encode(MessageType.PING, {}, 1, None)


def test_fragment_ceiling_is_enforced() -> None:
    with pytest.raises(FrameError, match="fragment ceiling"):
        FrameCodec(max_fragments=2).encode(MessageType.ITEM, os.urandom(900), 1, SECRET)


def test_frames_stay_under_live_radio_payload_ceiling() -> None:
    frames = FrameCodec().encode(
        MessageType.MAIL_RELAY,
        {"mesh_id": "!remote", "ciphertext": os.urandom(700)},
        1,
        SECRET,
    )

    assert len(frames) > 1
    assert max(map(len, frames)) <= 188


def test_incomplete_message_expires() -> None:
    codec = FrameCodec()
    frames = codec.encode(MessageType.ITEM, os.urandom(600), 10, SECRET)
    reassembler = Reassembler(timeout_s=5)
    assert reassembler.add("!peer", codec.decode_fragment(frames[0], SECRET), now=10) is None
    assert reassembler.expire(now=16) == 1
