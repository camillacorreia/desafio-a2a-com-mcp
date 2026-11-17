"""Sobe o servidor MCP em Streamable HTTP: `python -m servidor_mcp` ou `servidor-mcp`.

Variáveis de ambiente:

- `REQUEST_STATE_SECRET` (obrigatória): chave de integridade do requestState,
  com no mínimo 32 bytes de aleatoriedade. Sem ela o processo não sobe.
- `MCP_HOST` (padrão 127.0.0.1) e `MCP_PORT` (padrão 7301).
- `DADOS_DIR` (opcional): pasta com salas.json, reservas.json e politica-de-uso.md.
"""

from __future__ import annotations

import os
import re
import sys

import uvicorn

from servidor_mcp.dominio import CentralDeSalas, diretorio_dados
from servidor_mcp.log import LogDeRequests
from servidor_mcp.servidor import criar_servidor

BYTES_MINIMOS = 32
_HEX = re.compile(r"^(?:[0-9a-fA-F]{2})+$")
_COMO_GERAR = 'python3 -c "import secrets; print(secrets.token_hex(32))"'


def ler_segredo() -> bytes:
    """Lê `REQUEST_STATE_SECRET` e confere a entropia mínima.

    Um valor em hexadecimal (o formato de `secrets.token_hex`) vale pelos bytes
    que codifica, então `token_hex(32)` dá 32 bytes; qualquer outro texto vale
    pelos próprios bytes. Não existe valor padrão: sem segredo, o processo para.
    """
    bruto = os.environ.get("REQUEST_STATE_SECRET", "").strip()
    if not bruto:
        raise SystemExit(
            "REQUEST_STATE_SECRET nao definida. Gere uma chave com\n"
            f"    export REQUEST_STATE_SECRET=$({_COMO_GERAR})\n"
            "e suba o servidor de novo."
        )
    segredo = bytes.fromhex(bruto) if _HEX.match(bruto) else bruto.encode()
    if len(segredo) < BYTES_MINIMOS:
        raise SystemExit(
            f"REQUEST_STATE_SECRET tem {len(segredo)} bytes de aleatoriedade; o minimo e {BYTES_MINIMOS}. "
            f"Gere uma chave com: {_COMO_GERAR}"
        )
    return segredo


def main() -> None:
    segredo = ler_segredo()
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    porta = int(os.environ.get("MCP_PORT", "7301"))
    dados = diretorio_dados()

    mcp = criar_servidor(segredo, CentralDeSalas(dados))
    app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        host=host,
    )
    print(
        f"servidor MCP central-de-salas em http://{host}:{porta}/mcp (dados em {dados})",
        file=sys.stderr,
        flush=True,
    )
    uvicorn.run(LogDeRequests(app), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
