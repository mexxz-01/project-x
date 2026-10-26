#!/usr/bin/env python3
"""Zero-dependency test suite. Run: python tests/test_crypto.py"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("px", os.path.join(HERE, "..", "x.pyw"))
px = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(px)

PASSED = []


def check(name, fn):
    try:
        fn()
        PASSED.append(name)
        print(f"  ok    {name}")
    except Exception as e:
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")
        raise SystemExit(1)


def t_aes_gcm_vector():
    """NIST AES-256-GCM test vector - proves the AES core is correct."""
    assert px.crypto_selftest() is True, "AES-GCM self-test failed"


def t_seal_roundtrip():
    obj = {"ev": "req", "xid": 1, "cmd": "shell", "args": {"cmd": "whoami"}}
    assert px.unseal(px.seal(obj), replay=False) == obj


def t_seal_is_not_plaintext():
    blob = px.seal({"hello": "world"})
    assert b"world" not in blob, "plaintext leaked into ciphertext"


def t_seal_is_randomised():
    obj = {"x": 1}
    a, b = px.seal(obj), px.seal(obj)
    assert a != b, "nonce is not random"
    assert px.unseal(a, replay=False) == px.unseal(b, replay=False)


def t_tamper_is_rejected():
    blob = bytearray(px.seal({"x": 1}))
    blob[-1] ^= 0xFF
    try:
        px.unseal(bytes(blob), replay=False)
    except ValueError as e:
        assert "auth" in str(e)
        return
    raise AssertionError("tampered blob accepted")


def t_truncated_is_rejected():
    try:
        px.unseal(b"\x00" * 10, replay=False)
    except ValueError:
        return
    raise AssertionError("short blob accepted")


def t_replay_is_rejected():
    px._RX.clear()
    blob = px.seal({"x": 1})
    px.unseal(blob, replay=True)
    try:
        px.unseal(blob, replay=True)
    except ValueError as e:
        assert "replay" in str(e)
        return
    raise AssertionError("replayed frame accepted")


def t_wrong_key_fails():
    ek, mk = px._derive("a-different-passphrase")
    orig_ek, orig_mk = px.EK, px.MK
    try:
        px.EK, px.MK = ek, mk
        blob = px.seal({"x": 1})
    finally:
        px.EK, px.MK = orig_ek, orig_mk
    try:
        px.unseal(blob, replay=False)
    except ValueError:
        return
    raise AssertionError("frame decoded with the wrong key")


def t_large_payload():
    big = {"data": "A" * 600_000}
    assert px.unseal(px.seal(big), replay=False) == big


def t_keystream_is_not_repeating():
    ks = px._ks(px.EK, b"\x00" * 8, 128)
    assert ks[:32] != ks[32:64] != ks[64:96]


def t_xorb_exact():
    a, b = b"\x01\x02\x03", b"\xff\xfe\xfd"
    assert px._xorb(a, b) == bytes([0x01 ^ 0xFF, 0x02 ^ 0xFE, 0x03 ^ 0xFD])
    assert len(px._xorb(b"\x00\x00", b"\xff\xff")) == 2


if __name__ == "__main__":
    print(f"testing {os.path.basename(os.path.dirname(HERE))}/x.pyw\n")
    for name, fn in sorted(globals().items()):
        if name.startswith("t_") and callable(fn):
            check(name[2:], fn)
    print(f"\nALL {len(PASSED)} TESTS PASSED")