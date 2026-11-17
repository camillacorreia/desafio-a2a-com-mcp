"""O lado de dentro do agente: um host MCP com um cliente do SDK oficial.

Usa `ClientSession` do pacote `mcp` sobre `streamable_http_client`, sempre com
`allow_input_required=True`, para que o `input_required` volte cru para o
agente em vez de ser respondido por um callback de elicitation. Nenhum
callback de elicitation é registrado: quem responde a pergunta é o cliente
A2A, numa outra mensagem, muito depois.

Cada request leva no `_meta` o `traceparent` da Task. O SDK carimba
`protocolVersion`, `clientInfo` e `clientCapabilities` no `_meta` e os
headers `MCP-Protocol-Version`, `Mcp-Method` e `Mcp-Name`, e gera um id de
JSON-RPC novo por request, então o retry nunca repete o id do pedido inicial.
"""

from __future__ import annotations

import sys
from contextlib import AsyncExitStack
from typing import Any

import anyio
from mcp import types
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

from agente.trace import traceparent

NOME_CLIENTE = "agente-central-de-salas"
VERSAO_CLIENTE = "1.0.0"
URI_POLITICA = "politica://uso"


class SessaoElicitacaoForm(ClientSession):
    """`ClientSession` que declara só elicitation em form mode.

    O SDK 2.3.0 só anuncia elicitation quando existe um `elicitation_callback`,
    e nesse caso anuncia form e url juntos (`ClientSession._build_capabilities`).
    O agente não quer callback (o `input_required` precisa chegar cru) e não
    trata url mode, então a capability é declarada aqui, exatamente como o
    desafio pede: `{"elicitation": {"form": {}}}`.
    """

    def _build_capabilities(self, version: str) -> types.ClientCapabilities:
        base = super()._build_capabilities(version)
        forma = types.ElicitationCapability(form=types.FormElicitationCapability())
        return base.model_copy(update={"elicitation": forma})


def _log(texto: str) -> None:
    sys.stderr.write(f"agente {texto}\n")
    sys.stderr.flush()


class HostMCP:
    def __init__(self, url: str) -> None:
        self.url = url
        self._sessao: SessaoElicitacaoForm | None = None
        self._negociado = False
        self._trava = anyio.Lock()

    async def abrir(self, pilha: AsyncExitStack) -> None:
        leitura, escrita = await pilha.enter_async_context(streamable_http_client(self.url))
        self._sessao = await pilha.enter_async_context(
            SessaoElicitacaoForm(
                leitura,
                escrita,
                client_info=types.Implementation(name=NOME_CLIENTE, version=VERSAO_CLIENTE),
            )
        )

    async def _sessao_pronta(self) -> SessaoElicitacaoForm:
        """Negocia a versão com `server/discover` na primeira vez que o agente precisa do servidor.

        Fica preguiçoso para o agente subir mesmo com o servidor MCP ainda fora do ar.
        """
        assert self._sessao is not None, "HostMCP.abrir() nao foi chamado"
        async with self._trava:
            if not self._negociado:
                await self._sessao.discover()
                self._negociado = True
                _log(f"mcp negociado protocolo={self._sessao.protocol_version} servidor={self.url}")
        return self._sessao

    @staticmethod
    def _meta(trace_id: str) -> types.RequestParamsMeta:
        return {"traceparent": traceparent(trace_id)}

    async def descobrir_tools(self, trace_id: str) -> dict[str, types.Tool]:
        """`tools/list` em runtime: o agente não carrega lista fixa de tools."""
        sessao = await self._sessao_pronta()
        meta = self._meta(trace_id)
        resultado = await sessao.list_tools(params=types.PaginatedRequestParams(_meta=meta))
        _log(f"mcp tools/list traceparent={meta['traceparent']} tools={[t.name for t in resultado.tools]}")
        return {t.name: t for t in resultado.tools}

    async def versao_da_politica(self, trace_id: str) -> str:
        """Lê `politica://uso` e devolve a versão declarada na primeira linha (`versao: ...`)."""
        sessao = await self._sessao_pronta()
        meta = self._meta(trace_id)
        resultado = await sessao.read_resource(URI_POLITICA, meta=meta)
        _log(f"mcp resources/read uri={URI_POLITICA} traceparent={meta['traceparent']}")
        assert isinstance(resultado, types.ReadResourceResult)
        texto = next((c.text for c in resultado.contents if isinstance(c, types.TextResourceContents)), "")
        primeira = texto.splitlines()[0] if texto else ""
        chave, _, valor = primeira.partition(":")
        if chave.strip() != "versao" or not valor.strip():
            raise ValueError(f"o resource {URI_POLITICA} nao declara a versao na primeira linha")
        return valor.strip()

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace_id: str,
        *,
        respostas: types.InputResponses | None = None,
        request_state: str | None = None,
    ) -> types.CallToolResult | types.InputRequiredResult:
        """`tools/call` que devolve o `input_required` cru, sem resolver nada sozinho.

        No retry, `request_state` é o valor recebido do servidor, ecoado byte a byte.
        """
        sessao = await self._sessao_pronta()
        meta = self._meta(trace_id)
        resultado = await sessao.call_tool(
            nome,
            argumentos,
            input_responses=respostas,
            request_state=request_state,
            meta=meta,
            allow_input_required=True,
        )
        tipo = "input_required" if isinstance(resultado, types.InputRequiredResult) else "complete"
        retry = " retry=sim" if request_state is not None else ""
        _log(f"mcp tools/call name={nome}{retry} traceparent={meta['traceparent']} resultado={tipo}")
        return resultado
