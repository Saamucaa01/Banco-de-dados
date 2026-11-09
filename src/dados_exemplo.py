from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

FORMATO_DATA_DIGITADA = "%d/%m/%Y"  


@dataclass(frozen=True)
class _MonitoriaExemplo:
    disciplina: str
    monitor: str
    dia: str  
    inicio: str
    fim: str
    local: str
    vagas: str


@dataclass(frozen=True)
class _AgendamentoExemplo:
    indice_monitoria: int  
    matricula: str
    nome: str
    assunto: str
   
    transicoes: tuple[str, ...] = field(default=())


MONITORIAS_EXEMPLO: tuple[_MonitoriaExemplo, ...] = (
    _MonitoriaExemplo(
        "Banco de Dados", "Samuel Oliveira", "1", "14:00", "16:00",
        "Laboratório 03 - Bloco B", "5",
    ),
    _MonitoriaExemplo(
        "Algoritmos e Estruturas de Dados", "Beatriz Santos", "3", "10:00", "12:00",
        "Sala 204 - Bloco A", "4",
    ),
    _MonitoriaExemplo(
        "Programação Orientada a Objetos", "Lucas Almeida", "5", "16:00", "18:00",
        "Laboratório 01 - Bloco B", "3",
    ),
)

AGENDAMENTOS_EXEMPLO: tuple[_AgendamentoExemplo, ...] = (
    _AgendamentoExemplo(
        0, "2025001", "Ana Clara Ribeiro",
        "Modelagem relacional, chaves estrangeiras e restrições CHECK",
    ),
    _AgendamentoExemplo(
        0, "2025002", "Carlos Eduardo Lima",
        "Transações e funções no PostgreSQL",
        ("confirmado",),
    ),
    _AgendamentoExemplo(
        1, "2025003", "Mariana Costa",
        "Árvores binárias de busca e análise de complexidade",
        ("confirmado", "realizado"),
    ),
    _AgendamentoExemplo(
        2, "2025001", "Ana Clara Ribeiro",
        "Padrão Repository e arquitetura em camadas",
        ("cancelado",),
    ),
)


def calcular_proxima_data(dia_semana: int, *, referencia: date | None = None) -> date:
    """Próxima data (depois de hoje) que cai no ``dia_semana`` (0 = segunda, como date.weekday())."""
    hoje = referencia or date.today()
    dias_ate_alvo = (dia_semana - hoje.weekday()) % 7 or 7
    return hoje + timedelta(days=dias_ate_alvo)


def popular_dados_exemplo(
    servico_monitorias: Any,
    servico_agendamentos: Any,
    *,
    agora: datetime | None = None,
) -> dict[str, int]:
    """Cadastra monitorias e agendamentos de exemplo se o banco estiver vazio.

    Devolve ``{"monitorias": X, "agendamentos": Y}`` com o que foi criado
    (``0`` e ``0`` quando já havia dados).
    """
    if servico_monitorias.listar_monitorias() or servico_agendamentos.listar_agendamentos():
        return {"monitorias": 0, "agendamentos": 0}

    agora = agora or datetime.now()

    monitorias = [
        servico_monitorias.cadastrar_monitoria(
            m.disciplina, m.monitor, m.dia, m.inicio, m.fim, m.local, m.vagas
        )
        for m in MONITORIAS_EXEMPLO
    ]

    criados = 0
    for exemplo in AGENDAMENTOS_EXEMPLO:
        base = MONITORIAS_EXEMPLO[exemplo.indice_monitoria]
        monitoria = monitorias[exemplo.indice_monitoria]
        data = calcular_proxima_data(int(base.dia) - 1, referencia=agora.date())

        agendamento = servico_agendamentos.agendar(
            exemplo.matricula,
            exemplo.nome,
            str(monitoria.id),
            data.strftime(FORMATO_DATA_DIGITADA),
            exemplo.assunto,
            agora=agora,
        )
        for nova_situacao in exemplo.transicoes:
            servico_agendamentos.alterar_situacao(
                str(agendamento.id), nova_situacao, agora=agora
            )
        criados += 1

    return {"monitorias": len(monitorias), "agendamentos": criados}



carregar_dados_exemplo = popular_dados_exemplo
inicializar_dados_exemplo = popular_dados_exemplo


__all__ = [
    "AGENDAMENTOS_EXEMPLO",
    "MONITORIAS_EXEMPLO",
    "calcular_proxima_data",
    "carregar_dados_exemplo",
    "inicializar_dados_exemplo",
    "popular_dados_exemplo",
]
