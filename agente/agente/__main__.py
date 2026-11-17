"""Sobe o agente: `python -m agente` ou `agente`.

Variáveis de ambiente:

- `AGENTE_HOST` (padrão 127.0.0.1) e `AGENTE_PORT` (padrão 7300).
- `AGENTE_URL_PUBLICA` (opcional): URL do endpoint A2A anunciada no Agent Card.
  Padrão: `http://<AGENTE_HOST>:<AGENTE_PORT>/a2a`.
- `MCP_URL` (padrão http://127.0.0.1:7301/mcp): o servidor MCP, sempre por HTTP.
"""

from __future__ import annotations

import os
import sys

import uvicorn

from agente.a2a import criar_app


def main() -> None:
    host = os.environ.get("AGENTE_HOST", "127.0.0.1")
    porta = int(os.environ.get("AGENTE_PORT", "7300"))
    url_mcp = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")
    url_a2a = os.environ.get("AGENTE_URL_PUBLICA", f"http://{host}:{porta}/a2a")
    print(f"agente A2A em {url_a2a}, servidor MCP em {url_mcp}", file=sys.stderr, flush=True)
    uvicorn.run(criar_app(url_mcp, url_a2a), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
