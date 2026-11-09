from __future__ import annotations

import base64
import enum
import json
import sys
import types
import unittest
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import repositorios  
from banco import (  
    ConfiguracaoSupabase,
    ErroBancoDados,
    ErroConfiguracaoBanco,
    ErroIntegridadeBanco,
    PersistenciaError,
)
from dados_exemplo import (  
    calcular_proxima_data,
    popular_dados_exemplo,
)
from repositorios import (  
    AgendamentoModelo,
    AgendamentoRepositorio,
    MonitoriaModelo,
    MonitoriaRepositorio,
    MudancaSituacaoModelo,
)



class _ApiErroFalso(Exception):
    """Parecido com postgrest.APIError: tem code e message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _QueryBuilderFalso:
    def __init__(self, cliente: ClienteSupabaseFalso, tabela: str) -> None:
        self._cliente = cliente
        self._tabela = tabela
        self._operacao = "select"
        self._payload: dict[str, Any] = {}
        self._eq: list[tuple[str, Any]] = []
        self._in: list[tuple[str, list[Any]]] = []
        self._ordens: list[str] = []
        self._limite: int | None = None
        self._count: str | None = None

    def select(self, _colunas: str = "*", count: str | None = None):
        self._operacao, self._count = "select", count
        return self

    def insert(self, payload: dict[str, Any]):
        self._operacao, self._payload = "insert", dict(payload)
        return self

    def eq(self, coluna: str, valor: Any):
        self._eq.append((coluna, valor))
        return self

    def in_(self, coluna: str, valores: list[Any]):
        self._in.append((coluna, list(valores)))
        return self

    def order(self, coluna: str, **_kw: Any):
        self._ordens.append(coluna)
        return self

    def limit(self, tamanho: int):
        self._limite = tamanho
        return self

    def _casa(self, linha: dict[str, Any]) -> bool:
        return all(linha.get(c) == v for c, v in self._eq) and all(
            linha.get(c) in vs for c, vs in self._in
        )

    def execute(self) -> SimpleNamespace:
        linhas = self._cliente.tabelas[self._tabela]

        if self._operacao == "insert":
            if self._tabela != "monitorias":
                
                raise _ApiErroFalso("42501", f"permission denied for table {self._tabela}")
            novo = self._cliente.validar_monitoria(self._payload)
            if novo.get("id") is None:
                self._cliente.sequencias["monitorias"] += 1
                novo["id"] = self._cliente.sequencias["monitorias"]
            linhas.append(novo)
            return SimpleNamespace(data=[dict(novo)], count=1)

        filtradas = [r for r in linhas if self._casa(r)]
        for coluna in reversed(self._ordens):
            filtradas.sort(key=lambda r: r.get(coluna) or "")
        total = len(filtradas)
        if self._limite is not None:
            filtradas = filtradas[: self._limite]
        return SimpleNamespace(
            data=[dict(r) for r in filtradas],
            count=total if self._count == "exact" else None,
        )


def _como_utc(texto: str) -> str:
    """O PostgreSQL devolve timestamptz em UTC, mesmo que a gravação tenha outro fuso."""
    return datetime.fromisoformat(texto).astimezone(UTC).isoformat()


class _RpcFalso:
    """Imita as duas funções SQL (mesmos SQLSTATE e mensagens)."""

    def __init__(self, cliente: ClienteSupabaseFalso, nome: str, params: dict[str, Any]):
        self._c, self._nome, self._p = cliente, nome, params

    def execute(self) -> SimpleNamespace:
        c, p = self._c, self._p
        if not c.funcoes_instaladas:
            raise _ApiErroFalso(
                "PGRST202", f"Could not find the function public.{self._nome} in the schema cache"
            )

        if self._nome == "adicionar_agendamento_com_historico":
            monitoria = next(
                (m for m in c.tabelas["monitorias"] if m["id"] == p["p_monitoria_id"]), None
            )
            if monitoria is None:
                raise _ApiErroFalso("P0002", f"Monitoria com ID {p['p_monitoria_id']} não encontrada.")

            ativos = [
                a for a in c.tabelas["agendamentos"]
                if a["monitoria_id"] == p["p_monitoria_id"]
                and a["data"] == p["p_data"]
                and a["situacao"] in ("solicitado", "confirmado")
            ]
            ja_tem = any(a["aluno_matricula"] == p["p_aluno_matricula"] for a in ativos)
            if len(ativos) >= monitoria["vagas"] and not ja_tem:
                raise _ApiErroFalso("CC001", "Monitoria lotada")
            if ja_tem:
                raise _ApiErroFalso(
                    "23505",
                    'duplicate key value violates unique constraint '
                    '"uq_agendamentos_aluno_monitoria_data_ativo"',
                )
            if c.falhar_historico:
                
                raise _ApiErroFalso("23514", "violates check constraint (histórico)")

            c.sequencias["agendamentos"] += 1
            c.sequencias["historico_situacoes"] += 1
            ag = {
                "id": c.sequencias["agendamentos"],
                "monitoria_id": p["p_monitoria_id"],
                "aluno_matricula": p["p_aluno_matricula"],
                "aluno_nome": p["p_aluno_nome"],
                "data": p["p_data"],
                "assunto": p["p_assunto"],
                "situacao": p["p_situacao"],
                "criado_em": _como_utc(p["p_criado_em"]),
            }
            hist = {
                "id": c.sequencias["historico_situacoes"],
                "agendamento_id": ag["id"],
                "situacao_anterior": p["p_situacao_anterior"],
                "situacao_nova": p["p_situacao_nova"],
                "registrada_em": _como_utc(p["p_registrada_em"]),
            }
            c.tabelas["agendamentos"].append(ag)
            c.tabelas["historico_situacoes"].append(hist)
            return SimpleNamespace(data={"agendamento": dict(ag), "historico": dict(hist)})

        if self._nome == "registrar_mudanca_situacao":
            ag = next((a for a in c.tabelas["agendamentos"] if a["id"] == p["p_agendamento_id"]), None)
            if ag is None:
                raise _ApiErroFalso("P0002", f"Agendamento com ID {p['p_agendamento_id']} não encontrado.")
            if p["p_situacao_anterior"] is not None and ag["situacao"] != p["p_situacao_anterior"]:
                raise _ApiErroFalso("40001", "Conflito de situação no agendamento")
            if p["p_situacao_nova"] not in repositorios.SITUACOES_VALIDAS:
                raise _ApiErroFalso("23514", "violates check constraint ck_agendamentos_situacao_valida")

            anterior = ag["situacao"]
            ag["situacao"] = p["p_situacao_nova"]
            c.sequencias["historico_situacoes"] += 1
            hist = {
                "id": c.sequencias["historico_situacoes"],
                "agendamento_id": ag["id"],
                "situacao_anterior": p["p_situacao_anterior"] or anterior,
                "situacao_nova": p["p_situacao_nova"],
                "registrada_em": _como_utc(p["p_registrada_em"]),
            }
            c.tabelas["historico_situacoes"].append(hist)
            return SimpleNamespace(data={"agendamento": dict(ag), "historico": dict(hist)})

        raise AssertionError(f"RPC desconhecida: {self._nome}")


class ClienteSupabaseFalso:
    def __init__(self, *, funcoes_instaladas: bool = True) -> None:
        self.funcoes_instaladas = funcoes_instaladas
        self.falhar_historico = False
        self.tabelas: dict[str, list[dict[str, Any]]] = {
            "monitorias": [], "agendamentos": [], "historico_situacoes": [],
        }
        self.sequencias = {"monitorias": 0, "agendamentos": 0, "historico_situacoes": 0}

    def table(self, nome: str) -> _QueryBuilderFalso:
        return _QueryBuilderFalso(self, nome)

    def rpc(self, nome: str, parametros: dict[str, Any] | None = None) -> _RpcFalso:
        return _RpcFalso(self, nome, parametros or {})

    @staticmethod
    def validar_monitoria(payload: dict[str, Any]) -> dict[str, Any]:
        """CHECKs da tabela monitorias; hora volta como o PostgreSQL devolve (HH:MM:SS)."""
        if payload["vagas"] <= 0 or not 0 <= payload["dia_semana"] <= 5:
            raise _ApiErroFalso("23514", "violates check constraint")
        linha = dict(payload)
        linha["hora_inicio"] = f"{payload['hora_inicio']}:00"
        linha["hora_fim"] = f"{payload['hora_fim']}:00"
        return linha





class _ServicoMonitorias:
    def __init__(self, repo: MonitoriaRepositorio) -> None:
        self._repo = repo

    def cadastrar_monitoria(self, disciplina, monitor, dia, inicio, fim, local, vagas):
        return self._repo.adicionar(
            MonitoriaModelo(
                None, disciplina, monitor, int(dia) - 1,
                time.fromisoformat(inicio), time.fromisoformat(fim), local, int(vagas),
            )
        )

    def listar_monitorias(self):
        return self._repo.listar_todas()


class _ServicoAgendamentos:
    def __init__(self, repo: AgendamentoRepositorio, repo_monitorias: MonitoriaRepositorio):
        self._repo, self._monitorias = repo, repo_monitorias

    def agendar(self, matricula, nome, codigo_monitoria, data, assunto, agora=None):
        agora = agora or datetime.now()
        monitoria = self._monitorias.buscar_por_id(int(codigo_monitoria))
        dia = datetime.strptime(data, "%d/%m/%Y").date()
        assert dia >= agora.date() and dia.weekday() == monitoria.dia_semana, "data inválida"
        assert not self._repo.existe_ativo(matricula, monitoria.id, dia), "duplicado"
        assert self._repo.contar_ativos(monitoria.id, dia) < monitoria.vagas, "lotada"
        agendamento = AgendamentoModelo(
            None, monitoria.id, matricula, nome, dia, assunto, "solicitado", agora
        )
        mudanca = MudancaSituacaoModelo(None, None, None, "solicitado", agora)
        return self._repo.adicionar(agendamento, mudanca)

    def alterar_situacao(self, numero, nova_situacao, agora=None):
        agendamento = self._repo.buscar_por_id(int(numero))
        anterior = agendamento.situacao
        agendamento.situacao = nova_situacao
        mudanca = MudancaSituacaoModelo(
            None, agendamento.id, anterior, nova_situacao, agora or datetime.now()
        )
        return self._repo.registrar_mudanca(agendamento, mudanca)

    def listar_agendamentos(self):
        return self._repo.listar_todos()





def _monitoria(vagas: int = 3, dia: int = 0) -> MonitoriaModelo:
    return MonitoriaModelo(None, "Banco de Dados", "Samuel", dia, time(14), time(16), "Lab 03", vagas)


def _agendamento(monitoria_id: int, matricula: str = "2025100", data: date = date(2030, 1, 7)):
    ag = AgendamentoModelo(
        None, monitoria_id, matricula, "Maria", data, "Normalização",
        "solicitado", datetime(2026, 11, 1, 14, 30, 15),
    )
    mud = MudancaSituacaoModelo(None, None, None, "solicitado", datetime(2026, 11, 1, 14, 30, 15))
    return ag, mud


def _jwt(payload: dict[str, Any]) -> str:
    def b64(dados: bytes) -> str:
        return base64.urlsafe_b64encode(dados).rstrip(b"=").decode()

    return ".".join(
        [b64(b'{"alg":"HS256","typ":"JWT"}'), b64(json.dumps(payload).encode()), "assinatura"]
    )


class _BaseComBanco(unittest.TestCase):
    def setUp(self) -> None:
        repositorios._resolver_dominio.cache_clear()
        self.cliente = ClienteSupabaseFalso()
        self.repo_mon = MonitoriaRepositorio(cliente=self.cliente)
        self.repo_ag = AgendamentoRepositorio(cliente=self.cliente)

    def tearDown(self) -> None:
        repositorios._resolver_dominio.cache_clear()

    def _contagens(self) -> tuple[int, int]:
        return len(self.cliente.tabelas["agendamentos"]), len(self.cliente.tabelas["historico_situacoes"])





class TesteConfiguracao(unittest.TestCase):
    URL = "https://xyzcompany.supabase.co"

    def test_rejeita_vazio_e_placeholder(self) -> None:
        with self.assertRaises(ErroConfiguracaoBanco):
            ConfiguracaoSupabase(url="", key="").validar()
        with self.assertRaises(ErroConfiguracaoBanco):
            ConfiguracaoSupabase(
                url="https://seu-projeto.supabase.co",
                key="sua-chave-anon-ou-publishable-aqui",
            ).validar()

    def test_rejeita_url_invalida(self) -> None:
        with self.assertRaises(ErroConfiguracaoBanco):
            ConfiguracaoSupabase(url="ftp://x", key=_jwt({"role": "anon"})).validar()

    def test_aceita_chave_anon_e_publishable(self) -> None:
        ConfiguracaoSupabase(url=self.URL, key=_jwt({"role": "anon"})).validar()
        ConfiguracaoSupabase(url=self.URL, key="sb_publishable_abc123").validar()

    def test_recusa_chave_service_role_nos_dois_formatos(self) -> None:
        for chave in (_jwt({"role": "service_role"}), "sb_secret_abc123"):
            with self.subTest(chave=chave[:12]):
                with self.assertRaisesRegex(ErroConfiguracaoBanco, "service_role"):
                    ConfiguracaoSupabase(url=self.URL, key=chave).validar()

    def test_erros_da_camada_sao_persistencia_error(self) -> None:
       
        self.assertTrue(issubclass(ErroConfiguracaoBanco, PersistenciaError))
        self.assertTrue(issubclass(ErroIntegridadeBanco, PersistenciaError))
        self.assertEqual(str(ErroBancoDados("texto claro")), "texto claro")




class TesteMonitoriaRepositorio(_BaseComBanco):
    def test_adicionar_buscar_listar_devolvem_tipos_do_dominio(self) -> None:
        mon = self.repo_mon.adicionar(_monitoria(dia=0))
        self.assertEqual(mon.id, 1)

        lida = self.repo_mon.buscar_por_id(1)
        self.assertEqual(lida.dia_semana, 0)  
        self.assertEqual((lida.hora_inicio, lida.hora_fim), (time(14, 0), time(16, 0)))
        self.assertEqual(len(self.repo_mon.listar_todas()), 1)
        self.assertIsNone(self.repo_mon.buscar_por_id(99))

    def test_restricoes_recusadas_antes_de_chegar_ao_banco(self) -> None:
        invalidas = {
            "vagas 0": MonitoriaModelo(None, "X", "Y", 1, time(10), time(12), "L", 0),
            "domingo": MonitoriaModelo(None, "X", "Y", 6, time(10), time(12), "L", 3),
            "dia negativo": MonitoriaModelo(None, "X", "Y", -1, time(10), time(12), "L", 3),
            "fim antes do início": MonitoriaModelo(None, "X", "Y", 1, time(12), time(10), "L", 3),
            "disciplina vazia": MonitoriaModelo(None, "  ", "Y", 1, time(10), time(12), "L", 3),
        }
        for nome, monitoria in invalidas.items():
            with self.subTest(nome), self.assertRaises(ErroIntegridadeBanco):
                self.repo_mon.adicionar(monitoria)
        self.assertEqual(self.cliente.tabelas["monitorias"], [])

    def test_check_do_banco_vira_erro_de_integridade(self) -> None:
        
        with mock.patch.object(repositorios, "_normalizar_dia_semana", return_value=9):
            with self.assertRaises(ErroIntegridadeBanco):
                self.repo_mon.adicionar(_monitoria())




class TesteAgendamentoRepositorio(_BaseComBanco):
    def setUp(self) -> None:
        super().setUp()
        self.mon = self.repo_mon.adicionar(_monitoria(vagas=2))

    def test_adicionar_grava_agendamento_e_historico_juntos(self) -> None:
        ag, mud = _agendamento(self.mon.id)
        resultado = self.repo_ag.adicionar(ag, mud)

        self.assertIs(resultado, ag)
        self.assertEqual((ag.id, mud.id, mud.agendamento_id), (1, 1, 1))
        self.assertEqual(self._contagens(), (1, 1))
        self.assertEqual(ag.historico, [mud])

        lido = self.repo_ag.buscar_por_id(1)
        self.assertEqual(lido.data, date(2030, 1, 7))
        
        self.assertEqual(lido.criado_em, datetime(2026, 11, 1, 14, 30, 15))
        self.assertEqual(lido.historico[0].registrada_em, datetime(2026, 11, 1, 14, 30, 15))
        self.assertIsNone(lido.historico[0].situacao_anterior)

    def test_filtros_e_contagem(self) -> None:
        self.repo_ag.adicionar(*_agendamento(self.mon.id, "111"))
        self.repo_ag.adicionar(*_agendamento(self.mon.id, "222"))
        self.repo_ag.adicionar(*_agendamento(self.mon.id, "111", date(2030, 1, 14)))

        self.assertEqual(self.repo_ag.contar_ativos(self.mon.id, "2030-01-07"), 2)
        self.assertEqual(self.repo_ag.contar_ativos(self.mon.id, date(2030, 1, 14)), 1)
        self.assertTrue(self.repo_ag.existe_ativo("111", self.mon.id, date(2030, 1, 7)))
        self.assertFalse(self.repo_ag.existe_ativo("333", self.mon.id, date(2030, 1, 7)))
        self.assertEqual(len(self.repo_ag.listar_por_matricula("111")), 2)
        self.assertEqual(self.repo_ag.listar_por_matricula("  "), [])
        self.assertEqual(len(self.repo_ag.listar_por_monitoria(self.mon.id)), 3)
        self.assertEqual(len(self.repo_ag.listar_por_situacao("solicitado")), 3)
        self.assertEqual(self.repo_ag.listar_por_situacao("cancelado"), [])
        with self.assertRaises(ErroIntegridadeBanco):
            self.repo_ag.listar_por_situacao("inventada")

    def test_registrar_mudanca_atualiza_situacao_e_historico(self) -> None:
        ag, mud = _agendamento(self.mon.id)
        self.repo_ag.adicionar(ag, mud)

        for anterior, nova in (("solicitado", "confirmado"), ("confirmado", "cancelado")):
            ag.situacao = nova
            self.repo_ag.registrar_mudanca(
                ag, MudancaSituacaoModelo(None, ag.id, anterior, nova, datetime.now())
            )

        lido = self.repo_ag.buscar_por_id(ag.id)
        self.assertEqual(lido.situacao, "cancelado")
        self.assertEqual(
            [(h.situacao_anterior, h.situacao_nova) for h in lido.historico],
            [(None, "solicitado"), ("solicitado", "confirmado"), ("confirmado", "cancelado")],
        )
        
        self.assertEqual(self.repo_ag.contar_ativos(self.mon.id, "2030-01-07"), 0)
        self.assertFalse(self.repo_ag.existe_ativo("2025100", self.mon.id, "2030-01-07"))

    def test_conflito_de_situacao_nao_altera_nada(self) -> None:
        ag, mud = _agendamento(self.mon.id)
        self.repo_ag.adicionar(ag, mud)
        antes = self._contagens()

        ag.situacao = "cancelado"
        desatualizada = MudancaSituacaoModelo(None, ag.id, "confirmado", "cancelado", datetime.now())
        with self.assertRaises(ErroIntegridadeBanco):
            self.repo_ag.registrar_mudanca(ag, desatualizada)

        self.assertEqual(self._contagens(), antes)
        self.assertEqual(self.cliente.tabelas["agendamentos"][0]["situacao"], "solicitado")

    def test_situacao_invalida_nao_chega_ao_banco(self) -> None:
        ag, mud = _agendamento(self.mon.id)
        self.repo_ag.adicionar(ag, mud)
        with self.assertRaises(ErroIntegridadeBanco):
            self.repo_ag.registrar_mudanca(
                ag, MudancaSituacaoModelo(None, ag.id, "solicitado", "inventada", datetime.now())
            )
        self.assertEqual(self._contagens(), (1, 1))

    def test_falha_no_historico_nao_deixa_agendamento_orfao(self) -> None:
        self.cliente.falhar_historico = True
        with self.assertRaises(ErroIntegridadeBanco):
            self.repo_ag.adicionar(*_agendamento(self.mon.id))
        self.assertEqual(self._contagens(), (0, 0))

    def test_sem_a_funcao_sql_recusa_em_vez_de_gravar_sem_atomicidade(self) -> None:
        cliente = ClienteSupabaseFalso(funcoes_instaladas=False)
        repo = AgendamentoRepositorio(cliente=cliente)
        with self.assertRaisesRegex(ErroConfiguracaoBanco, "schema_supabase.sql"):
            repo.adicionar(*_agendamento(1))
        self.assertEqual(cliente.tabelas["agendamentos"], [])

    def test_inconsistencia_entre_situacao_e_mudanca(self) -> None:
        ag, _ = _agendamento(self.mon.id)
        mud = MudancaSituacaoModelo(None, None, None, "confirmado", datetime.now())
        with self.assertRaises(ErroIntegridadeBanco):
            self.repo_ag.adicionar(ag, mud)
        self.assertEqual(self._contagens(), (0, 0))

    def test_gravar_direto_nas_tabelas_e_negado_pelo_banco(self) -> None:
       
        with self.assertRaises(_ApiErroFalso):
            self.cliente.table("agendamentos").insert({"x": 1}).execute()


class TesteCorridas(_BaseComBanco):
    """Dois pedidos passam pela checagem do serviço; o banco decide quem fica."""

    def setUp(self) -> None:
        super().setUp()
        self.mon = self.repo_mon.adicionar(_monitoria(vagas=1))
        self.repo_ag.adicionar(*_agendamento(self.mon.id, "111"))

    def test_lotada_sem_excecoes_py_vira_erro_de_integridade(self) -> None:
        with self.assertRaisesRegex(ErroIntegridadeBanco, "lotada"):
            self.repo_ag.adicionar(*_agendamento(self.mon.id, "222"))
        self.assertEqual(self._contagens(), (1, 1))

    def test_lotada_e_duplicado_viram_os_erros_do_dominio_quando_existem(self) -> None:
        class MonitoriaLotadaError(PersistenciaError):
            def __init__(self, monitoria_id, data):
                super().__init__(f"lotada {monitoria_id} {data}")
                self.monitoria_id, self.data = monitoria_id, data

        class AgendamentoDuplicadoError(PersistenciaError):
            def __init__(self, matricula, monitoria_id, data):
                super().__init__("duplicado")
                self.matricula, self.monitoria_id, self.data = matricula, monitoria_id, data

        excecoes = types.SimpleNamespace(
            MonitoriaLotadaError=MonitoriaLotadaError,
            AgendamentoDuplicadoError=AgendamentoDuplicadoError,
        )
        with mock.patch.dict(sys.modules, {"excecoes": excecoes}):
            with self.assertRaises(MonitoriaLotadaError) as lotada:
                self.repo_ag.adicionar(*_agendamento(self.mon.id, "222"))
            self.assertEqual((lotada.exception.monitoria_id, lotada.exception.data), (self.mon.id, date(2030, 1, 7)))

            with self.assertRaises(AgendamentoDuplicadoError) as duplicado:
                self.repo_ag.adicionar(*_agendamento(self.mon.id, "111"))
            self.assertEqual(duplicado.exception.matricula, "111")





def _modelos_falsos(*, frozen: bool) -> types.ModuleType:
    """modelos.py como a documentação descreve: Agendamento(None, monitoria_id,
    matricula, nome, data, assunto, Situacao.SOLICITADO, agora)."""
    modulo = types.ModuleType("modelos")

    class Situacao(enum.Enum):
        SOLICITADO = "solicitado"
        CONFIRMADO = "confirmado"
        REALIZADO = "realizado"
        CANCELADO = "cancelado"

    @dataclass
    class Monitoria:
        id: int | None
        disciplina: str
        monitor: str
        dia_semana: int
        hora_inicio: time
        hora_fim: time
        local: str
        vagas: int

    @dataclass(frozen=frozen)
    class Agendamento:
        id: int | None
        monitoria_id: int
        aluno_matricula: str
        aluno_nome: str
        data: date
        assunto: str
        situacao: Situacao
        criado_em: datetime
        historico: list = field(default_factory=list)

    @dataclass
    class MudancaSituacao:
        agendamento_id: int | None
        situacao_anterior: Situacao | None
        situacao_nova: Situacao
        registrada_em: datetime
        id: int | None = None

    modulo.Situacao, modulo.Monitoria = Situacao, Monitoria
    modulo.Agendamento, modulo.MudancaSituacao = Agendamento, MudancaSituacao
    return modulo


class TesteIntegracaoComDominio(_BaseComBanco):
    def _rodar(self, *, frozen: bool) -> None:
        modelos = _modelos_falsos(frozen=frozen)
        S = modelos.Situacao
        agora = datetime(2026, 11, 1, 14, 30)
        with mock.patch.dict(sys.modules, {"modelos": modelos}):
            repositorios._resolver_dominio.cache_clear()
            mon = self.repo_mon.adicionar(
                modelos.Monitoria(None, "BD", "Samuel", 0, time(14), time(16), "Lab", 2)
            )
            self.assertIsInstance(self.repo_mon.buscar_por_id(mon.id), modelos.Monitoria)

            ag = modelos.Agendamento(None, mon.id, "2025001", "Ana", date(2030, 1, 7), "x", S.SOLICITADO, agora)
            mud = modelos.MudancaSituacao(None, None, S.SOLICITADO, agora)
            ag = self.repo_ag.adicionar(ag, mud)  # como o serviço: devolve o resultado

            self.assertEqual(ag.id, 1)
            lido = self.repo_ag.buscar_por_id(ag.id)
            self.assertIsInstance(lido, modelos.Agendamento)
            self.assertIs(lido.situacao, S.SOLICITADO)
            self.assertIsInstance(lido.historico[0], modelos.MudancaSituacao)
            self.assertIs(lido.historico[0].situacao_nova, S.SOLICITADO)
            self.assertEqual(len(self.repo_ag.listar_por_situacao(S.SOLICITADO)), 1)

            
            ag = _trocar(ag, situacao=S.CONFIRMADO, frozen=frozen)
            mud2 = modelos.MudancaSituacao(ag.id, S.SOLICITADO, S.CONFIRMADO, agora)
            ag = self.repo_ag.registrar_mudanca(ag, mud2)
            self.assertIs(ag.situacao, S.CONFIRMADO)
            self.assertIs(self.repo_ag.buscar_por_id(ag.id).situacao, S.CONFIRMADO)

    def test_classes_mutaveis(self) -> None:
        self._rodar(frozen=False)

    def test_classes_imutaveis_devolvem_copia_com_id(self) -> None:
        self._rodar(frozen=True)

    def test_campo_que_o_dominio_exige_e_a_tabela_nao_tem(self) -> None:
        modelos = _modelos_falsos(frozen=False)

        @dataclass
        class MonitoriaComCampoExtra:
            id: int
            disciplina: str
            campo_inexistente: str

        modelos.Monitoria = MonitoriaComCampoExtra
        self.repo_mon.adicionar(_monitoria())
        with mock.patch.dict(sys.modules, {"modelos": modelos}):
            repositorios._resolver_dominio.cache_clear()
            with self.assertRaisesRegex(PersistenciaError, "campo_inexistente"):
                self.repo_mon.listar_todas()


def _trocar(objeto: Any, *, frozen: bool, **campos: Any) -> Any:
    if frozen:
        import dataclasses

        return dataclasses.replace(objeto, **campos)
    for nome, valor in campos.items():
        setattr(objeto, nome, valor)
    return objeto





class TesteDadosExemplo(_BaseComBanco):
    def setUp(self) -> None:
        super().setUp()
        self.servico_mon = _ServicoMonitorias(self.repo_mon)
        self.servico_ag = _ServicoAgendamentos(self.repo_ag, self.repo_mon)

    def test_insere_so_com_banco_vazio_e_nao_duplica(self) -> None:
        agora = datetime(2026, 11, 1, 14, 30)  # um domingo
        primeiro = popular_dados_exemplo(self.servico_mon, self.servico_ag, agora=agora)
        self.assertEqual(primeiro, {"monitorias": 3, "agendamentos": 4})

        segundo = popular_dados_exemplo(self.servico_mon, self.servico_ag, agora=agora)
        self.assertEqual(segundo, {"monitorias": 0, "agendamentos": 0})
        self.assertEqual(len(self.repo_mon.listar_todas()), 3)
        self.assertEqual(len(self.repo_ag.listar_todos()), 4)

    def test_datas_futuras_no_dia_da_semana_da_monitoria(self) -> None:
        agora = datetime(2026, 11, 1, 14, 30)
        popular_dados_exemplo(self.servico_mon, self.servico_ag, agora=agora)

        monitorias = {m.id: m for m in self.repo_mon.listar_todas()}
        for ag in self.repo_ag.listar_todos():
            self.assertGreater(ag.data, agora.date())
            self.assertEqual(ag.data.weekday(), monitorias[ag.monitoria_id].dia_semana)

    def test_situacoes_e_historico_dos_exemplos(self) -> None:
        popular_dados_exemplo(self.servico_mon, self.servico_ag, agora=datetime(2026, 11, 1, 9, 0))
        resumo = [
            (a.situacao, [h.situacao_nova for h in a.historico])
            for a in self.repo_ag.listar_todos()
        ]
        self.assertCountEqual(
            resumo,
            [
                ("solicitado", ["solicitado"]),
                ("confirmado", ["solicitado", "confirmado"]),
                ("realizado", ["solicitado", "confirmado", "realizado"]),
                ("cancelado", ["solicitado", "cancelado"]),
            ],
        )

    def test_calcular_proxima_data(self) -> None:
        segunda = date(2026, 11, 2)
        self.assertEqual(calcular_proxima_data(0, referencia=segunda), date(2026, 11, 9))  # nunca "hoje"
        self.assertEqual(calcular_proxima_data(2, referencia=segunda), date(2026, 11, 4))
        self.assertEqual(calcular_proxima_data(0, referencia=date(2026, 11, 1)), date(2026, 11, 2))


if __name__ == "__main__":
    unittest.main()
