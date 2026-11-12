import time

import pytest

from app.signature import SignatureError, compute_signature, make_header, verify

SECRET = "whsec_test_secret"
BODY = b'{"id": "evt_1", "type": "charge.succeeded"}'


def test_valid_signature_passes():
    verify(BODY, make_header(BODY, SECRET), SECRET)


def test_changed_body_fails():
    header = make_header(BODY, SECRET)
    with pytest.raises(SignatureError):
        verify(BODY.replace(b"evt_1", b"evt_2"), header, SECRET)


def test_wrong_secret_fails():
    with pytest.raises(SignatureError):
        verify(BODY, make_header(BODY, "whsec_attacker"), SECRET)


def test_old_timestamp_fails():
    old = int(time.time()) - 301
    with pytest.raises(SignatureError, match="tolerance"):
        verify(BODY, make_header(BODY, SECRET, timestamp=old), SECRET)


def test_timestamp_just_inside_tolerance_passes():
    ts = 1_700_000_000
    verify(BODY, make_header(BODY, SECRET, timestamp=ts), SECRET, now=ts + 300)


def test_missing_header_fails():
    with pytest.raises(SignatureError, match="missing"):
        verify(BODY, None, SECRET)
    with pytest.raises(SignatureError):
        verify(BODY, "", SECRET)


def test_multiple_v1_values_one_correct_passes():
    ts = int(time.time())
    good = compute_signature(BODY, SECRET, ts)
    header = f"t={ts},v1={'0' * 64},v1={good},v0=legacy"
    verify(BODY, header, SECRET)


def test_multiple_v1_values_none_correct_fails():
    ts = int(time.time())
    header = f"t={ts},v1={'0' * 64},v1={'f' * 64}"
    with pytest.raises(SignatureError):
        verify(BODY, header, SECRET)


@pytest.mark.parametrize("header", ["garbage", "t=abc,v1=xyz", "v1=abc", "t=1700000000"])
def test_malformed_header_fails(header):
    with pytest.raises(SignatureError):
        verify(BODY, header, SECRET)


def test_unconfigured_secret_rejects_everything():
    with pytest.raises(SignatureError, match="not configured"):
        verify(BODY, make_header(BODY, ""), "")
