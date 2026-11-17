"""Domínio das salas: dados, política de uso e reservas em memória.

Nada aqui conhece MCP. O servidor chama estas funções e traduz as exceções
`RegraViolada` em erro de execução de tool (`isError: true`).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

FUSO_SAO_PAULO = timezone(timedelta(hours=-3))
JANELA_INICIO = time(8, 0)
JANELA_FIM = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"
ERRO_DATA = "Data invalida: {valor} nao esta no formato ISO 8601"


class RegraViolada(Exception):
    """Pedido que a política ou os dados recusam. A mensagem vai para o cliente como está."""


def diretorio_dados() -> Path:
    """`dados/` na raiz do repositório, ou o caminho em `DADOS_DIR`.

    Resolvido a partir deste arquivo, e não do diretório corrente, para que o
    servidor suba de qualquer lugar.
    """
    configurado = os.environ.get("DADOS_DIR")
    if configurado:
        return Path(configurado).resolve()
    return Path(__file__).resolve().parents[2] / "dados"


@dataclass(frozen=True)
class Intervalo:
    inicio: datetime
    fim: datetime

    def sobrepoe(self, outro: Intervalo) -> bool:
        # Fim exclusivo: 14:00-15:00 e 15:00-16:00 não conflitam.
        return self.inicio < outro.fim and outro.inicio < self.fim


def _data(valor: str) -> datetime:
    try:
        instante = datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        raise RegraViolada(ERRO_DATA.format(valor=valor)) from None
    if instante.tzinfo is None:
        instante = instante.replace(tzinfo=FUSO_SAO_PAULO)
    return instante


class CentralDeSalas:
    """Salas, política e reservas. As reservas vivem só em memória."""

    def __init__(self, dados: Path) -> None:
        self.salas: list[dict] = json.loads((dados / "salas.json").read_text(encoding="utf-8"))
        self.reservas: list[dict] = json.loads((dados / "reservas.json").read_text(encoding="utf-8"))
        self.texto_politica = (dados / "politica-de-uso.md").read_text(encoding="utf-8")
        primeira_linha = self.texto_politica.splitlines()[0]
        chave, _, valor = primeira_linha.partition(":")
        if chave.strip() != "versao" or not valor.strip():
            raise ValueError(f"primeira linha da politica sem versao: {primeira_linha!r}")
        self.versao_politica = valor.strip()
        self._sequencia = max((int(r["id"].split("-")[1]) for r in self.reservas), default=0)
        self._por_id = {s["id"]: s for s in self.salas}

    def sala(self, sala_id: str) -> dict:
        sala = self._por_id.get(sala_id)
        if sala is None:
            raise RegraViolada(ERRO_SALA.format(sala=sala_id))
        return sala

    def validar(self, sala_id: str, inicio: str, fim: str) -> Intervalo:
        """Aplica as regras na ordem que deixa cada erro inequívoco.

        Sala primeiro, depois intervalo invertido (10:00 -> 09:00 é inválido,
        não "fora da janela"), depois janela, por último duração (09:00 -> 12:00
        está dentro da janela e só viola a duração).
        """
        self.sala(sala_id)
        intervalo = Intervalo(_data(inicio), _data(fim))
        if intervalo.fim <= intervalo.inicio:
            raise RegraViolada(ERRO_INTERVALO)
        local_inicio = intervalo.inicio.astimezone(FUSO_SAO_PAULO)
        local_fim = intervalo.fim.astimezone(FUSO_SAO_PAULO)
        dentro = (
            local_inicio.date() == local_fim.date()
            and local_inicio.time() >= JANELA_INICIO
            and local_fim.time() <= JANELA_FIM
        )
        if not dentro:
            raise RegraViolada(ERRO_JANELA)
        if intervalo.fim - intervalo.inicio > DURACAO_MAXIMA:
            raise RegraViolada(ERRO_DURACAO)
        return intervalo

    def conflitos(self, sala_id: str, intervalo: Intervalo) -> list[dict]:
        return [
            r
            for r in self.reservas
            if r["sala"] == sala_id and intervalo.sobrepoe(Intervalo(_data(r["inicio"]), _data(r["fim"])))
        ]

    def alternativas(self, sala_id: str, intervalo: Intervalo) -> list[str]:
        """Salas livres no intervalo com capacidade >= a pedida, no máximo três.

        Ordem: capacidade crescente e, no empate, id em ordem alfabética.
        """
        minima = self.sala(sala_id)["capacidade"]
        candidatas = [
            s
            for s in self.salas
            if s["id"] != sala_id and s["capacidade"] >= minima and not self.conflitos(s["id"], intervalo)
        ]
        candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
        return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]

    def reservar(self, sala_id: str, inicio: str, fim: str, responsavel: str) -> dict:
        """Grava a reserva. Quem chama já validou; aqui só se confere o conflito de novo."""
        intervalo = self.validar(sala_id, inicio, fim)
        if self.conflitos(sala_id, intervalo):
            raise RegraViolada(f"Sala ocupada no intervalo: {sala_id}")
        self._sequencia += 1
        reserva = {
            "id": f"res-{self._sequencia:04d}",
            "sala": sala_id,
            "inicio": inicio,
            "fim": fim,
            "responsavel": responsavel,
        }
        self.reservas.append(reserva)
        return reserva
