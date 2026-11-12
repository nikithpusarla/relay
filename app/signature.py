"""Stripe webhook signature verification, written by hand.

Stripe sends a header like:  Stripe-Signature: t=1700000000,v1=abc...,v1=def...
  t  = when Stripe signed it (Unix seconds)
  v1 = HMAC-SHA256(secret, f"{t}." + raw_body) in hex. There can be several
       v1 values while a secret is being rotated; any one matching is enough.
"""

import hashlib
import hmac
import time


class SignatureError(Exception):
    """The request did not provably come from Stripe."""


def compute_signature(payload: bytes, secret: str, timestamp: int) -> str:
    # Sign the exact raw bytes: re-serialising parsed JSON can change spacing
    # or key order, and then the signature would never match.
    signed = f"{timestamp}.".encode() + payload
    return hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()


def make_header(payload: bytes, secret: str, timestamp: int | None = None) -> str:
    """Build a valid header. Used by tests and the demo sender."""
    ts = int(time.time()) if timestamp is None else timestamp
    return f"t={ts},v1={compute_signature(payload, secret, ts)}"


def _parse_header(header: str) -> tuple[int, list[str]]:
    timestamp: int | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, sep, value = part.strip().partition("=")
        if not sep:
            continue
        if key == "t":
            try:
                timestamp = int(value)
            except ValueError as exc:
                raise SignatureError("bad timestamp") from exc
        elif key == "v1":
            signatures.append(value)
    if timestamp is None:
        raise SignatureError("no timestamp in header")
    if not signatures:
        raise SignatureError("no v1 signature in header")
    return timestamp, signatures


def verify(
    payload: bytes,
    header: str | None,
    secret: str,
    tolerance: int = 300,
    now: float | None = None,
) -> None:
    """Raise SignatureError unless the header is a fresh, valid Stripe signature."""
    if not secret:
        raise SignatureError("webhook secret is not configured")
    if not header:
        raise SignatureError("missing Stripe-Signature header")

    timestamp, signatures = _parse_header(header)

    # The timestamp is inside the signed data, so an attacker can't change it.
    # Rejecting old ones stops "replay attacks" with a captured request.
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance:
        raise SignatureError("timestamp outside tolerance")

    expected = compute_signature(payload, secret, timestamp)
    # compare_digest takes the same time whether the first or last character
    # differs, so an attacker can't guess the signature byte by byte by timing.
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise SignatureError("no matching signature")
