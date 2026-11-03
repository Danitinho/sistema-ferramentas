"""
tests/ambiente.py
=================
Monta o app apontando TODOS os bancos para uma pasta temporária.

Nunca suba o app de teste sobre `dados/`: o serviço da porta 80 está com os
bancos abertos e importar o `app` roda migrações (CLAUDE.md, seção 8). Aqui
cada módulo recebe um caminho dentro de `tmp` ANTES de `import app`, o
agendador de backup não sobe e o backup grava noutra pasta temporária.

Uso:
    from tests.ambiente import montar
    amb = montar()            # cria tudo; idempotente por processo
    cli = amb.cliente("admin")   # test_client já logado
"""
import os
import sys
import tempfile

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if RAIZ not in sys.path:
    sys.path.insert(0, RAIZ)

_amb = None


class Ambiente:
    def __init__(self):
        self.tmp = tempfile.mkdtemp(prefix="sf_teste_")
        self.dados = os.path.join(self.tmp, "dados")
        os.makedirs(os.path.join(self.dados, "relatorios"), exist_ok=True)
        self._apontar()
        from app import app
        app.config["TESTING"] = True
        self.app = app

    def caminho(self, nome):
        return os.path.join(self.dados, nome)

    def _apontar(self):
        from pathlib import Path
        from scripts import (auth, catalogo, debitos, fornecedores, vencidos,
                             relatorios_vendas, backup, certificado)
        auth.DADOS_DIR = self.dados
        auth.BANCO = self.caminho("sistema.db")
        auth.SECRET_ARQ = self.caminho("secret.key")
        for mod, nome in ((catalogo, "catalogo.db"), (debitos, "debitos.db"),
                          (fornecedores, "fornecedores.db"), (vencidos, "vencidos.db")):
            mod.DADOS_DIR = self.dados
            mod.BANCO = self.caminho(nome)
        rv = os.path.join(self.dados, "relatorios")
        relatorios_vendas.RAIZ = rv
        relatorios_vendas.BANCO = os.path.join(rv, "vendas.db")
        for attr, sub in (("DIR_ENTRADA", "entrada"), ("DIR_PROCESSADO", "processados"),
                          ("DIR_SAIDA", "saida")):
            if hasattr(relatorios_vendas, attr):
                setattr(relatorios_vendas, attr, os.path.join(rv, sub))
        backup.DADOS_DIR = self.dados
        backup.BACKUP_DIR = os.path.join(self.tmp, "backups")
        backup.iniciar_agendador = lambda *a, **k: None
        certificado.DIR_TLS = os.path.join(self.dados, "tls")
        for attr, arq in (("CA_CRT", "ca.crt"), ("CA_KEY", "ca.key"),
                          ("SRV_CRT", "servidor.crt"), ("SRV_KEY", "servidor.key")):
            setattr(certificado, attr, os.path.join(certificado.DIR_TLS, arq))
        try:
            from scripts import reclassificacao as R
            R.VENDAS_DB = Path(self.dados) / "relatorios" / "sem_vendas.db"
            R._banco = R.FilaBanco(Path(self.caminho("reclassificacao.db")))
        except Exception:  # noqa: BLE001 — módulo opcional para estes testes
            pass
        try:
            from scripts import agente_erp as ag
            ag.BANCO = self.caminho("agente.db")
            ag._banco = ag.BancoAgente(self.caminho("agente.db"))
        except Exception:  # noqa: BLE001
            pass
        # placas: listas e PDFs gerados vão para a pasta temporária
        from scripts import layouts_routes
        layouts_routes.UPLOAD_FOLDER = os.path.join(self.tmp, "uploads")
        layouts_routes.OUTPUT_FOLDER = os.path.join(self.tmp, "outputs")
        # módulos novos: o mesmo padrão
        try:
            from scripts import perdas
            perdas.DADOS_DIR = self.dados
            perdas.BANCO = self.caminho("perdas.db")
        except ImportError:
            pass

    def criar_usuario(self, usuario, senha="segredo1", papel="usuario", agente=False):
        from scripts import auth
        auth.criar_usuario(usuario, senha, nome=usuario.title(), papel=papel, criado_por="teste")
        if agente:
            auth.definir_acesso_agente(usuario, True, por="teste")

    def cliente(self, usuario=None):
        cli = self.app.test_client()
        if usuario:
            with cli.session_transaction() as s:
                s["usuario"] = usuario
                s["nome"] = usuario
        return cli


def montar():
    global _amb
    if _amb is None:
        _amb = Ambiente()
    return _amb
