"""SOAP 1.1 adapter: document-style request/response over the same operation handlers.

The first child element of <Body> selects the operation (``soap_operation`` in
the spec); its child elements become the request body. Results come back as
``<{Operation}Response>``; twin errors come back as a SOAP Fault with HTTP 500
and the twin error code in ``<detail><code>``.

Limits: no WSDL is generated or validated, no WS-Security, no MTOM/attachments,
no SOAP 1.2 envelope, and SOAPAction is not required. Verified with the bundled
client and replay only, not with a vendor SOAP stack.
"""
from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

ENV_NS = "http://schemas.xmlsoap.org/soap/envelope/"


class SoapError(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _to_obj(el: ET.Element) -> Any:
    kids = list(el)
    if not kids:
        return (el.text or "").strip()
    return {_local(k.tag): _to_obj(k) for k in kids}


def parse_envelope(raw: bytes) -> tuple[str, dict]:
    text = raw.decode("utf-8", "replace")
    if "<!DOCTYPE" in text or "<!ENTITY" in text:
        raise SoapError("DTD / entity declarations are rejected")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SoapError(f"not well-formed XML: {exc}") from exc
    if _local(root.tag) != "Envelope":
        raise SoapError("root element must be a SOAP Envelope")
    body = next((c for c in root if _local(c.tag) == "Body"), None)
    if body is None or not len(body):
        raise SoapError("SOAP Body is empty")
    op = body[0]
    params = _to_obj(op)
    return _local(op.tag), params if isinstance(params, dict) else {}


def _xml(name: str, value: Any) -> str:
    if isinstance(value, dict):
        inner = "".join(_xml(k, v) for k, v in value.items() if not str(k).startswith("_"))
        return f"<{name}>{inner}</{name}>"
    if isinstance(value, list):
        return f"<{name}>{''.join(_xml('item', v) for v in value)}</{name}>"
    return f"<{name}>{escape('' if value is None else str(value))}</{name}>"


def envelope(element: str, fields: Any, namespace: str) -> str:
    inner = "".join(_xml(k, v) for k, v in (fields or {}).items() if not str(k).startswith("_")) if isinstance(fields, dict) else ""
    return (f'<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="{ENV_NS}"><soap:Body>'
            f'<{element} xmlns="{escape(namespace)}">{inner}</{element}></soap:Body></soap:Envelope>')


def fault(code: str, message: str, client: bool, trace_id: str | None = None) -> str:
    return (f'<?xml version="1.0" encoding="utf-8"?><soap:Envelope xmlns:soap="{ENV_NS}"><soap:Body><soap:Fault>'
            f'<faultcode>soap:{"Client" if client else "Server"}</faultcode><faultstring>{escape(message)}</faultstring>'
            f'<detail><code>{escape(code)}</code><trace_id>{escape(trace_id or "")}</trace_id></detail>'
            f'</soap:Fault></soap:Body></soap:Envelope>')


def parse_response(text: str) -> dict:
    """Client side: {'ok', 'fields', 'fault_code', 'fault_string'}."""
    root = ET.fromstring(text.encode("utf-8"))
    body = next(c for c in root if _local(c.tag) == "Body")
    first = body[0]
    if _local(first.tag) == "Fault":
        obj = _to_obj(first)
        return {"ok": False, "fields": {}, "fault_code": (obj.get("detail") or {}).get("code"),
                "fault_string": obj.get("faultstring")}
    obj = _to_obj(first)
    return {"ok": True, "fields": obj if isinstance(obj, dict) else {}, "fault_code": None, "fault_string": None}
