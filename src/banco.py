from __future__ import annotations

import base64
import json
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:  
    load_dotenv = None  
try:
    from supabase import create_client
except ImportError:
    create_client = None  

if TYPE_CHECKING:
    from supabase import Client as SupabaseClient
else:
    SupabaseClient = Any


try:
    from excecoes import PersistenciaError
except ImportError:  
    class PersistenciaError(RuntimeError):  
        """Substituto local, usado só quando excecoes.py não está no projeto."""


class ErroBancoDados(PersistenciaError):
    """Base de todos os erros desta camada; para a interface é um PersistenciaError."""

    def __init__(self, mensagem: str = "") -> None:

        Exception.__init__(self, mensagem)
        self.mensagem = mensagem

    def __str__(self) -> str:
        return self.mensagem


class ErroConfiguracaoBanco(ErroBancoDados):
    """Credenciais ausentes, inválidas ou perigosas; ou esquema não aplicado."""


class ErroConexaoBanco(ErroBancoDados):
    """Falha ao inicializar o cliente ou ao falar com o servidor Supabase."""


class ErroOperacaoBanco(ErroBancoDados):
    """Erro de execução em consulta, inserção, atualização ou RPC."""


class ErroIntegridadeBanco(ErroOperacaoBanco):
    """Violação de FK, CHECK, UNIQUE, NOT NULL ou conflito de situação."""



TABELA_MONITORIAS = "monitorias"
TABELA_AGENDAMENTOS = "agendamentos"
TABELA_HISTORICO_SITUACOES = "historico_situacoes"

RPC_ADICIONAR_AGENDAMENTO = "adicionar_agendamento_com_historico"
RPC_REGISTRAR_MUDANCA = "registrar_mudanca_situacao"


CODIGO_SQL_MONITORIA_LOTADA = "CC001"

_VALORES_PLACEHOLDER = {
    "https://seu-projeto.supabase.co",
    "sua-chave-anon-ou-publishable-aqui",
    "sua-chave-anon-ou-service-role-aqui",
}



def _chave_e_privilegiada(chave: str) -> bool:
  
    if chave.startswith("sb_secret_"):
        return True

    partes = chave.split(".")
    if len(partes) != 3:
        return False
    try:
        payload_b64 = partes[1] + "=" * (-len(partes[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("role") == "service_role"


@dataclass(frozen=True, slots=True)
class ConfiguracaoSupabase:
    """Credenciais de acesso ao Supabase, já validadas por ``validar()``."""

    url: str
    key: str

    def validar(self) -> None:
        url_limpa = (self.url or "").strip()
        key_limpa = (self.key or "").strip()

        ausentes = [
            nome
            for nome, valor in (("SUPABASE_URL", url_limpa), ("SUPABASE_KEY", key_limpa))
            if not valor
        ]
        if ausentes:
            raise ErroConfiguracaoBanco(
                f"Variáveis de ambiente não configuradas: {', '.join(ausentes)}. "
                "Copie '.env.example' para '.env' e preencha com os dados de "
                "Project Settings -> API do Supabase."
            )

        if url_limpa in _VALORES_PLACEHOLDER or key_limpa in _VALORES_PLACEHOLDER:
            raise ErroConfiguracaoBanco(
                "SUPABASE_URL e SUPABASE_KEY ainda estão com os valores de exemplo. "
                "Substitua-os pelas credenciais reais do projeto no arquivo '.env'."
            )

        if _chave_e_privilegiada(key_limpa):
            raise ErroConfiguracaoBanco(
                "SUPABASE_KEY é uma chave service_role/secret, que ignora o RLS e "
                "dá acesso total ao banco. Use a chave 'anon' (ou 'publishable') "
                "do painel do Supabase."
            )

        parsed = urlparse(url_limpa)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ErroConfiguracaoBanco(
                f"SUPABASE_URL inválida: '{url_limpa}'. "
                "Esperado algo como https://<projeto>.supabase.co."
            )


def _carregar_env_fallback(caminho_env: Path) -> None:
    """Lê um .env só com a biblioteca padrão, caso python-dotenv não exista."""
    if not caminho_env.is_file():
        return

    for linha_bruta in caminho_env.read_text(encoding="utf-8").splitlines():
        linha = linha_bruta.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave = chave.strip().removeprefix("export ").strip()
        valor = valor.strip().strip('"').strip("'")
        if chave and chave not in os.environ:
            os.environ[chave] = valor


def _candidatos_env(caminho_env: str | Path | None) -> list[Path]:
    if caminho_env is not None:
        return [Path(caminho_env)]

    
    diretorio_modulo = Path(__file__).resolve().parent
    candidatos = [
        diretorio_modulo / ".env",
        diretorio_modulo.parent / ".env",
        Path.cwd().resolve() / ".env",
    ]
    unicos: list[Path] = []
    for candidato in candidatos:
        if candidato not in unicos:
            unicos.append(candidato)
    return unicos


def carregar_configuracao(
    caminho_env: str | Path | None = None,
    *,
    sobrescrever: bool = False,
) -> ConfiguracaoSupabase:
    """Carrega o ``.env`` e devolve a configuração validada.

    Variáveis lidas: ``SUPABASE_URL`` e ``SUPABASE_KEY`` (ou ``SUPABASE_ANON_KEY``).
    A chave ``service_role`` nunca é aceita, nem como alternativa.
    """
    for arquivo in _candidatos_env(caminho_env):
        if not arquivo.is_file():
            continue
        if load_dotenv is not None:
            load_dotenv(dotenv_path=arquivo, override=sobrescrever)
        else:
            _carregar_env_fallback(arquivo)

    url = os.environ.get("SUPABASE_URL", "").strip()
    key = (
        os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_ANON_KEY") or ""
    ).strip()

    config = ConfiguracaoSupabase(url=url, key=key)
    config.validar()
    return config



class BancoDados:
    """Ponto único de acesso ao cliente Supabase usado pelos repositórios."""

    _cliente_padrao: SupabaseClient | None = None

    def __init__(
        self,
        cliente: SupabaseClient | None = None,
        *,
        url: str | None = None,
        key: str | None = None,
        caminho_env: str | Path | None = None,
    ) -> None:
        self._cliente: SupabaseClient | None = cliente
        self._url = url
        self._key = key
        self._caminho_env = caminho_env

    @property
    def cliente(self) -> SupabaseClient:
        """Cliente ativo. Sem cliente injetado, usa o compartilhado (criado sob demanda)."""
        if self._cliente is None:
            self._cliente = self._criar_cliente()
        return self._cliente

    def _criar_cliente(self) -> SupabaseClient:
        if self._url is not None or self._key is not None:
            config = ConfiguracaoSupabase(url=self._url or "", key=self._key or "")
            config.validar()
        else:
            config = carregar_configuracao(self._caminho_env)

        if create_client is None:
            raise ErroConfiguracaoBanco(
                "A biblioteca 'supabase' não está instalada. "
                "Execute: pip install -r requirements.txt"
            )

        try:
            return create_client(config.url, config.key)
        except Exception as exc:
            raise ErroConexaoBanco(
                f"Não foi possível inicializar o cliente Supabase: {exc}"
            ) from exc

    def tabela(self, nome_tabela: str) -> Any:
        """Atalho para ``cliente.table(nome_tabela)``."""
        try:
            return self.cliente.table(nome_tabela)
        except ErroBancoDados:
            raise
        except Exception as exc:
            raise ErroConexaoBanco(
                f"Falha ao acessar a tabela '{nome_tabela}' no Supabase: {exc}"
            ) from exc

    def rpc(self, nome_funcao: str, parametros: dict[str, Any] | None = None) -> Any:
        """Atalho para ``cliente.rpc``: chama uma função PostgreSQL (uma transação)."""
        try:
            if parametros is None:
                return self.cliente.rpc(nome_funcao)
            return self.cliente.rpc(nome_funcao, parametros)
        except ErroBancoDados:
            raise
        except Exception as exc:
            raise ErroConexaoBanco(
                f"Falha ao preparar a chamada da função '{nome_funcao}': {exc}"
            ) from exc

    def verificar_conexao(self) -> bool:
        """Confirma que o cliente alcança o banco e que o esquema foi aplicado."""
        try:
            self.tabela(TABELA_MONITORIAS).select("id").limit(1).execute()
            return True
        except ErroBancoDados:
            raise
        except Exception as exc:
            raise ErroConexaoBanco(
                "Não foi possível consultar o Supabase. Confirme SUPABASE_URL e "
                "SUPABASE_KEY no '.env' e se 'schema_supabase.sql' já foi executado "
                f"no SQL Editor. Detalhes: {exc}"
            ) from exc

    def fechar(self) -> None:
        """Solta o cliente deste gerenciador (o supabase-py não mantém conexão aberta)."""
        self._cliente = None

    @classmethod
    def obter_cliente(
        cls,
        *,
        forcar_novo: bool = False,
        caminho_env: str | Path | None = None,
    ) -> SupabaseClient:
        """Cliente compartilhado (singleton), criado na primeira chamada."""
        if cls._cliente_padrao is None or forcar_novo:
            cls._cliente_padrao = cls(caminho_env=caminho_env).cliente
        return cls._cliente_padrao

    @classmethod
    def definir_cliente(cls, cliente: SupabaseClient | None) -> None:
        """Injeta ou limpa o cliente compartilhado (útil em testes)."""
        cls._cliente_padrao = cliente



def obter_cliente(
    *,
    forcar_novo: bool = False,
    caminho_env: str | Path | None = None,
) -> SupabaseClient:
    """Cliente Supabase compartilhado, usado pelos repositórios."""
    return BancoDados.obter_cliente(forcar_novo=forcar_novo, caminho_env=caminho_env)


def obter_conexao(
    *,
    forcar_novo: bool = False,
    caminho_env: str | Path | None = None,
) -> SupabaseClient:
    """Alias de ``obter_cliente`` para código escrito na época do SQLite."""
    return obter_cliente(forcar_novo=forcar_novo, caminho_env=caminho_env)


def inicializar_banco(
    cliente: SupabaseClient | None = None,
    *,
    verificar: bool = True,
    caminho_env: str | Path | None = None,
) -> SupabaseClient:
    """Abre a conexão (é o que o main.py chama) e, por padrão, testa o acesso.

    No Supabase as tabelas não são criadas pelo Python: execute
    ``schema_supabase.sql`` uma vez no SQL Editor. Com ``verificar=True`` um
    esquema ausente aparece aqui, com instrução clara, e não no meio do menu.
    """
    if cliente is not None:
        BancoDados.definir_cliente(cliente)
    ativo = BancoDados(
        cliente=cliente or BancoDados.obter_cliente(caminho_env=caminho_env)
    )
    if verificar:
        ativo.verificar_conexao()
    return ativo.cliente


def verificar_conexao(cliente: SupabaseClient | None = None) -> bool:
    """Testa a conectividade e a existência das tabelas."""
    return BancoDados(cliente=cliente or BancoDados.obter_cliente()).verificar_conexao()


def fechar_conexao() -> None:
    """Descarta o cliente compartilhado."""
    BancoDados.definir_cliente(None)


@contextmanager
def conexao_banco(
    cliente: SupabaseClient | None = None,
    *,
    caminho_env: str | Path | None = None,
) -> Iterator[SupabaseClient]:
    """Uso: ``with conexao_banco() as db: ...`` — descarta o cliente ao sair."""
    ativo = cliente or obter_cliente(caminho_env=caminho_env)
    try:
        yield ativo
    finally:
        if cliente is None:
            fechar_conexao()


__all__ = [
    "BancoDados",
    "CODIGO_SQL_MONITORIA_LOTADA",
    "ConfiguracaoSupabase",
    "ErroBancoDados",
    "ErroConexaoBanco",
    "ErroConfiguracaoBanco",
    "ErroIntegridadeBanco",
    "ErroOperacaoBanco",
    "PersistenciaError",
    "RPC_ADICIONAR_AGENDAMENTO",
    "RPC_REGISTRAR_MUDANCA",
    "TABELA_AGENDAMENTOS",
    "TABELA_HISTORICO_SITUACOES",
    "TABELA_MONITORIAS",
    "carregar_configuracao",
    "conexao_banco",
    "fechar_conexao",
    "inicializar_banco",
    "obter_cliente",
    "obter_conexao",
    "verificar_conexao",
]
