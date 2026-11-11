"""OCPP 1.6J message framing - the smallest honest implementation of the wire format.

OCPP 1.6 has two bindings: SOAP/XML (`1.6S`) and JSON over WebSocket (`1.6J`). This is the
`J` binding, because it is the one that is actually deployed and the one a test double can
implement without an XML stack.

Every OCPP frame is a JSON array whose first element is the *message type id*:

    CALL          [2, uniqueId, action, payload]                 charge point -> CSMS, and back
    CALLRESULT    [3, uniqueId, payload]                         the reply to a CALL
    CALLERROR     [4, uniqueId, errorCode, errorDescription, {}] a failed CALL

The `uniqueId` is a per-connection correlation id; OCPP has no trace context of any kind.
That absence is worth noticing (docs/standards.md): a transaction is correlated north-south
by `transactionId`, not by a W3C `traceparent`, so the charge point and the CSMS produce two
independent traces unless something outside the protocol joins them.
"""

from __future__ import annotations

import json
from typing import Any

CALL = 2
CALL_RESULT = 3
CALL_ERROR = 4

SUBPROTOCOL = "ocpp1.6"


class OCPPProtocolError(ValueError):
    """A frame that does not parse as OCPP 1.6J."""


def call(unique_id: str, action: str, payload: dict[str, Any]) -> str:
    return json.dumps([CALL, unique_id, action, payload], separators=(",", ":"))


def result(unique_id: str, payload: dict[str, Any]) -> str:
    return json.dumps([CALL_RESULT, unique_id, payload], separators=(",", ":"))


def error(unique_id: str, code: str, description: str,
          details: dict[str, Any] | None = None) -> str:
    return json.dumps([CALL_ERROR, unique_id, code, description, details or {}],
                      separators=(",", ":"))


def parse(text: str) -> tuple[int, str, Any, Any]:
    """Parse one frame. Returns (message_type_id, unique_id, action_or_payload, payload_or_none).

    The third element is the action for a CALL and the payload for a CALLRESULT/CALLERROR;
    the fourth is the payload for a CALL, and the error details for a CALLERROR. Callers
    that only care about the common case can treat ``kind = parse(text)[0]``.
    """
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as exc:
        raise OCPPProtocolError(f"not JSON: {exc}") from exc
    if not isinstance(msg, list) or len(msg) < 3:
        raise OCPPProtocolError(f"frame is not a 3+ element array: {msg!r}")
    mtype = msg[0]
    if mtype == CALL:
        if len(msg) != 4:
            raise OCPPProtocolError(f"CALL must have 4 elements, got {len(msg)}")
        return mtype, msg[1], msg[2], msg[3]
    if mtype == CALL_RESULT:
        return mtype, msg[1], msg[2], None
    if mtype == CALL_ERROR:
        details = msg[4] if len(msg) > 4 else {}
        return mtype, msg[1], msg[2], {"description": msg[3], "details": details}
    raise OCPPProtocolError(f"unknown message type id {mtype!r}")
