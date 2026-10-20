"""Minimal protobuf wire-format reader (no protobuf library, no .proto files needed).

Used to pull a few known fields out of Rajant BC|Commander snapshots. Callers ask for
fields by number path; nothing else in the message is decoded or kept.
"""

from __future__ import annotations

import struct


class WireError(ValueError):
    pass


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise WireError("truncated varint")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7
        if shift > 70:
            raise WireError("varint too long")


def iter_fields(buf: bytes):
    """Yield (field number, wire type, value). Length-delimited values stay raw bytes."""
    i, n = 0, len(buf)
    while i < n:
        key, i = _varint(buf, i)
        num, wt = key >> 3, key & 7
        if wt == 0:
            val, i = _varint(buf, i)
        elif wt == 1:
            val, i = buf[i:i + 8], i + 8
        elif wt == 2:
            ln, i = _varint(buf, i)
            if i + ln > n:
                raise WireError("truncated field")
            val, i = buf[i:i + ln], i + ln
        elif wt == 5:
            val, i = buf[i:i + 4], i + 4
        else:
            raise WireError(f"unsupported wire type {wt}")
        yield num, wt, val


def all_of(buf: bytes | None, num: int) -> list:
    if not buf:
        return []
    return [v for f, _wt, v in iter_fields(buf) if f == num]


def first(buf: bytes | None, *path: int):
    """Follow a field-number path (first occurrence at each level)."""
    cur = buf
    for num in path:
        if not isinstance(cur, (bytes, bytearray)):
            return None
        vals = all_of(cur, num)
        if not vals:
            return None
        cur = vals[0]
    return cur


def text(value) -> str | None:
    if isinstance(value, (bytes, bytearray)):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return None if value is None else str(value)


def double(value) -> float | None:
    if isinstance(value, (bytes, bytearray)) and len(value) == 8:
        return struct.unpack("<d", value)[0]
    if isinstance(value, (bytes, bytearray)) and len(value) == 4:
        return struct.unpack("<f", value)[0]
    return None


# -- writer, used by tests to build synthetic snapshots ----------------------
def _enc_varint(v: int) -> bytes:
    out = bytearray()
    while True:
        b = v & 0x7F
        v >>= 7
        if v:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field(num: int, value) -> bytes:
    if isinstance(value, bool) or isinstance(value, int):
        return _enc_varint(num << 3) + _enc_varint(int(value))
    if isinstance(value, float):
        return _enc_varint((num << 3) | 1) + struct.pack("<d", value)
    data = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    return _enc_varint((num << 3) | 2) + _enc_varint(len(data)) + data


def message(*parts: bytes) -> bytes:
    return b"".join(parts)
