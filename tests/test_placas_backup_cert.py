"""Placas de oferta, backup e certificado HTTPS."""
import codecs
import io
import os
import sqlite3
import tempfile
import threading
import unittest

from tests.ambiente import montar


def _ler(caminho):
    with open(caminho, "rb") as f:
        return f.read()


class TestPlacas(unittest.TestCase):
    LAYOUT = "super_oferta_antigo"     # produto : preço : unidade

    @classmethod
    def setUpClass(cls):
        cls.amb = montar()
        cls.amb.criar_usuario("placas")

    def test_ler_texto_qualquer_codificacao(self):
        from scripts.gerador_layouts import ler_texto
        pasta = tempfile.mkdtemp()
        txt = "AÇÚCAR:4,99:KG\nCAFÉ:12,90:UN\n"
        for nome, dados in (("u16", codecs.BOM_UTF16_LE + txt.encode("utf-16-le")),
                            ("u8bom", codecs.BOM_UTF8 + txt.encode("utf-8")),
                            ("u8", txt.encode("utf-8")),
                            ("ansi", txt.encode("cp1252"))):
            p = os.path.join(pasta, nome)
            with open(p, "wb") as f:
                f.write(dados)
            self.assertEqual(ler_texto(p).replace("\r\n", "\n"), txt, nome)

    def _gerar(self, cli, **form):
        r = cli.post(f"/layouts/{self.LAYOUT}/gerar", data={"pdf_layout": "4", **form},
                     content_type="multipart/form-data")
        return r.status_code, r.get_json()

    def test_txt_utf16_do_erp_gera(self):
        cli = self.amb.cliente("placas")
        dados = codecs.BOM_UTF16_LE + "LEITE INTEGRAL:4,99:UN\n".encode("utf-16-le")
        st, d = self._gerar(cli, produtos=(io.BytesIO(dados), "lista.txt"))
        self.assertEqual(st, 200, d)
        self.assertEqual(d["geradas"], 1)

    def test_duas_geracoes_simultaneas_nao_se_misturam(self):
        resultados = {}

        def gerar(nome, n):
            cli = self.amb.cliente("placas")
            st, d = self._gerar(cli, **{"produto[]": [f"{nome} {i}" for i in range(n)],
                                        "preco[]": ["1,99"] * n, "unidade[]": ["UN"] * n})
            resultados[nome] = (st, d)

        t1 = threading.Thread(target=gerar, args=("ARROZ", 3))
        t2 = threading.Thread(target=gerar, args=("FEIJAO", 5))
        t1.start(); t2.start(); t1.join(); t2.join()
        (s1, a), (s2, b) = resultados["ARROZ"], resultados["FEIJAO"]
        self.assertEqual((s1, s2), (200, 200), (a, b))
        self.assertEqual((a["geradas"], b["geradas"]), (3, 5))
        self.assertNotEqual(a["pdf_url"], b["pdf_url"])
        cli = self.amb.cliente("placas")
        for d in (a, b):
            r = cli.get(d["pdf_url"])
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.data.startswith(b"%PDF"))
            r.close()
        # imagens e lista intermediárias não ficam para trás
        from scripts import layouts_routes
        sobras = os.listdir(os.path.join(layouts_routes.OUTPUT_FOLDER, "ofertas_geradas"))
        self.assertEqual(sobras, [])

    def test_dois_pontos_no_nome_nao_desloca_campos(self):
        st, d = self._gerar(self.amb.cliente("placas"),
                            **{"produto[]": ["SUCO 1:1 LARANJA"], "preco[]": ["5,49"],
                               "unidade[]": ["UN"]})
        self.assertEqual(st, 200, d)
        self.assertEqual((d["geradas"], d["falhas"]), (1, 0))

    def test_pdf_so_pelo_nome_gerado(self):
        cli = self.amb.cliente("placas")
        self.assertEqual(cli.get("/layouts/pdf/..%5Capp.py").status_code, 404)
        self.assertEqual(cli.get("/layouts/pdf/qualquer.pdf").status_code, 404)


class TestBackup(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.amb = montar()
        from scripts import backup, debitos, auth
        cls.b = backup
        debitos.listar_empresas()      # garante debitos.db
        auth.obter_ou_criar_secret()

    def test_backup_verifica_copia_e_guarda_config(self):
        r = self.b.fazer_backup()
        self.assertEqual(r["falhas"], 0, r)
        self.assertGreaterEqual(r["config"], 1)        # ao menos o secret.key
        s = self.b.status()
        self.assertFalse(s["atrasado"], s)
        self.assertIsNotNone(s["ultima_copia"])

    def test_copia_com_defeito_e_detectada(self):
        pasta = tempfile.mkdtemp()
        p = os.path.join(pasta, "ruim.db")
        with open(p, "wb") as f:
            f.write(b"SQLite format 3\x00" + os.urandom(4096))
        try:
            defeito = self.b._verificar(p)
        except sqlite3.DatabaseError as e:     # também derruba o item no fazer_backup
            defeito = str(e)
        self.assertTrue(defeito)

    def test_atraso_quando_ultima_copia_e_velha(self):
        from datetime import datetime, timedelta
        velha = datetime.now() - timedelta(hours=self.b.INTERVALO_HORAS + 5)
        s = self.b.saude(ultima_dt=velha)
        self.assertTrue(s["atrasado"])
        self.assertIn("h", s["motivo_atraso"])

    def test_restaurar_reprepara_o_esquema(self):
        from scripts import debitos, db_util
        self.b.fazer_backup()
        g = next(g for g in self.b.listar_backups() if g["banco"] == "debitos.db")
        ok, msg = self.b.restaurar("debitos.db", g["copias"][0]["arquivo"])
        self.assertTrue(ok, msg)
        self.assertNotIn(db_util._chave(debitos.BANCO), db_util._prontos)
        debitos.listar_empresas()     # reabre e prepara de novo sem erro


class TestCertificado(unittest.TestCase):
    def test_ca_estavel_e_servidor_refeito_quando_ip_muda(self):
        montar()
        from scripts import certificado as c
        from cryptography import x509
        r1 = c.garantir(ips=["192.168.0.52"], nomes=["loja-pc", "localhost"])
        self.assertTrue(os.path.isfile(c.CA_KEY))
        ca1 = _ler(c.CA_CRT)
        r2 = c.garantir(ips=["192.168.0.52"], nomes=["loja-pc", "localhost"])
        self.assertIsNone(r2["refeito"])
        r3 = c.garantir(ips=["192.168.0.60"], nomes=["loja-pc", "localhost"])
        self.assertEqual(r3["refeito"], "IP ou nome da máquina mudou")
        self.assertEqual(_ler(c.CA_CRT), ca1)     # aparelhos não reinstalam
        srv = x509.load_pem_x509_certificate(_ler(c.SRV_CRT))
        san = srv.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        self.assertIn("192.168.0.60", [str(i) for i in san.get_values_for_type(x509.IPAddress)])
        # o certificado do servidor é assinado pela CA
        ca = x509.load_pem_x509_certificate(ca1)
        srv.verify_directly_issued_by(ca)
        info = c.info()
        self.assertTrue(info["existe"])
        self.assertEqual(len(info["ca_impressao"].split(":")), 32)
        self.assertNotIn("PRIVATE", str(info))

    def test_download_nao_entrega_a_chave(self):
        amb = montar()
        from scripts import certificado as c
        c.garantir(ips=["192.168.0.52"], nomes=["localhost"])
        r = amb.cliente().get("/certificado/ca.crt")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"BEGIN CERTIFICATE", r.data)
        self.assertNotIn(b"PRIVATE KEY", r.data)
        r.close()


class TestFilaReclassificacao(unittest.TestCase):
    def test_reserva_entrega_blocos_disjuntos_e_so_pronto(self):
        montar()
        from scripts import reclassificacao as R
        b = R.banco()
        itens = [R.Item(i, f"78900{i:06d}", f"PRODUTO {i}", True, "2026", "1", "1", "1",
                        "6", "51", "208", "ALTA", "teste") for i in range(60)]
        b.semear(itens)
        b.garantir_operador("op_a")
        b.garantir_operador("op_b")
        # nada nasce pronto: sem curadoria, a fila não entrega nada
        self.assertEqual(b.reservar("op_a", 10, True, [])["itens"], [])
        cods = [i.cod for i in itens]
        r = b.aceitar_sugestao(cods, por="curador")
        self.assertEqual(len(r.get("recusados", [])), 0, r)
        entregues = {}

        def pegar(op):
            entregues[op] = [x["cod"] for x in b.reservar(op, 25, True, [])["itens"]]

        ts = [threading.Thread(target=pegar, args=(op,)) for op in ("op_a", "op_b")]
        [t.start() for t in ts]
        [t.join() for t in ts]
        a, bb = set(entregues["op_a"]), set(entregues["op_b"])
        self.assertEqual(len(a), 25)
        self.assertEqual(len(bb), 25)
        self.assertFalse(a & bb)
        # só o dono fecha
        cod = next(iter(a))
        self.assertFalse(b.concluir("op_b", cod, "alterado")["ok"])
        self.assertTrue(b.concluir("op_a", cod, "alterado", "6", "51", "208")["ok"])


if __name__ == "__main__":
    unittest.main()
