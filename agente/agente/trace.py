"""W3C trace context: o trace-id que chega no header A2A segue até o `_meta` MCP."""

from __future__ import annotations

import re
import secrets

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-([0-9a-f]{16})-[0-9a-f]{2}$")


def trace_id_de(header: str | None) -> str | None:
    """Extrai o trace-id de um `traceparent` válido, ou None."""
    if not header:
        return None
    casamento = _TRACEPARENT.match(header.strip().lower())
    if casamento is None or casamento.group(1) == "0" * 32 or casamento.group(2) == "0" * 16:
        return None
    return casamento.group(1)


def novo_trace_id() -> str:
    return secrets.token_hex(16)


def traceparent(trace_id: str) -> str:
    """Um span novo dentro do mesmo trace: o span-id muda a cada request MCP, o trace-id não."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"
