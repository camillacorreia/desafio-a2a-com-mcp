"""Servidor MCP `central-de-salas`: três tools, um resource e o MRTR da reserva.

O MRTR é conduzido pelo corpo de `reservar_sala`, que devolve um
`InputRequiredResult` do SDK quando a sala está ocupada. O `requestState` desse
resultado carrega o pedido inteiro (sala, intervalo, responsável) e as
alternativas oferecidas, e é selado pelo `RequestStateBoundary` do SDK
(AES-256-GCM, chave derivada de `REQUEST_STATE_SECRET`), com expiração e vínculo
ao método, à tool e ao digest dos argumentos. No retry o SDK verifica o selo e
entrega o texto puro em `Context.request_state`; o servidor não guarda nada
entre a pergunta e o retry.

Por que não um resolver (`Resolve(...)` com `Elicit`): o estado que o SDK sela
para resolvers guarda só as respostas e o digest da pergunta, e a resposta só
vale se o resolver, rodando de novo com os dados atuais, renderizar exatamente
a mesma pergunta. Depois de um restart as reservas em memória voltam ao
arquivo, a lista de alternativas muda e a resposta válida era descartada.
Selando as alternativas oferecidas, a escolha é conferida contra o que foi
perguntado, e o servidor só pergunta de novo se a sala escolhida deixou de
estar livre.
"""

from __future__ import annotations

import json
from typing import Literal

from mcp import types
from mcp.server.elicitation import render_elicitation_schema
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError
from mcp_types.version import is_version_at_least
from pydantic import BaseModel, Field, create_model

from servidor_mcp.dominio import ERRO_SEM_ALTERNATIVA, CentralDeSalas, Intervalo, RegraViolada

NOME = "central-de-salas"
VERSAO = "1.0.0"
URI_POLITICA = "politica://uso"
MENSAGEM_ESCOLHA = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."
# Chave do único pedido em `inputRequests`; o cliente devolve a mesma em `inputResponses`.
CHAVE_ESCOLHA = "escolha_de_sala"
VERSAO_ESTADO = 1
PRIMEIRA_VERSAO_MRTR = "2026-07-28"

# Vida do requestState: 10 minutos, dentro da faixa de 5 a 30 pedida pelo desafio.
TTL_REQUEST_STATE = 600.0

# Preenchido por `criar_servidor`.
_central: CentralDeSalas | None = None


def central() -> CentralDeSalas:
    assert _central is not None, "criar_servidor() ainda nao foi chamado"
    return _central


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


def _validar(sala: str, inicio: str, fim: str) -> Intervalo:
    try:
        return central().validar(sala, inicio, fim)
    except RegraViolada as e:
        raise ToolError(str(e)) from None


def _modelo_escolha(alternativas: list[str]) -> type[BaseModel]:
    """Schema plano da elicitation: uma propriedade `sala` restrita às alternativas.

    Com duas ou mais alternativas o pydantic emite `enum`; com uma só, `const`.
    O desafio aceita as duas formas.
    """
    tipo = Literal[tuple(alternativas)]  # type: ignore[valid-type]
    return create_model(
        "EscolhaDeSala",
        sala=(tipo, Field(title="Sala", description="Sala alternativa escolhida")),
    )


def _exigir_elicitacao_form(ctx: Context) -> None:
    """Sem elicitation em form mode declarada neste request, não há pergunta: `-32021`.

    Mesma regra e mesmo formato de erro que o SDK aplica aos resolvers
    (`_require_capability` em `mcp/server/mcpserver/resolve.py`).
    """
    if ctx.protocol_version is None or not is_version_at_least(ctx.protocol_version, PRIMEIRA_VERSAO_MRTR):
        raise ToolError(f"Escolher alternativa exige a revisao {PRIMEIRA_VERSAO_MRTR} do MCP")
    capacidades = ctx.client_capabilities
    elicitacao = capacidades.elicitation if capacidades is not None else None
    if elicitacao is not None and (elicitacao.form is not None or elicitacao.url is None):
        return
    exigidas = types.ClientCapabilities(
        elicitation=types.ElicitationCapability(form=types.FormElicitationCapability())
    )
    dados = types.MissingRequiredClientCapabilityErrorData(required_capabilities=exigidas)
    raise MCPError(
        code=types.MISSING_REQUIRED_CLIENT_CAPABILITY,
        message=f"Client did not declare the form elicitation capability required by {CHAVE_ESCOLHA!r}",
        data=dados.model_dump(by_alias=True, mode="json", exclude_none=True),
    )


def _perguntar(ctx: Context, sala: str, inicio: str, fim: str, responsavel: str) -> types.InputRequiredResult:
    """Sala ocupada: devolve `input_required` com a elicitation e o pedido inteiro no estado.

    O `requestState` sai daqui em texto puro; o `RequestStateBoundary` do SDK o
    sela antes de ele chegar ao fio.
    """
    intervalo = _validar(sala, inicio, fim)
    alternativas = central().alternativas(sala, intervalo)
    if not alternativas:
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    _exigir_elicitacao_form(ctx)
    pergunta = types.ElicitRequest(
        params=types.ElicitRequestFormParams(
            message=MENSAGEM_ESCOLHA,
            requested_schema=render_elicitation_schema(_modelo_escolha(alternativas)),
        )
    )
    estado = {
        "v": VERSAO_ESTADO,
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
        "alternativas": alternativas,
    }
    return types.InputRequiredResult(
        input_requests={CHAVE_ESCOLHA: pergunta},
        request_state=json.dumps(estado, separators=(",", ":")),
    )


def _ler_estado(texto: str) -> dict:
    """Estado já autenticado pelo SDK. Forma inesperada só aparece com drift de versão: `-32602`."""
    try:
        estado = json.loads(texto)
        campos = ("sala", "inicio", "fim", "responsavel")
        valido = (
            estado.get("v") == VERSAO_ESTADO
            and all(isinstance(estado.get(c), str) for c in campos)
            and isinstance(estado.get("alternativas"), list)
        )
    except (ValueError, AttributeError):
        valido = False
    if not valido:
        raise MCPError(code=types.INVALID_PARAMS, message="Invalid or expired requestState")
    return estado


def _retomar(ctx: Context, estado_texto: str) -> ReservaOut | types.InputRequiredResult:
    """Retry: conclui com os valores selados, nunca com os que o cliente reenviou.

    A escolha precisa estar entre as alternativas oferecidas quando o estado foi
    selado e a sala precisa estar livre agora no intervalo selado. Se ela foi
    tomada entre a pausa e o retry, o servidor pergunta de novo com as
    alternativas atuais; mudanças nas outras alternativas não importam.
    """
    estado = _ler_estado(estado_texto)
    sala, inicio, fim, responsavel = estado["sala"], estado["inicio"], estado["fim"], estado["responsavel"]
    oferecidas: list[str] = estado["alternativas"]

    resposta = (ctx.input_responses or {}).get(CHAVE_ESCOLHA)
    if resposta is None:
        # Retry sem resposta: refaz a pergunta a partir do estado atual.
        return _perguntar(ctx, sala, inicio, fim, responsavel)
    if not isinstance(resposta, types.ElicitResult):
        raise ToolError(f"Resposta inesperada para {CHAVE_ESCOLHA}: esperava o resultado de uma elicitation")
    if resposta.action != "accept":
        # decline ou cancel: conclui sem reservar e sem isError.
        return ReservaOut(reservado=False, motivo="recusado")

    escolhida = (resposta.content or {}).get("sala")
    if escolhida not in oferecidas:
        raise ToolError(f"Escolha invalida: {escolhida} nao esta entre as alternativas oferecidas ({', '.join(oferecidas)})")

    intervalo = _validar(sala, inicio, fim)
    capacidade_ok = central().sala(escolhida)["capacidade"] >= central().sala(sala)["capacidade"]
    if not capacidade_ok or central().conflitos(escolhida, intervalo):
        return _perguntar(ctx, sala, inicio, fim, responsavel)
    return _gravar(escolhida, inicio, fim, responsavel)


def _gravar(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    try:
        reserva = central().reservar(sala, inicio, fim, responsavel)
    except RegraViolada as e:
        raise ToolError(str(e)) from None
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=reserva["sala"],
        inicio=reserva["inicio"],
        fim=reserva["fim"],
        responsavel=reserva["responsavel"],
        politica=central().versao_politica,
    )


def criar_servidor(segredo: bytes, dados: CentralDeSalas) -> MCPServer:
    global _central
    _central = dados

    mcp = MCPServer(
        NOME,
        version=VERSAO,
        log_level="WARNING",
        request_state_security=RequestStateSecurity(keys=[segredo], ttl=TTL_REQUEST_STATE),
    )

    @mcp.tool(description="Lista todas as salas com capacidade e recursos.")
    async def listar_salas() -> ListaDeSalas:
        return ListaDeSalas(salas=[SalaOut(**s) for s in central().salas])

    @mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
    async def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
        intervalo = _validar(sala, inicio, fim)
        conflitos = central().conflitos(sala, intervalo)
        return Disponibilidade(
            sala=sala,
            livre=not conflitos,
            conflitos=[ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in conflitos],
        )

    @mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
    async def reservar_sala(
        sala: str, inicio: str, fim: str, responsavel: str, ctx: Context
    ) -> ReservaOut | types.InputRequiredResult:
        if ctx.request_state is not None:
            # Retry: o SDK já verificou selo, expiração e o vínculo com estes argumentos.
            return _retomar(ctx, ctx.request_state)
        intervalo = _validar(sala, inicio, fim)
        if central().conflitos(sala, intervalo):
            return _perguntar(ctx, sala, inicio, fim, responsavel)
        return _gravar(sala, inicio, fim, responsavel)

    @mcp.resource(
        URI_POLITICA,
        name="politica-de-uso",
        description="Politica de uso das salas. A primeira linha declara a versao.",
        mime_type="text/markdown",
    )
    def politica() -> str:
        return central().texto_politica

    return mcp
