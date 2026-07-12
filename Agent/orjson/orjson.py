from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import date, datetime, time
from decimal import Decimal
from enum import Enum
from typing import Any

OPT_APPEND_NEWLINE = 1 << 0
OPT_INDENT_2 = 1 << 1
OPT_NAIVE_UTC = 1 << 2
OPT_NON_STR_KEYS = 1 << 3
OPT_OMIT_MICROSECONDS = 1 << 4
OPT_PASSTHROUGH_DATACLASS = 1 << 5
OPT_PASSTHROUGH_DATETIME = 1 << 6
OPT_PASSTHROUGH_SUBCLASS = 1 << 7
OPT_PASSTHROUGH_TUPLE = 1 << 8
OPT_PASSTHROUGH_UUID = 1 << 9
OPT_REPLACE_SURROGATES = 1 << 10
OPT_SERIALIZE_DATACLASS = 1 << 11
OPT_SERIALIZE_NUMPY = 1 << 12
OPT_SERIALIZE_PYDANTIC = 1 << 13
OPT_SERIALIZE_UUID = 1 << 14
OPT_SORT_KEYS = 1 << 15
OPT_STRICT_INTEGER = 1 << 16
OPT_UTC_Z = 1 << 17


class JSONEncodeError(TypeError):
    pass


JSONDecodeError = json.JSONDecodeError


class Fragment:
    def __init__(self, payloadb: bytes):
        self.payloadb = payloadb


def _default_encoder(obj: Any, default: Any, option: int) -> Any:
    if isinstance(obj, Fragment):
        return json.loads(obj.payloadb)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        if option & OPT_PASSTHROUGH_DATACLASS:
            return default(obj) if default is not None else dataclasses.asdict(obj)
        return dataclasses.asdict(obj)
    if isinstance(obj, uuid.UUID):
        if option & OPT_PASSTHROUGH_UUID and default is not None:
            return default(obj)
        return str(obj)
    if isinstance(obj, (datetime, date, time)):
        if option & OPT_PASSTHROUGH_DATETIME and default is not None:
            return default(obj)
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (set, frozenset)):
        return list(obj)
    if hasattr(obj, "tolist"):
        try:
            return obj.tolist()
        except Exception:
            pass
    if default is not None:
        return default(obj)
    raise JSONEncodeError(f"Type is not JSON serializable: {type(obj).__name__}")


def dumps(
    obj: Any,
    /,
    *,
    default: Any | None = None,
    option: int | None = None,
) -> bytes:
    option = option or 0
    if isinstance(obj, Fragment):
        return obj.payloadb

    try:
        text = json.dumps(
            obj,
            default=lambda value: _default_encoder(value, default, option),
            ensure_ascii=False,
            sort_keys=bool(option & OPT_SORT_KEYS),
            separators=(",", ":") if not (option & OPT_INDENT_2) else None,
            indent=2 if option & OPT_INDENT_2 else None,
        )
        if option & OPT_APPEND_NEWLINE:
            text += "\n"
        return text.encode("utf-8")
    except json.JSONDecodeError as exc:
        raise JSONEncodeError(str(exc)) from exc
    except TypeError as exc:
        raise JSONEncodeError(str(exc)) from exc


def loads(payload: bytes | bytearray | memoryview | str, /) -> Any:
    if isinstance(payload, memoryview):
        payload = payload.tobytes()
    if isinstance(payload, (bytes, bytearray)):
        payload = payload.decode("utf-8")
    return json.loads(payload)
