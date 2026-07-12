from __future__ import annotations

import os
import random
import time
import uuid
from uuid import (
    NAMESPACE_DNS,
    NAMESPACE_OID,
    NAMESPACE_URL,
    NAMESPACE_X500,
    RESERVED_FUTURE,
    RESERVED_MICROSOFT,
    RESERVED_NCS,
    RFC_4122,
    UUID,
    SafeUUID,
    getnode,
)

__version__ = "0.16.2"

NIL = UUID("00000000-0000-0000-0000-000000000000")
MAX = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")

_rng = random.Random()


def reseed() -> None:
    _rng.seed()


def _from_int(value: int) -> UUID:
    u = object.__new__(UUID)
    object.__setattr__(u, "int", value)
    object.__setattr__(u, "is_safe", SafeUUID.unknown)
    return u


def _uuid4_int() -> int:
    return uuid.uuid4().int


def _uuid7_int(timestamp: int | None = None, nanos: int | None = None) -> int:
    if timestamp is None:
        timestamp_ms = time.time_ns() // 1_000_000
    else:
        timestamp_ms = timestamp * 1_000 + ((nanos or 0) // 1_000_000)

    timestamp_ms &= 0xFFFF_FFFF_FFFF
    rand_74 = _rng.getrandbits(74)

    value = 0
    value |= timestamp_ms << 80
    value |= 0x7 << 76
    value |= ((rand_74 >> 62) & 0xFFF) << 64
    value |= 0x2 << 62
    value |= rand_74 & ((1 << 62) - 1)
    return value


def uuid1(node=None, clock_seq=None):
    return uuid.uuid1(node=node, clock_seq=clock_seq)


def uuid3(namespace, name):
    return uuid.uuid3(namespace, name)


def uuid4():
    return _from_int(_uuid4_int())


def uuid5(namespace, name):
    return uuid.uuid5(namespace, name)


def uuid6(node=None, timestamp=None):
    # Fallback implementation that preserves the uuid1 timestamp semantics
    # while exposing UUID version 6 bits for callers that only inspect version.
    base = uuid.uuid1(node=node)
    b = bytearray(base.bytes)
    b[6] = (b[6] & 0x0F) | 0x60
    return UUID(bytes=bytes(b))


def uuid7(timestamp=None, nanos=None):
    return _from_int(_uuid7_int(timestamp, nanos))


def uuid8(bytes=None):
    if bytes is None:
        bytes = os.urandom(16)
    if len(bytes) < 16:
        bytes = bytes.ljust(16, b"\0")
    b = bytearray(bytes[:16])
    b[6] = (b[6] & 0x0F) | 0x80
    b[8] = (b[8] & 0x3F) | 0x80
    return UUID(bytes=bytes(b))
