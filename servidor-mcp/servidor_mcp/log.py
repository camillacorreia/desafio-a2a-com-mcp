"""Uma linha no stderr por request recebido, antes de qualquer validação do SDK.

Fica na borda ASGI, e não num middleware do SDK, para registrar também o que o
transporte recusa antes de chegar a um handler (`_meta` incompleto, header que
não bate com o corpo). Não usa o logging do MCP, que é depreciado.

Formato, pensado para grep:

    mcp method=tools/call id=7 name=reservar_sala traceparent=00-... http=200 resultado=input_required
"""

from __future__ import annotations

import json
import sys
from datetime import datetime

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_LIMITE_RESPOSTA = 64 * 1024


def _campos_do_pedido(corpo: bytes) -> dict[str, str]:
    try:
        mensagem = json.loads(corpo)
    except (ValueError, UnicodeDecodeError):
        return {"method": "<json-invalido>"}
    if not isinstance(mensagem, dict):
        return {"method": "<nao-objeto>"}
    params = mensagem.get("params") if isinstance(mensagem.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    campos = {"method": str(mensagem.get("method", "-")), "id": json.dumps(mensagem.get("id"))}
    nome = params.get("name") or params.get("uri")
    if nome:
        campos["name"] = str(nome)
    campos["traceparent"] = str(meta.get("traceparent", "-"))
    if "requestState" in params:
        campos["retry"] = "sim"
    return campos


def _resultado(corpo: bytes) -> str:
    try:
        resposta = json.loads(corpo)
    except (ValueError, UnicodeDecodeError):
        return "-"
    if not isinstance(resposta, dict):
        return "-"
    if "error" in resposta:
        return f"erro={resposta['error'].get('code')}"
    resultado = resposta.get("result") or {}
    tipo = resultado.get("resultType", "complete")
    if resultado.get("isError"):
        return f"{tipo},isError"
    return str(tipo)


class LogDeRequests:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        recebidas: list[Message] = []
        corpo = b""
        while True:
            mensagem = await receive()
            recebidas.append(mensagem)
            corpo += mensagem.get("body", b"")
            if mensagem["type"] != "http.request" or not mensagem.get("more_body", False):
                break

        async def reenviar() -> Message:
            if recebidas:
                return recebidas.pop(0)
            return await receive()

        status = "-"
        resposta = b""

        async def capturar(mensagem: Message) -> None:
            nonlocal status, resposta
            if mensagem["type"] == "http.response.start":
                status = str(mensagem["status"])
            elif mensagem["type"] == "http.response.body" and len(resposta) < _LIMITE_RESPOSTA:
                resposta += mensagem.get("body", b"")
            await send(mensagem)

        try:
            await self.app(scope, reenviar, capturar)
        finally:
            campos = _campos_do_pedido(corpo)
            texto = " ".join(f"{k}={v}" for k, v in campos.items())
            agora = datetime.now().isoformat(timespec="milliseconds")
            sys.stderr.write(f"{agora} mcp {texto} http={status} resultado={_resultado(resposta)}\n")
            sys.stderr.flush()
