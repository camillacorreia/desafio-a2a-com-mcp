"""O lado de fora do agente: servidor A2A v1.0, binding JSON-RPC 2.0 sobre HTTP.

- `GET /.well-known/agent-card.json`: o Agent Card.
- `POST /a2a`: `SendMessage` e `GetTask`.
"""

from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from agente.host_mcp import HostMCP
from agente.tarefas import INVALID_PARAMS, Agente, ErroA2A
from agente.trace import trace_id_de

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INTERNAL_ERROR = -32603

EXEMPLO = (
    "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
)


def agent_card(url_a2a: str) -> dict[str, Any]:
    return {
        "name": "Central de Salas",
        "description": "Reserva salas de reuniao da Hill Valley Tech.",
        "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
        "version": "1.0.0",
        "supportedInterfaces": [{"url": url_a2a, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}],
        "capabilities": {"streaming": False, "pushNotifications": False, "extendedAgentCard": False},
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "reservar-sala",
                "name": "Reservar sala",
                "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
                "tags": ["salas", "agenda"],
                "inputModes": ["text/plain"],
                "outputModes": ["text/plain"],
                "examples": [EXEMPLO],
            }
        ],
    }


def _erro(id_rpc: Any, codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_rpc, "error": {"code": codigo, "message": mensagem}})


def _ok(id_rpc: Any, resultado: dict[str, Any]) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_rpc, "result": resultado})


def criar_app(url_mcp: str, url_a2a: str) -> Starlette:
    host = HostMCP(url_mcp)
    agente = Agente(host)
    card = agent_card(url_a2a)

    async def servir_card(_: Request) -> JSONResponse:
        return JSONResponse(card)

    async def send_message(params: dict[str, Any], trace_id: str | None) -> dict[str, Any]:
        mensagem = params.get("message")
        if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
            raise ErroA2A(INVALID_PARAMS, "params.message com parts e obrigatorio")
        if mensagem.get("role") not in (None, "ROLE_USER"):
            raise ErroA2A(INVALID_PARAMS, "a mensagem enviada ao agente deve ter role ROLE_USER")
        tarefa = await agente.receber(mensagem, trace_id)
        return {"task": tarefa.como_a2a()}

    async def get_task(params: dict[str, Any]) -> dict[str, Any]:
        task_id = params.get("id")
        if not isinstance(task_id, str):
            raise ErroA2A(INVALID_PARAMS, "params.id e obrigatorio")
        limite = params.get("historyLength")
        return {"task": agente.obter(task_id).como_a2a(limite if isinstance(limite, int) else None)}

    async def a2a(request: Request) -> JSONResponse:
        try:
            corpo = json.loads(await request.body())
        except ValueError:
            return _erro(None, PARSE_ERROR, "JSON invalido")
        if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
            return _erro(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Request JSON-RPC invalido")
        id_rpc = corpo.get("id")
        metodo = corpo["method"]
        params = corpo.get("params") if isinstance(corpo.get("params"), dict) else {}
        trace_id = trace_id_de(request.headers.get("traceparent"))
        sys.stderr.write(f"agente a2a method={metodo} id={json.dumps(id_rpc)} trace-id={trace_id or '-'}\n")
        try:
            if metodo == "SendMessage":
                return _ok(id_rpc, await send_message(params, trace_id))
            if metodo == "GetTask":
                return _ok(id_rpc, await get_task(params))
            return _erro(id_rpc, METHOD_NOT_FOUND, f"Metodo nao suportado: {metodo}")
        except ErroA2A as erro:
            sys.stderr.write(f"agente a2a erro={erro.codigo} mensagem={erro.mensagem!r}\n")
            return _erro(id_rpc, erro.codigo, erro.mensagem)

    @asynccontextmanager
    async def ciclo_de_vida(_: Starlette) -> AsyncIterator[None]:
        async with AsyncExitStack() as pilha:
            await host.abrir(pilha)
            yield

    return Starlette(
        routes=[
            Route("/.well-known/agent-card.json", servir_card, methods=["GET"]),
            Route("/a2a", a2a, methods=["POST"]),
        ],
        lifespan=ciclo_de_vida,
    )
