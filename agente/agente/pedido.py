"""Leitura dos dois formatos fixos de texto que o agente aceita. Sem LLM, só regra.

    reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>
    escolha=<id da sala> | escolha=recusar

O agente não valida datas nem salas: isso é regra de domínio e mora no servidor
MCP. Aqui só se confere a forma do texto.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

FORMATO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
RECUSAR = "recusar"

_PEDIDO = re.compile(r"^reservar\s+sala=(\S+)\s+inicio=(\S+)\s+fim=(\S+)\s+responsavel=(.+?)\s*$")
_ESCOLHA = re.compile(r"^escolha=(\S+)\s*$")


@dataclass(frozen=True)
class Pedido:
    sala: str
    inicio: str
    fim: str
    responsavel: str

    def argumentos(self) -> dict[str, str]:
        return {"sala": self.sala, "inicio": self.inicio, "fim": self.fim, "responsavel": self.responsavel}


def ler_pedido(texto: str) -> Pedido | None:
    casamento = _PEDIDO.match(texto.strip())
    if casamento is None:
        return None
    return Pedido(*casamento.groups())


def ler_escolha(texto: str) -> str | None:
    casamento = _ESCOLHA.match(texto.strip())
    return casamento.group(1) if casamento else None
