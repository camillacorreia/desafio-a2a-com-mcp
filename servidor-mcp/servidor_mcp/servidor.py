"""Servidor MCP `central-de-salas`: três tools, um resource e o MRTR da reserva.

O MRTR usa o caminho de primeira classe do SDK Python v2: um resolver
(`Resolve(escolha_de_sala)`) que devolve `Elicit[...]` quando a sala está
ocupada. Na revisão 2026-07-28 o SDK transforma isso num resultado
`input_required` com a elicitation em form mode dentro de `inputRequests`, e
num retry lê `inputResponses` e `requestState` de `params`. O `requestState`
é selado pelo `RequestStateBoundary` do SDK (AES-256-GCM, chave derivada de
`REQUEST_STATE_SECRET`), com expiração e vínculo ao método, à tool e ao digest
dos argumentos. O servidor não guarda nada entre a pergunta e o retry.
"""

from __future__ import annotations

from typing import Annotated, Literal

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.request_state import RequestStateSecurity
from pydantic import BaseModel, Field, create_model

from servidor_mcp.dominio import ERRO_SEM_ALTERNATIVA, CentralDeSalas, Intervalo, RegraViolada

NOME = "central-de-salas"
VERSAO = "1.0.0"
URI_POLITICA = "politica://uso"
MENSAGEM_ESCOLHA = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."

# Vida do requestState: 10 minutos, dentro da faixa de 5 a 30 pedida pelo desafio.
TTL_REQUEST_STATE = 600.0

# Preenchido por `criar_servidor`. Resolvers do SDK recebem só Context, outros
# resolvers ou argumentos da tool, então o domínio fica num global do módulo.
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


async def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[BaseModel] | None:
    """Resolver do MRTR: decide se a reserva precisa perguntar algo ao cliente.

    Roda a cada rodada (pedido original e retry) e recalcula as alternativas
    com o estado atual das reservas. O SDK só aceita a resposta do cliente se a
    pergunta renderizada agora for idêntica à que foi feita, então uma
    alternativa que deixou de estar livre entre a pausa e o retry gera uma
    pergunta nova em vez de uma reserva errada.
    """
    intervalo = _validar(sala, inicio, fim)
    if not central().conflitos(sala, intervalo):
        return None
    alternativas = central().alternativas(sala, intervalo)
    if not alternativas:
        raise ToolError(ERRO_SEM_ALTERNATIVA)
    return Elicit(MENSAGEM_ESCOLHA, _modelo_escolha(alternativas))


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
        sala: str,
        inicio: str,
        fim: str,
        responsavel: str,
        escolha: Annotated[ElicitationResult[BaseModel], Resolve(escolha_de_sala)],
    ) -> ReservaOut:
        if not isinstance(escolha, AcceptedElicitation):
            # decline ou cancel: conclui sem reservar e sem isError.
            return ReservaOut(reservado=False, motivo="recusado")
        # Sem conflito o resolver não perguntou nada e `data` é None: reserva a sala pedida.
        destino = sala if escolha.data is None else escolha.data.sala
        try:
            reserva = central().reservar(destino, inicio, fim, responsavel)
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

    @mcp.resource(
        URI_POLITICA,
        name="politica-de-uso",
        description="Politica de uso das salas. A primeira linha declara a versao.",
        mime_type="text/markdown",
    )
    def politica() -> str:
        return central().texto_politica

    return mcp
