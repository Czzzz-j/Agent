from __future__ import annotations

import pickle
from dataclasses import dataclass

__version__ = "0.3.0"

OPT_DATETIME_AS_TIMESTAMP_EXT = 0
OPT_NAIVE_UTC = 0
OPT_NON_STR_KEYS = 0
OPT_OMIT_MICROSECONDS = 0
OPT_PASSTHROUGH_BIG_INT = 0
OPT_PASSTHROUGH_DATACLASS = 0
OPT_PASSTHROUGH_DATETIME = 0
OPT_PASSTHROUGH_ENUM = 0
OPT_PASSTHROUGH_SUBCLASS = 0
OPT_PASSTHROUGH_TUPLE = 0
OPT_PASSTHROUGH_UUID = 0
OPT_REPLACE_SURROGATES = 0
OPT_SERIALIZE_NUMPY = 0
OPT_SERIALIZE_PYDANTIC = 0
OPT_SORT_KEYS = 0
OPT_UTC_Z = 0


class MsgpackEncodeError(TypeError):
    pass


class MsgpackDecodeError(ValueError):
    pass


@dataclass
class Ext:
    code: int
    data: bytes


def packb(obj, *_, default=None, **__) -> bytes:
    try:
        return pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    except Exception as exc:
        if default is not None:
            return pickle.dumps(default(obj), protocol=pickle.HIGHEST_PROTOCOL)
        raise MsgpackEncodeError(str(exc)) from exc


def unpackb(data: bytes, *_, **__):
    try:
        return pickle.loads(data)
    except Exception as exc:
        raise MsgpackDecodeError(str(exc)) from exc
