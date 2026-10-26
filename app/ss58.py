"""Minimal SS58 helpers (validation / normalisation) without extra dependencies."""

import hashlib

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_PREFIX = b"SS58PRE"
SS58_FORMAT = 42


def _b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        n = n * 58 + _B58.index(ch)
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    pad = len(s) - len(s.lstrip("1"))
    return b"\x00" * pad + raw


def _b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    pad = len(b) - len(b.lstrip(b"\x00"))
    return "1" * pad + out


def encode(pubkey: bytes, fmt: int = SS58_FORMAT) -> str:
    assert len(pubkey) == 32 and fmt < 64
    payload = bytes([fmt]) + pubkey
    checksum = hashlib.blake2b(_PREFIX + payload, digest_size=64).digest()[:2]
    return _b58encode(payload + checksum)


def decode(address: str) -> bytes:
    """Return the 32-byte public key, raising ValueError when invalid."""
    try:
        raw = _b58decode(address)
    except ValueError as e:
        raise ValueError("invalid base58") from e
    if len(raw) != 35 or raw[0] >= 64:
        raise ValueError("unsupported SS58 address")
    payload, checksum = raw[:33], raw[33:]
    if hashlib.blake2b(_PREFIX + payload, digest_size=64).digest()[:2] != checksum:
        raise ValueError("bad checksum")
    return payload[1:]


def normalize(value) -> str | None:
    """Turn an SS58 string, 0x-hex string, bytes or int sequence into a generic (42) SS58 address."""
    if value is None:
        return None
    if isinstance(value, str):
        v = value.strip()
        if v.startswith("0x") and len(v) == 66:
            return encode(bytes.fromhex(v[2:]))
        return encode(decode(v))
    if isinstance(value, (bytes, bytearray)) and len(value) == 32:
        return encode(bytes(value))
    if isinstance(value, (list, tuple)):
        if len(value) == 1:
            return normalize(value[0])
        if len(value) == 32 and all(isinstance(x, int) for x in value):
            return encode(bytes(value))
    raise ValueError(f"cannot interpret account id: {value!r}")


def is_module_account(address: str) -> bool:
    """Pallet-owned account (public key starts with b"modl"), e.g. Subtensor's per-subnet
    accounts that staking/unstaking moves TAO through. Never a user's wallet."""
    try:
        return decode(address)[:4] == b"modl"
    except ValueError:
        return False


def is_valid(address: str) -> bool:
    try:
        decode(address)
        return True
    except ValueError:
        return False


def short(address: str | None) -> str:
    if not address:
        return "?"
    return f"{address[:6]}…{address[-4:]}"
