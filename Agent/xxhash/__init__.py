from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

VERSION = "3.7.0"
XXHASH_VERSION = "0.8.0"


def _coerce_bytes(data: Any) -> bytes:
    if data is None:
        return b""
    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if isinstance(data, memoryview):
        return data.tobytes()
    if isinstance(data, str):
        return data.encode("utf-8")
    return str(data).encode("utf-8")


@dataclass
class _HashState:
    data: bytearray
    digest_size: int
    seed: int

    def digest(self) -> bytes:
        hasher = hashlib.blake2b(
            bytes(self.data),
            digest_size=self.digest_size,
            person=self.seed.to_bytes(8, "little", signed=False),
        )
        return hasher.digest()

    def hexdigest(self) -> str:
        return self.digest().hex()

    def intdigest(self) -> int:
        return int.from_bytes(self.digest(), "big", signed=False)

    def update(self, data: Any) -> None:
        self.data.extend(_coerce_bytes(data))

    def copy(self) -> "_HashState":
        return _HashState(bytearray(self.data), self.digest_size, self.seed)

    def reset(self) -> None:
        self.data.clear()


def _new_state(data: Any = b"", *, digest_size: int, seed: int = 0) -> _HashState:
    return _HashState(bytearray(_coerce_bytes(data)), digest_size, seed)


def xxh32(data: Any = b"", seed: int = 0) -> _HashState:
    return _new_state(data, digest_size=4, seed=seed)


def xxh64(data: Any = b"", seed: int = 0) -> _HashState:
    return _new_state(data, digest_size=8, seed=seed)


def xxh64_intdigest(data: Any = b"", seed: int = 0) -> int:
    return xxh64(data, seed=seed).intdigest()


def xxh3_64(data: Any = b"", seed: int = 0) -> _HashState:
    return _new_state(data, digest_size=8, seed=seed)


def xxh3_128(data: Any = b"", seed: int = 0) -> _HashState:
    return _new_state(data, digest_size=16, seed=seed)


def xxh3_128_hexdigest(data: Any = b"", seed: int = 0) -> str:
    return xxh3_128(data, seed=seed).hexdigest()


def xxh3_64_hexdigest(data: Any = b"", seed: int = 0) -> str:
    return xxh3_64(data, seed=seed).hexdigest()


__all__ = [
    "VERSION",
    "XXHASH_VERSION",
    "xxh32",
    "xxh64",
    "xxh64_intdigest",
    "xxh3_64",
    "xxh3_64_hexdigest",
    "xxh3_128",
    "xxh3_128_hexdigest",
]
