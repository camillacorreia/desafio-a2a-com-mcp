"""Tasks A2A e a ponte com o MRTR do MCP.

A ponte são duas funções:

- `Agente._concluir_rodada` recebe o resultado de um `tools/call`. Se ele é
  `input_required`, a Task vai para `TASK_STATE_INPUT_REQUIRED`, com a linha
  `alternativas: ...` como mensagem, e o `requestState` fica guardado na
  `Pausa` daquela Task.
- `Agente._retomar` é a volta: com a escolha do cliente A2A, repete o mesmo
  `tools/call` (id de JSON-RPC novo, gerado pelo SDK) levando `inputResponses`
  com a mesma chave e o `requestState` ecoado sem modificação.

O `requestState` é opaco para o agente: é guardado e ecoado, nunca aberto,
interpretado ou reconstruído, e nunca sai em resposta A2A (a serialização da
Task em `Tarefa.como_a2a` não toca na `Pausa`).

O estado das Tasks vive em memória, num dicionário deste processo.
"""

from __future__ import annotations

import json
import secrets
import sys
from dataclasses import dataclass, field
from typing import Any

import anyio
import httpx2
from mcp import types
from mcp.shared.exceptions import MCPError

from agente.host_mcp import HostMCP
from agente.pedido import FORMATO_PEDIDO, RECUSAR, ler_escolha, ler_pedido
from agente.trace import novo_trace_id

TOOL_RESERVA = "reservar_sala"

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = frozenset({COMPLETED, CANCELED, FAILED})


class ErroA2A(Exception):
    """Erro de JSON-RPC devolvido ao cliente A2A."""

    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


# Códigos de erro do binding JSON-RPC do A2A v1.0.
TASK_NOT_FOUND = -32001
UNSUPPORTED_OPERATION = -32004
INVALID_PARAMS = -32602


def _id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


def _log(texto: str) -> None:
    sys.stderr.write(f"agente {texto}\n")
    sys.stderr.flush()


@dataclass
class Pausa:
    """O que o agente precisa para repetir o `tools/call`. Nunca serializado para o A2A."""

    tool: str
    argumentos: dict[str, Any]
    chave: str
    request_state: str | None
    alternativas: list[str]


@dataclass
class Tarefa:
    id: str
    context_id: str
    trace_id: str
    estado: str = SUBMITTED
    mensagem_status: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artefatos: list[dict[str, Any]] = field(default_factory=list)
    politica: str | None = None
    pausa: Pausa | None = field(default=None, repr=False)
    trava: anyio.Lock = field(default_factory=anyio.Lock, repr=False)

    def como_a2a(self, limite_historico: int | None = None) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem_status is not None:
            status["message"] = self.mensagem_status
        historico = self.historico
        if limite_historico is not None:
            historico = historico[-limite_historico:] if limite_historico > 0 else []
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": historico,
            "artifacts": self.artefatos,
        }

    def falar(self, estado: str, texto: str) -> None:
        """Muda o estado com uma mensagem do agente, que também entra no histórico."""
        mensagem = {
            "messageId": _id("msg"),
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }
        self.historico.append(mensagem)
        self.mensagem_status = mensagem
        self.estado = estado
        _log(f"a2a task={self.id} estado={estado} mensagem={texto!r}")


def _linha_alternativas(alternativas: list[str]) -> str:
    return "alternativas: " + ", ".join(alternativas)


def _texto(resultado: types.CallToolResult) -> str:
    return " ".join(c.text for c in resultado.content if isinstance(c, types.TextContent)).strip()


def _ler_pergunta(resultado: types.InputRequiredResult) -> tuple[str, list[str]]:
    """Chave e alternativas da elicitation.

    Lê só o pedido de input, que é público e feito para o cliente. O
    `requestState` não é tocado.
    """
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1:
        raise ValueError(f"esperava uma pergunta do servidor MCP, vieram {len(pedidos)}")
    chave, pedido = next(iter(pedidos.items()))
    if not isinstance(pedido, types.ElicitRequest) or not isinstance(pedido.params, types.ElicitRequestFormParams):
        raise ValueError("o servidor MCP pediu um input que nao e elicitation em form mode")
    campo = (pedido.params.requested_schema.get("properties") or {}).get("sala") or {}
    if "enum" in campo:
        alternativas = [str(v) for v in campo["enum"]]
    elif "const" in campo:
        alternativas = [str(campo["const"])]
    else:
        raise ValueError("a elicitation do servidor MCP nao restringe a sala a uma lista de alternativas")
    return chave, alternativas


class Agente:
    def __init__(self, host: HostMCP) -> None:
        self.host = host
        self.tarefas: dict[str, Tarefa] = {}

    def obter(self, task_id: str) -> Tarefa:
        tarefa = self.tarefas.get(task_id)
        if tarefa is None:
            raise ErroA2A(TASK_NOT_FOUND, f"Task nao encontrada: {task_id}")
        return tarefa

    async def receber(self, mensagem: dict[str, Any], header_traceparent_trace_id: str | None) -> Tarefa:
        """`SendMessage`: abre uma Task nova ou continua uma Task pausada."""
        texto = " ".join(p.get("text", "") for p in mensagem.get("parts", []) if isinstance(p, dict)).strip()
        task_id = mensagem.get("taskId")
        if task_id:
            return await self._continuar(self.obter(str(task_id)), mensagem, texto)

        tarefa = Tarefa(
            id=_id("task"),
            context_id=str(mensagem.get("contextId") or _id("ctx")),
            trace_id=header_traceparent_trace_id or novo_trace_id(),
        )
        tarefa.historico.append(mensagem)
        self.tarefas[tarefa.id] = tarefa
        _log(f"a2a task={tarefa.id} estado={SUBMITTED} trace-id={tarefa.trace_id}")
        async with tarefa.trava:
            tarefa.estado = WORKING
            _log(f"a2a task={tarefa.id} estado={WORKING}")
            await self._iniciar(tarefa, texto)
        return tarefa

    async def _iniciar(self, tarefa: Tarefa, texto: str) -> None:
        pedido = ler_pedido(texto)
        if pedido is None:
            tarefa.falar(FAILED, f"Pedido invalido: use o formato {FORMATO_PEDIDO}")
            return
        try:
            # Descoberta em runtime: o nome da tool é conferido contra o tools/list, a cada Task.
            tools = await self.host.descobrir_tools(tarefa.trace_id)
            if TOOL_RESERVA not in tools:
                tarefa.falar(FAILED, f"O servidor MCP nao oferece a tool {TOOL_RESERVA}")
                return
            tarefa.politica = await self.host.versao_da_politica(tarefa.trace_id)
            resultado = await self.host.chamar_tool(TOOL_RESERVA, pedido.argumentos(), tarefa.trace_id)
        except Exception as erro:  # noqa: BLE001 - qualquer falha do MCP termina a Task com mensagem clara
            tarefa.falar(FAILED, _descrever_falha(erro))
            return
        self._concluir_rodada(tarefa, TOOL_RESERVA, pedido.argumentos(), resultado)

    def _concluir_rodada(
        self,
        tarefa: Tarefa,
        tool: str,
        argumentos: dict[str, Any],
        resultado: types.CallToolResult | types.InputRequiredResult,
    ) -> None:
        """A ponte, na ida: o resultado de um `tools/call` decide o próximo estado da Task."""
        if isinstance(resultado, types.InputRequiredResult):
            try:
                chave, alternativas = _ler_pergunta(resultado)
            except ValueError as erro:
                tarefa.falar(FAILED, f"Resposta do servidor MCP fora do esperado: {erro}")
                return
            tarefa.pausa = Pausa(
                tool=tool,
                argumentos=argumentos,
                chave=chave,
                request_state=resultado.request_state,
                alternativas=alternativas,
            )
            tarefa.falar(INPUT_REQUIRED, _linha_alternativas(alternativas))
            return

        if resultado.is_error:
            tarefa.falar(FAILED, _texto(resultado) or "A tool devolveu erro sem mensagem")
            return

        dados = resultado.structured_content or {}
        if dados.get("reservado") is False:
            tarefa.falar(CANCELED, f"Reserva nao realizada: {dados.get('motivo') or 'recusado'}.")
            return

        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": tarefa.politica,
        }
        tarefa.artefatos.append(
            {"artifactId": _id("art"), "name": "reserva", "parts": [{"text": json.dumps(reserva, ensure_ascii=False)}]}
        )
        tarefa.falar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")

    async def _continuar(self, tarefa: Tarefa, mensagem: dict[str, Any], texto: str) -> Tarefa:
        async with tarefa.trava:
            if tarefa.estado in TERMINAIS:
                raise ErroA2A(
                    UNSUPPORTED_OPERATION,
                    f"Task {tarefa.id} esta em estado terminal ({tarefa.estado}) e nao aceita novas mensagens",
                )
            if tarefa.estado != INPUT_REQUIRED or tarefa.pausa is None:
                raise ErroA2A(UNSUPPORTED_OPERATION, f"Task {tarefa.id} nao esta aguardando resposta ({tarefa.estado})")
            tarefa.historico.append(mensagem)
            await self._retomar(tarefa, ler_escolha(texto))
        return tarefa

    async def _retomar(self, tarefa: Tarefa, escolha: str | None) -> None:
        """A ponte, na volta: a escolha vira `inputResponses` e o `requestState` volta ao servidor."""
        pausa = tarefa.pausa
        assert pausa is not None
        if escolha == RECUSAR:
            resposta = types.ElicitResult(action="decline")
        elif escolha is not None and escolha in pausa.alternativas:
            resposta = types.ElicitResult(action="accept", content={"sala": escolha})
        else:
            # Fora do enum (ou texto que não é escolha): a Task continua pausada, com a mesma lista.
            tarefa.falar(INPUT_REQUIRED, _linha_alternativas(pausa.alternativas))
            return

        tarefa.pausa = None
        tarefa.estado = WORKING
        _log(f"a2a task={tarefa.id} estado={WORKING} retomada={resposta.action}")
        try:
            resultado = await self.host.chamar_tool(
                pausa.tool,
                pausa.argumentos,
                tarefa.trace_id,
                respostas={pausa.chave: resposta},
                request_state=pausa.request_state,
            )
        except Exception as erro:  # noqa: BLE001
            tarefa.falar(FAILED, _descrever_falha(erro))
            return
        # Se o servidor perguntar de novo (a sala escolhida foi tomada no meio do caminho),
        # a Task volta a pausar com as alternativas e o requestState novos.
        self._concluir_rodada(tarefa, pausa.tool, pausa.argumentos, resultado)


def _descrever_falha(erro: Exception) -> str:
    if isinstance(erro, MCPError):
        return f"Erro do servidor MCP ({erro.error.code}): {erro.error.message}"
    if isinstance(erro, httpx2.HTTPError | OSError):
        return f"Servidor MCP indisponivel: {erro}"
    return f"Falha ao falar com o servidor MCP: {type(erro).__name__}: {erro}"
