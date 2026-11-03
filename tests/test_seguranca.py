"""Login, permissões, pedido de outro site e o preparo único do esquema."""
import os
import subprocess
import sys
import unittest

from tests.ambiente import montar, RAIZ


class TestSenhaEUsuarios(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.amb = montar()
        from scripts import auth
        cls.auth = auth
        cls.amb.criar_usuario("adm_seg", papel="admin")

    def test_senha_curta_recusada_ao_criar_e_trocar(self):
        ok, msg = self.auth.criar_usuario("curta", "12345")
        self.assertFalse(ok)
        self.assertIn("6", msg)
        self.amb.criar_usuario("troca_senha")
        ok, _ = self.auth.alterar_senha("troca_senha", "abc")
        self.assertFalse(ok)
        ok, _ = self.auth.alterar_senha("troca_senha", "abcdef")
        self.assertTrue(ok)
        self.assertTrue(self.auth.verificar("troca_senha", "abcdef"))

    def test_excluir_e_soft_delete_e_recriar(self):
        self.amb.criar_usuario("some")
        ok, _ = self.auth.excluir_usuario("some", por="adm_seg")
        self.assertTrue(ok)
        self.assertIsNone(self.auth.verificar("some", "segredo1"))
        self.assertFalse(self.auth.permissoes("some")["existe"])
        self.assertNotIn("some", [u["usuario"] for u in self.auth.listar_usuarios()])
        # a linha continua lá (auditoria aponta para ela)
        conn = self.auth._conn()
        try:
            r = conn.execute("SELECT excluido_em FROM usuarios WHERE usuario='some'").fetchone()
        finally:
            conn.close()
        self.assertIsNotNone(r["excluido_em"])
        # recriar com o mesmo nome reaproveita, sem herdar acesso
        ok, _ = self.auth.criar_usuario("some", "outrasenha")
        self.assertTrue(ok)
        self.assertTrue(self.auth.verificar("some", "outrasenha"))
        self.assertFalse(self.auth.permissoes("some")["agente"])

    def test_nao_exclui_ultimo_admin(self):
        conn = self.auth._conn()
        try:
            admins = [r[0] for r in conn.execute(
                "SELECT usuario FROM usuarios WHERE papel='admin' AND excluido_em IS NULL")]
        finally:
            conn.close()
        if len(admins) == 1:
            ok, msg = self.auth.excluir_usuario(admins[0], por="x")
            self.assertFalse(ok)


class TestLimiteDeTentativas(unittest.TestCase):
    def setUp(self):
        montar()
        from scripts import auth
        self.auth = auth
        auth._falhas.clear()
        auth._bloqueio.clear()

    def test_bloqueia_conta_depois_de_5_erros_e_libera_depois(self):
        a = self.auth
        for i in range(a.FALHAS_USUARIO - 1):
            a.registrar_falha("alvo", f"10.0.0.{i}", agora=100 + i)
        self.assertEqual(a.bloqueio_restante("alvo", "10.0.0.99", agora=110), 0)
        a.registrar_falha("alvo", "10.0.0.50", agora=111)
        self.assertGreater(a.bloqueio_restante("alvo", "10.0.0.99", agora=112), 0)
        self.assertEqual(a.bloqueio_restante("alvo", "10.0.0.99",
                                             agora=111 + a.BLOQUEIO_S + 1), 0)

    def test_ip_varrendo_contas(self):
        a = self.auth
        for i in range(a.FALHAS_IP):
            a.registrar_falha(f"conta{i}", "10.9.9.9", agora=200 + i)
        self.assertGreater(a.bloqueio_restante("qualquer", "10.9.9.9", agora=230), 0)
        self.assertEqual(a.bloqueio_restante("qualquer", "10.9.9.8", agora=230), 0)

    def test_login_pela_rota_devolve_429(self):
        amb = montar()
        amb.criar_usuario("travado")
        cli = amb.cliente()
        for _ in range(self.auth.FALHAS_USUARIO):
            cli.post("/login", data={"usuario": "travado", "senha": "errada"})
        r = cli.post("/login", data={"usuario": "travado", "senha": "segredo1"})
        self.assertEqual(r.status_code, 429)
        self.auth._falhas.clear()
        self.auth._bloqueio.clear()
        r = cli.post("/login", data={"usuario": "travado", "senha": "segredo1"})
        self.assertEqual(r.status_code, 302)


class TestGuarda(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.amb = montar()
        cls.amb.criar_usuario("chefe", papel="admin")
        cls.amb.criar_usuario("comum")

    def test_redefinir_senha_so_admin(self):
        r = self.amb.cliente("comum").post("/sistema/usuarios/api/redefinir-senha",
                                           json={"usuario": "chefe", "senha": "hackeado"})
        self.assertEqual(r.status_code, 403)
        r = self.amb.cliente("chefe").post("/sistema/usuarios/api/redefinir-senha",
                                           json={"usuario": "comum", "senha": "nova123"})
        self.assertTrue(r.get_json()["ok"])
        from scripts import auth
        self.assertTrue(auth.verificar("comum", "nova123"))
        auth.alterar_senha("comum", "segredo1")

    def test_restaurar_backup_so_admin(self):
        r = self.amb.cliente("comum").post("/sistema/backup/api/restaurar",
                                           json={"banco": "debitos.db", "arquivo": "x.db"})
        self.assertEqual(r.status_code, 403)

    def test_sessao_de_usuario_excluido_cai(self):
        self.amb.criar_usuario("demitido")
        cli = self.amb.cliente("demitido")
        self.assertEqual(cli.get("/").status_code, 200)
        from scripts import auth
        auth.excluir_usuario("demitido", por="chefe")
        r = cli.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r.headers["Location"])
        self.assertEqual(cli.get("/catalogo/api/status").status_code, 401)

    def test_post_de_outro_site_recusado(self):
        cli = self.amb.cliente("chefe")
        url = "/sistema/usuarios/api/senha"
        r = cli.post(url, json={"senha": "abcdefg"},
                     headers={"Origin": "http://site-malicioso.com"})
        self.assertEqual(r.status_code, 403)
        r = cli.post(url, json={"senha": "abcdefg"}, headers={"Origin": "null"})
        self.assertEqual(r.status_code, 403)
        r = cli.post(url, json={"senha": "segredo1"}, headers={"Origin": "http://localhost"})
        self.assertEqual(r.status_code, 200)
        # sem Origin nem Referer (programa / agente): passa para a guarda normal
        r = cli.post(url, json={"senha": "segredo1"})
        self.assertEqual(r.status_code, 200)

    def test_get_de_outro_site_nao_e_barrado(self):
        r = self.amb.cliente("chefe").get("/", headers={"Origin": "http://outro"})
        self.assertEqual(r.status_code, 200)

    def test_certificado_publico_sem_login(self):
        self.assertEqual(self.amb.cliente().get("/certificado").status_code, 200)

    def test_upload_grande_413(self):
        app = self.amb.app
        antes = app.config["MAX_CONTENT_LENGTH"]
        app.config["MAX_CONTENT_LENGTH"] = 10
        try:
            r = self.amb.cliente("chefe").post("/perdas/api/perda", data="x" * 100,
                                               content_type="application/json")
            self.assertEqual(r.status_code, 413)
        finally:
            app.config["MAX_CONTENT_LENGTH"] = antes


class TestLoginFechadoSeAuthQuebrar(unittest.TestCase):
    def test_telas_503_e_api_do_agente_continua(self):
        codigo = (
            "import sys; sys.path.insert(0, %r)\n"
            "sys.modules['scripts.auth_routes'] = None\n"
            "from tests.ambiente import montar\n"
            "a = montar(); c = a.app.test_client()\n"
            "print(c.get('/').status_code, c.get('/debitos/').status_code,"
            " c.get('/reclassificacao/api/ping').status_code)\n" % RAIZ)
        r = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True,
                           timeout=120, cwd=RAIZ, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(r.stdout.strip().splitlines()[-1], "503 503 200", r.stderr[-2000:])


class TestPreparoUnico(unittest.TestCase):
    def test_init_roda_uma_vez_e_aceita_banco_aninhado(self):
        import sqlite3
        import tempfile
        from scripts import db_util
        pasta = tempfile.mkdtemp()
        a, b = os.path.join(pasta, "a.db"), os.path.join(pasta, "b.db")
        chamadas = []

        def init_b(conn):
            chamadas.append("b")

        def init_a(conn):
            chamadas.append("a")
            # o init de um banco abre OUTRO (como vencidos -> fornecedores)
            db_util.preparar(sqlite3.connect(b), b, init_b)
            # e reabre o próprio sem recursão infinita
            db_util.preparar(conn, a, init_a)

        db_util.preparar(sqlite3.connect(a), a, init_a)
        db_util.preparar(sqlite3.connect(a), a, init_a)
        self.assertEqual(chamadas, ["a", "b"])
        db_util.esquecer(a)
        db_util.preparar(sqlite3.connect(a), a, init_a)
        self.assertEqual(chamadas, ["a", "b", "a"])


if __name__ == "__main__":
    unittest.main()
