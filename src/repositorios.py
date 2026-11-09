from __future__ import annotations

import dataclasses
import inspect
from dataclasses import dataclass, field
from datetime import date, datetime, time
from enum import Enum
from functools import lru_cache
from typing import Any

from banco import (
    CODIGO_SQL_MONITORIA_LOTADA,
    RPC_ADICIONAR_AGENDAMENTO,
    RPC_REGISTRAR_MUDANCA,
    TABELA_AGENDAMENTOS,
    TABELA_HISTORICO_SITUACOES,
    TABELA_MONITORIAS,
    BancoDados,
    ErroBancoDados,
    ErroConfiguracaoBanco,
    ErroIntegridadeBanco,
    ErroOperacaoBanco,
    obter_cliente,
)

SITUACOES_VALIDAS: tuple[str, ...] = ("solicitado", "confirmado", "realizado", "cancelado")

SITUACOES_ATIVAS: tuple[str, ...] = ("solicitado", "confirmado")
DIAS_SEMANA_VALIDOS = range(0, 6)  

_INDICE_AGENDAMENTO_ATIVO_UNICO = "uq_agendamentos_aluno_monitoria_data_ativo"




@dataclass
class MonitoriaModelo:
    id: int | None
    disciplina: str
    monitor: str
    dia_semana: int
    hora_inicio: time
    hora_fim: time
    local: str
    vagas: int


@dataclass
class MudancaSituacaoModelo:
    id: int | None = None
    agendamento_id: int | None = None
    situacao_anterior: Any = None
    situacao_nova: Any = "solicitado"
    registrada_em: datetime = field(default_factory=datetime.now)


@dataclass
class AgendamentoModelo:
    id: int | None
    monitoria_id: int
    aluno_matricula: str
    aluno_nome: str
    data: date
    assunto: str
    situacao: Any = "solicitado"
    criado_em: datetime = field(default_factory=datetime.now)
    historico: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class _Dominio:
    monitoria: type
    agendamento: type
    mudanca: type
    situacao: type | None


@lru_cache(maxsize=1)
def _resolver_dominio() -> _Dominio:
    """Localiza as classes do domínio (modelos.py) ou usa os modelos de reserva."""
    try:
        import modelos
    except ImportError:
        return _Dominio(MonitoriaModelo, AgendamentoModelo, MudancaSituacaoModelo, None)

    situacao = getattr(modelos, "Situacao", None)
    if situacao is None:
        try:
            import regras

            situacao = getattr(regras, "Situacao", None)
        except ImportError:
            pass

    return _Dominio(
        getattr(modelos, "Monitoria", MonitoriaModelo),
        getattr(modelos, "Agendamento", AgendamentoModelo),
        getattr(modelos, "MudancaSituacao", MudancaSituacaoModelo),
        situacao,
    )




def _obter_campo(obj: Any, *nomes: str, padrao: Any = None) -> Any:
    """Primeiro atributo (ou chave) não nulo entre ``nomes``."""
    for nome in nomes:
        valor = obj.get(nome) if isinstance(obj, dict) else getattr(obj, nome, None)
        if valor is not None:
            return valor
    return padrao


def _atualizar(objeto: Any, **campos: Any) -> Any:
    """Grava campos no objeto e o devolve; se for imutável, devolve uma cópia alterada."""
    if isinstance(objeto, dict):
        objeto.update(campos)
        return objeto

    try:
        for nome, valor in campos.items():
            setattr(objeto, nome, valor)
        return objeto
    except AttributeError:  # dataclass(frozen=True), por exemplo
        if not dataclasses.is_dataclass(objeto):
            raise
        aceitos = {f.name for f in dataclasses.fields(objeto) if f.init}
        return dataclasses.replace(
            objeto, **{n: v for n, v in campos.items() if n in aceitos}
        )


def _construir(classe: type, valores: dict[str, Any]) -> Any:
    """Cria ``classe`` passando só os parâmetros que o ``__init__`` dela declara."""
    parametros = inspect.signature(classe).parameters
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parametros.values()):
        kwargs = dict(valores)
    else:
        kwargs = {n: v for n, v in valores.items() if n in parametros}

    faltando = [
        n
        for n, p in parametros.items()
        if p.default is inspect.Parameter.empty
        and p.kind
        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
        and n not in kwargs
    ]
    if faltando:
        raise ErroOperacaoBanco(
            f"A classe {classe.__name__} exige os campos {', '.join(faltando)}, que a "
            "tabela não fornece. Os nomes dos campos devem ser os das colunas "
            "(Quadro 5 da documentação); ajuste modelos.py ou repositorios.py."
        )
    return classe(**kwargs)




def _como_situacao(valor: Any) -> Any:
    """Texto do banco -> ``Situacao`` do domínio (ou o próprio texto, sem o enum)."""
    if valor is None:
        return None
    enum_situacao = _resolver_dominio().situacao
    if enum_situacao is None:
        return str(valor)
    try:
        return enum_situacao(str(valor))
    except ValueError:
        return str(valor)


def _normalizar_situacao(valor: Any, *, permitir_nulo: bool = False) -> str | None:
    if valor is None:
        if permitir_nulo:
            return None
        raise ErroIntegridadeBanco("A situação não pode ser nula.")

    if isinstance(valor, Enum):
        valor = valor.value

    texto = str(valor).strip().lower()
    if not texto and permitir_nulo:
        return None
    if texto not in SITUACOES_VALIDAS:
        raise ErroIntegridadeBanco(
            f"Situação inválida: '{valor}'. Valores permitidos: {', '.join(SITUACOES_VALIDAS)}."
        )
    return texto


def _normalizar_data(valor: Any) -> str:
    """Aceita date, datetime ou texto AAAA-MM-DD / DD/MM/AAAA; devolve AAAA-MM-DD."""
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()

    texto = str(valor or "").strip().split("T", 1)[0]
    if not texto:
        raise ErroIntegridadeBanco("A data do agendamento é obrigatória.")
    for formato in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto, formato).date().isoformat()
        except ValueError:
            continue
    raise ErroIntegridadeBanco(f"Data inválida: '{valor}'. Use AAAA-MM-DD ou DD/MM/AAAA.")


def _normalizar_hora(valor: Any, nome_campo: str = "horário") -> str:
    """Aceita time, datetime ou texto HH:MM[:SS]; devolve HH:MM."""
    if isinstance(valor, (time, datetime)):
        return valor.strftime("%H:%M")

    texto = str(valor or "").strip()
    if not texto:
        raise ErroIntegridadeBanco(f"O campo '{nome_campo}' é obrigatório.")
    for formato in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(texto, formato).strftime("%H:%M")
        except ValueError:
            continue
    raise ErroIntegridadeBanco(f"Horário inválido em '{nome_campo}': '{valor}'. Use HH:MM.")


def _normalizar_dia_semana(valor: Any) -> int:
    """0 (segunda) a 5 (sábado), a numeração de ``date.weekday()``."""
    if isinstance(valor, bool) or valor is None:
        raise ErroIntegridadeBanco("O dia da semana deve ser um número de 0 a 5.")
    try:
        dia = int(valor)
    except (TypeError, ValueError) as exc:
        raise ErroIntegridadeBanco(
            f"Dia da semana inválido: '{valor}'. Use 0 (segunda) a 5 (sábado)."
        ) from exc
    if dia not in DIAS_SEMANA_VALIDOS:
        raise ErroIntegridadeBanco(
            f"Dia da semana inválido: {dia}. Use 0 (segunda) a 5 (sábado)."
        )
    return dia


def _normalizar_timestamp(valor: Any = None) -> str:
    """Datetime (ou texto ISO) -> texto ISO com fuso. Sem fuso = hora local."""
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        valor = datetime.now()
    if isinstance(valor, str):
        try:
            valor = datetime.fromisoformat(valor.strip())
        except ValueError as exc:
            raise ErroIntegridadeBanco(f"Data e hora inválidas: '{valor}'.") from exc
    if not isinstance(valor, datetime):
        raise ErroIntegridadeBanco(f"Data e hora inválidas: '{valor}'.")
    if valor.tzinfo is None:
        valor = valor.astimezone()  # hora local -> com fuso
    return valor.isoformat(timespec="seconds")


def _ler_data(valor: Any) -> date:
    return date.fromisoformat(_normalizar_data(valor))


def _ler_hora(valor: Any) -> time:
    return time.fromisoformat(_normalizar_hora(valor))


def _ler_datetime(valor: Any) -> datetime:
    """Timestamp do banco (com fuso) -> datetime local sem fuso, como ``datetime.now()``."""
    momento = datetime.fromisoformat(str(valor))
    if momento.tzinfo is not None:
        momento = momento.astimezone().replace(tzinfo=None)
    return momento




def _instanciar_monitoria(registro: dict[str, Any]) -> Any:
    return _construir(
        _resolver_dominio().monitoria,
        {
            "id": int(registro["id"]),
            "disciplina": str(registro["disciplina"]),
            "monitor": str(registro["monitor"]),
            "dia_semana": int(registro["dia_semana"]),
            "hora_inicio": _ler_hora(registro["hora_inicio"]),
            "hora_fim": _ler_hora(registro["hora_fim"]),
            "local": str(registro["local"]),
            "vagas": int(registro["vagas"]),
        },
    )


def _instanciar_mudanca(registro: dict[str, Any]) -> Any:
    return _construir(
        _resolver_dominio().mudanca,
        {
            "id": int(registro["id"]),
            "agendamento_id": int(registro["agendamento_id"]),
            "situacao_anterior": _como_situacao(registro.get("situacao_anterior")),
            "situacao_nova": _como_situacao(registro["situacao_nova"]),
            "registrada_em": _ler_datetime(registro["registrada_em"]),
        },
    )


def _instanciar_agendamento(registro: dict[str, Any], historico: list[Any]) -> Any:
    instancia = _construir(
        _resolver_dominio().agendamento,
        {
            "id": int(registro["id"]),
            "monitoria_id": int(registro["monitoria_id"]),
            "aluno_matricula": str(registro["aluno_matricula"]),
            "aluno_nome": str(registro["aluno_nome"]),
            "data": _ler_data(registro["data"]),
            "assunto": str(registro["assunto"]),
            "situacao": _como_situacao(registro["situacao"]),
            "criado_em": _ler_datetime(registro["criado_em"]),
            "historico": historico,
        },
    )
    if not hasattr(instancia, "historico"):
        try:
            instancia.historico = historico
        except AttributeError:
            pass
    return instancia




def _erro_de_excecoes(nome: str, *argumentos: Any) -> Exception | None:
    """Cria um erro de excecoes.py (assinatura da documentação) ou None se não der."""
    try:
        import excecoes

        return getattr(excecoes, nome)(*argumentos)
    except (ImportError, AttributeError, TypeError):
        return None


def _tratar_erro_supabase(
    operacao: str, exc: Exception, *, contexto: dict[str, Any] | None = None
) -> Exception:
    """Converte qualquer falha da API/PostgreSQL em erro da camada de persistência."""
    if isinstance(exc, ErroBancoDados):
        return exc

    contexto = contexto or {}
    codigo = str(getattr(exc, "code", "") or "").upper()
    mensagem = str(getattr(exc, "message", None) or exc)
    texto_busca = f"{codigo} {mensagem} {getattr(exc, 'details', '')}".lower()


    if codigo == CODIGO_SQL_MONITORIA_LOTADA and "monitoria_id" in contexto:
        erro = _erro_de_excecoes(
            "MonitoriaLotadaError", contexto["monitoria_id"], contexto.get("data")
        )
        if erro is not None:
            return erro
    if codigo == "23505" and _INDICE_AGENDAMENTO_ATIVO_UNICO in texto_busca:
        erro = _erro_de_excecoes(
            "AgendamentoDuplicadoError",
            contexto.get("matricula"),
            contexto.get("monitoria_id"),
            contexto.get("data"),
        )
        if erro is not None:
            return erro

    codigos_integridade = ("23502", "23503", "23505", "23514", "22023", "P0002", "40001")
    termos_integridade = ("foreign key", "check constraint", "unique constraint", "duplicate key")
    if (
        codigo in codigos_integridade
        or codigo == CODIGO_SQL_MONITORIA_LOTADA
        or any(t in texto_busca for t in termos_integridade)
    ):
        return ErroIntegridadeBanco(f"Erro de integridade ao {operacao}: {mensagem}")

    if codigo == "42501":
        return ErroConfiguracaoBanco(
            f"Permissão negada ao {operacao}. Execute 'schema_supabase.sql' (seção de "
            f"segurança) e confira se SUPABASE_KEY é a chave anon. Detalhes: {mensagem}"
        )

    return ErroOperacaoBanco(f"Erro no banco de dados ao {operacao}: {mensagem}")


def _erro_rpc_ausente(exc: Exception) -> bool:
    codigo = str(getattr(exc, "code", "") or "").upper()
    mensagem = str(getattr(exc, "message", None) or exc).lower()
    return codigo in ("PGRST202", "42883") or "could not find the function" in mensagem




class MonitoriaRepositorio:
    """Persistência de monitorias."""

    def __init__(self, cliente: Any | None = None) -> None:
        self._banco = BancoDados(cliente=cliente)

    @property
    def cliente(self) -> Any:
        return self._banco.cliente

    def adicionar(self, monitoria: Any) -> Any:
        """Grava a monitoria e devolve o objeto com o ``id`` gerado pelo banco."""
        disciplina = str(_obter_campo(monitoria, "disciplina", padrao="")).strip()
        monitor = str(_obter_campo(monitoria, "monitor", padrao="")).strip()
        local = str(_obter_campo(monitoria, "local", padrao="")).strip()
        if not disciplina or not monitor or not local:
            raise ErroIntegridadeBanco("Disciplina, monitor e local são obrigatórios.")

        dia_semana = _normalizar_dia_semana(_obter_campo(monitoria, "dia_semana"))
        hora_inicio = _normalizar_hora(_obter_campo(monitoria, "hora_inicio"), "hora_inicio")
        hora_fim = _normalizar_hora(_obter_campo(monitoria, "hora_fim"), "hora_fim")
        if hora_fim <= hora_inicio:
            raise ErroIntegridadeBanco(
                "O horário de término deve ser posterior ao de início."
            )

        try:
            vagas = int(_obter_campo(monitoria, "vagas"))
        except (TypeError, ValueError) as exc:
            raise ErroIntegridadeBanco("O número de vagas deve ser um inteiro.") from exc
        if vagas <= 0:
            raise ErroIntegridadeBanco("O número de vagas deve ser maior que zero.")

        payload: dict[str, Any] = {
            "disciplina": disciplina,
            "monitor": monitor,
            "dia_semana": dia_semana,
            "hora_inicio": hora_inicio,
            "hora_fim": hora_fim,
            "local": local,
            "vagas": vagas,
        }
        id_existente = _obter_campo(monitoria, "id")
        if id_existente is not None:
            payload["id"] = int(id_existente)

        try:
            resposta = self._banco.tabela(TABELA_MONITORIAS).insert(payload).execute()
            dados = resposta.data or []
            if not dados:
                raise ErroOperacaoBanco("O Supabase não devolveu a monitoria gravada.")
            return _atualizar(monitoria, id=int(dados[0]["id"]))
        except Exception as exc:
            raise _tratar_erro_supabase("adicionar monitoria", exc) from exc

    def buscar_por_id(self, id: int) -> Any | None:
        """A monitoria com esse ``id``, ou ``None`` se não existir."""
        try:
            resposta = (
                self._banco.tabela(TABELA_MONITORIAS)
                .select("*")
                .eq("id", int(id))
                .limit(1)
                .execute()
            )
            dados = resposta.data or []
            return _instanciar_monitoria(dados[0]) if dados else None
        except Exception as exc:
            raise _tratar_erro_supabase(f"buscar monitoria id={id}", exc) from exc

    def listar_todas(self) -> list[Any]:
        """Todas as monitorias, por ``id``."""
        try:
            resposta = self._banco.tabela(TABELA_MONITORIAS).select("*").order("id").execute()
            return [_instanciar_monitoria(item) for item in resposta.data or []]
        except Exception as exc:
            raise _tratar_erro_supabase("listar monitorias", exc) from exc





class AgendamentoRepositorio:
    """Persistência de agendamentos e do histórico de situações."""

    def __init__(self, cliente: Any | None = None) -> None:
        self._banco = BancoDados(cliente=cliente)

    @property
    def cliente(self) -> Any:
        return self._banco.cliente

 

    def _carregar_historicos(self, agendamento_ids: list[int]) -> dict[int, list[Any]]:
        agrupado: dict[int, list[Any]] = {ag_id: [] for ag_id in agendamento_ids}
        if not agendamento_ids:
            return agrupado

        resposta = (
            self._banco.tabela(TABELA_HISTORICO_SITUACOES)
            .select("*")
            .in_("agendamento_id", agendamento_ids)
            .order("registrada_em")
            .order("id")
            .execute()
        )
        for linha in resposta.data or []:
            agrupado.setdefault(int(linha["agendamento_id"]), []).append(
                _instanciar_mudanca(linha)
            )
        return agrupado

    def _montar(self, registros: list[dict[str, Any]]) -> list[Any]:
        historicos = self._carregar_historicos([int(r["id"]) for r in registros])
        return [_instanciar_agendamento(r, historicos[int(r["id"])]) for r in registros]

    def _listar(self, operacao: str, **filtros: Any) -> list[Any]:
        try:
            consulta = self._banco.tabela(TABELA_AGENDAMENTOS).select("*")
            for coluna, valor in filtros.items():
                consulta = consulta.eq(coluna, valor)
            resposta = consulta.order("data").order("id").execute()
            return self._montar(resposta.data or [])
        except Exception as exc:
            raise _tratar_erro_supabase(operacao, exc) from exc

    def buscar_por_id(self, id: int) -> Any | None:
        """O agendamento (com histórico) ou ``None`` se não existir."""
        try:
            resposta = (
                self._banco.tabela(TABELA_AGENDAMENTOS)
                .select("*")
                .eq("id", int(id))
                .limit(1)
                .execute()
            )
            dados = resposta.data or []
            return self._montar(dados)[0] if dados else None
        except Exception as exc:
            raise _tratar_erro_supabase(f"buscar agendamento id={id}", exc) from exc

    def listar_todos(self) -> list[Any]:
        return self._listar("listar agendamentos")

    def listar_por_matricula(self, matricula: str) -> list[Any]:
        matricula_limpa = str(matricula or "").strip()
        if not matricula_limpa:
            return []
        return self._listar(
            f"listar agendamentos da matrícula {matricula_limpa}",
            aluno_matricula=matricula_limpa,
        )

    def listar_por_monitoria(self, monitoria_id: int) -> list[Any]:
        return self._listar(
            f"listar agendamentos da monitoria {monitoria_id}",
            monitoria_id=int(monitoria_id),
        )

    def listar_por_situacao(self, situacao: str | Enum) -> list[Any]:
        situacao_texto = _normalizar_situacao(situacao)
        return self._listar(
            f"listar agendamentos {situacao_texto}s", situacao=situacao_texto
        )

    def contar_ativos(self, monitoria_id: int, data: str | date | datetime) -> int:
        """Quantos agendamentos ocupam vaga (solicitado/confirmado) na monitoria e data."""
        data_iso = _normalizar_data(data)
        try:
            resposta = (
                self._banco.tabela(TABELA_AGENDAMENTOS)
                .select("id", count="exact")
                .eq("monitoria_id", int(monitoria_id))
                .eq("data", data_iso)
                .in_("situacao", list(SITUACOES_ATIVAS))
                .execute()
            )
            if getattr(resposta, "count", None) is not None:
                return int(resposta.count)
            return len(resposta.data or [])
        except Exception as exc:
            raise _tratar_erro_supabase(
                f"contar agendamentos ativos (monitoria {monitoria_id}, {data_iso})", exc
            ) from exc

    def existe_ativo(
        self, matricula: str, monitoria_id: int, data: str | date | datetime
    ) -> bool:
        """O aluno já ocupa uma vaga nessa monitoria e data?"""
        matricula_limpa = str(matricula or "").strip()
        if not matricula_limpa:
            return False
        data_iso = _normalizar_data(data)
        try:
            resposta = (
                self._banco.tabela(TABELA_AGENDAMENTOS)
                .select("id")
                .eq("aluno_matricula", matricula_limpa)
                .eq("monitoria_id", int(monitoria_id))
                .eq("data", data_iso)
                .in_("situacao", list(SITUACOES_ATIVAS))
                .limit(1)
                .execute()
            )
            return bool(resposta.data)
        except Exception as exc:
            raise _tratar_erro_supabase("verificar agendamento ativo", exc) from exc

   

    def _executar_funcao(self, nome: str, parametros: dict[str, Any]) -> dict[str, Any]:
        """Chama uma função SQL e devolve ``{"agendamento": ..., "historico": ...}``."""
        try:
            resposta = self._banco.rpc(nome, parametros).execute()
        except ErroBancoDados:
            raise
        except Exception as exc:
            if _erro_rpc_ausente(exc):
                raise ErroConfiguracaoBanco(
                    f"A função '{nome}' não existe no banco. Execute 'schema_supabase.sql' "
                    "no SQL Editor do Supabase. Gravar sem ela não seria atômico, "
                    "por isso a gravação foi recusada."
                ) from exc
            raise

        dados = resposta.data
        if isinstance(dados, list) and dados:
            dados = dados[0]
        if not isinstance(dados, dict) or not {"agendamento", "historico"} <= dados.keys():
            raise ErroOperacaoBanco(f"Resposta inesperada da função '{nome}'.")
        return dados

    def adicionar(self, agendamento: Any, mudanca: Any | None = None) -> Any:
        """Grava o agendamento e sua primeira linha de histórico, juntos ou nada.

        Devolve o agendamento com o ``id`` gerado; ``mudanca`` também recebe
        ``id`` e ``agendamento_id``.
        """
        if _obter_campo(agendamento, "monitoria_id") is None:
            raise ErroIntegridadeBanco("O campo 'monitoria_id' é obrigatório.")
        monitoria_id = int(_obter_campo(agendamento, "monitoria_id"))

        matricula = str(
            _obter_campo(agendamento, "aluno_matricula", "matricula", padrao="")
        ).strip()
        nome = str(_obter_campo(agendamento, "aluno_nome", "nome", padrao="")).strip()
        assunto = str(_obter_campo(agendamento, "assunto", padrao="")).strip()
        if not matricula or not nome or not assunto:
            raise ErroIntegridadeBanco(
                "Matrícula, nome do aluno e assunto são obrigatórios."
            )

        data_iso = _normalizar_data(_obter_campo(agendamento, "data"))
        situacao = _normalizar_situacao(
            _obter_campo(agendamento, "situacao", padrao="solicitado")
        )
        criado_em = _normalizar_timestamp(_obter_campo(agendamento, "criado_em"))

        if mudanca is not None:
            situacao_anterior = _normalizar_situacao(
                _obter_campo(mudanca, "situacao_anterior"), permitir_nulo=True
            )
            situacao_nova = _normalizar_situacao(
                _obter_campo(mudanca, "situacao_nova", padrao=situacao)
            )
            registrada_em = _normalizar_timestamp(
                _obter_campo(mudanca, "registrada_em") or criado_em
            )
        else:
            situacao_anterior, situacao_nova, registrada_em = None, situacao, criado_em

        if situacao_nova != situacao:
            raise ErroIntegridadeBanco(
                f"A situação do agendamento ('{situacao}') difere da situação nova "
                f"da mudança ('{situacao_nova}')."
            )

        contexto = {
            "monitoria_id": monitoria_id,
            "matricula": matricula,
            "data": date.fromisoformat(data_iso),
        }
        try:
            dados = self._executar_funcao(
                RPC_ADICIONAR_AGENDAMENTO,
                {
                    "p_monitoria_id": monitoria_id,
                    "p_aluno_matricula": matricula,
                    "p_aluno_nome": nome,
                    "p_data": data_iso,
                    "p_assunto": assunto,
                    "p_situacao": situacao,
                    "p_criado_em": criado_em,
                    "p_situacao_anterior": situacao_anterior,
                    "p_situacao_nova": situacao_nova,
                    "p_registrada_em": registrada_em,
                },
            )
            agendamento_id = int(dados["agendamento"]["id"])
            agendamento = _atualizar(agendamento, id=agendamento_id)

            linha_historico = dados["historico"]
            if mudanca is not None:
                mudanca = _atualizar(
                    mudanca, id=int(linha_historico["id"]), agendamento_id=agendamento_id
                )
            self._anexar_historico(
                agendamento, mudanca if mudanca is not None else _instanciar_mudanca(linha_historico)
            )
            return agendamento
        except Exception as exc:
            raise _tratar_erro_supabase(
                "adicionar agendamento com histórico", exc, contexto=contexto
            ) from exc

    def registrar_mudanca(self, agendamento: Any, mudanca: Any) -> Any:
        """Grava a nova situação e a linha de histórico, juntas ou nada.

        ``mudanca.situacao_anterior`` é conferida no banco: se outra pessoa já
        mudou o agendamento, a operação falha em vez de sobrescrever.
        """
        agendamento_id_bruto = _obter_campo(agendamento, "id") or _obter_campo(
            mudanca, "agendamento_id"
        )
        if agendamento_id_bruto is None:
            raise ErroIntegridadeBanco(
                "Não é possível registrar a mudança sem o ID do agendamento."
            )
        agendamento_id = int(agendamento_id_bruto)

        situacao_nova = _normalizar_situacao(
            _obter_campo(mudanca, "situacao_nova", "situacao")
            or _obter_campo(agendamento, "situacao")
        )
        situacao_anterior = _normalizar_situacao(
            _obter_campo(mudanca, "situacao_anterior"), permitir_nulo=True
        )
        registrada_em = _normalizar_timestamp(_obter_campo(mudanca, "registrada_em"))

        try:
            dados = self._executar_funcao(
                RPC_REGISTRAR_MUDANCA,
                {
                    "p_agendamento_id": agendamento_id,
                    "p_situacao_anterior": situacao_anterior,
                    "p_situacao_nova": situacao_nova,
                    "p_registrada_em": registrada_em,
                },
            )
            linha_historico = dados["historico"]
            agendamento = _atualizar(
                agendamento, situacao=_como_situacao(dados["agendamento"]["situacao"])
            )
            mudanca = _atualizar(
                mudanca,
                id=int(linha_historico["id"]),
                agendamento_id=agendamento_id,
                situacao_anterior=_como_situacao(linha_historico.get("situacao_anterior")),
            )
            self._anexar_historico(agendamento, mudanca)
            return agendamento
        except Exception as exc:
            raise _tratar_erro_supabase(
                f"registrar mudança de situação do agendamento {agendamento_id}", exc
            ) from exc

    @staticmethod
    def _anexar_historico(agendamento: Any, mudanca: Any) -> None:
        historico = _obter_campo(agendamento, "historico")
        if isinstance(historico, list):
            if mudanca not in historico:
                historico.append(mudanca)
        else:
            try:
                _atualizar(agendamento, historico=[mudanca])
            except (AttributeError, TypeError):
                pass


__all__ = [
    "SITUACOES_ATIVAS",
    "SITUACOES_VALIDAS",
    "AgendamentoModelo",
    "AgendamentoRepositorio",
    "MonitoriaModelo",
    "MonitoriaRepositorio",
    "MudancaSituacaoModelo",
    "obter_cliente",
]
