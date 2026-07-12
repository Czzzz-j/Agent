from __future__ import annotations

import time
import uuid
import warnings

import uuid_utils
from uuid_utils import _uuid4_int, _uuid7_int

__version__ = uuid_utils.__version__


def _from_int(n: int) -> uuid.UUID:
    u = object.__new__(uuid.UUID)
    object.__setattr__(u, "int", n)
    object.__setattr__(u, "is_safe", uuid.SafeUUID.unknown)
    return u


def uuid1(node=None, clock_seq=None):
    return _from_int(uuid_utils.uuid1(node, clock_seq).int)


def uuid3(namespace, name):
    namespace = uuid_utils.UUID(namespace.hex) if namespace else namespace
    return _from_int(uuid_utils.uuid3(namespace, name).int)


def uuid4():
    return _from_int(_uuid4_int())


def uuid5(namespace, name):
    namespace = uuid_utils.UUID(namespace.hex) if namespace else namespace
    return _from_int(uuid_utils.uuid5(namespace, name).int)


def uuid6(node=None, timestamp=None):
    return _from_int(uuid_utils.uuid6(node, timestamp).int)


def uuid7(timestamp=None, nanos=None):
    return _from_int(_uuid7_int(timestamp, nanos))


def uuid8(bytes):
    return _from_int(uuid_utils.uuid8(bytes).int)
