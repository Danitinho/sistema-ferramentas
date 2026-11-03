"""Rebaixa de vencidos e perdas da loja."""
import io
import unittest
from datetime import date, timedelta

from tests.ambiente import montar
from tests import dados_teste as dt


def _dia(n):
    return (date.today() + timedelta(days=n)).isoformat()


class TestRegraDaRebaixa(unittest.TestCase):
    def setUp(self):
        montar()
        from scripts import vencidos_rebaixa as rb
        self.rb = rb

    def test_regra(self):
        s = self.rb.sugerir_preco(10.00, 5.00, "alto", 20)
        self.assertEqual((s["desconto_pct"], s["preco"]), (30, 6.99))
        s = self.rb.sugerir_preco(10.00, 5.00, "medio", 20)
        self.assertEqual(s["preco"], 7.99)        # 8,50 -> ,99 para baixo
        s = self.rb.sugerir_preco(10.00, 5.00, "alto", 5)
        self.assertEqual(s["desconto_pct"], 40)   # +10 faltando <= 7 dias
        self.assertEqual(s["preco"], 5.99)
        self.assertIsNone(self.rb.sugerir_preco(10.00, 5.00, "baixo", 5))
        self.assertIsNone(self.rb.sugerir_preco(None, 5.00, "alto", 5))

    def test_piso_no_custo(self):
        s = self.rb.sugerir_preco(10.00, 8.50, "alto", 3)
        self.assertTrue(s["travado_no_custo"])
        self.assertEqual(s["preco"], 8.50)
        self.assertIsNone(self.rb.sugerir_preco(10.00, 10.00, "alto", 3))


class TestFluxoRebaixa(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.amb = montar()
        dt.base_padrao()
        cls.amb.criar_usuario("rebaixa")
        from scripts import vencidos as v, vencidos_rebaixa as rb
        cls.v, cls.rb = v, rb
        # 100 iogurtes vencendo em 10 dias, vendendo ~1/dia: sobram ~90 (risco alto)
        ok, _ = v.registrar_aviso("IOGURTE TESTE 170G", "7890000000017", 100, "LATICINIO X",
                                  "frios", _dia(10), custo="2,00", venda="4,49")
        assert ok
        conn = v._conn()
        try:
            cls.aviso_id = conn.execute("SELECT id FROM avisos WHERE codigo_barras=?"
                                        " ORDER BY criado_em DESC", ("7890000000017",)
                                        ).fetchone()[0]
        finally:
            conn.close()

    def test_sugere_registra_lanca_e_cancela(self):
        sug = [s for s in self.rb.sugestoes() if s["aviso_id"] == self.aviso_id]
        self.assertEqual(len(sug), 1)
        s = sug[0]
        self.assertEqual(s["risco"]["nivel"], "alto")
        self.assertEqual(s["sugestao"]["preco"], 2.99)        # 4,49 x 0,70 = 3,14 -> 2,99
        self.assertEqual(s["fim_sugerido"], _dia(9))

        cli = self.amb.cliente("rebaixa")
        # validações
        r = cli.post("/vencidos/api/rebaixa", json={"aviso_id": self.aviso_id,
                                                    "preco_por": "4.49"}).get_json()
        self.assertFalse(r["ok"])                              # não é rebaixa
        r = cli.post("/vencidos/api/rebaixa", json={"aviso_id": self.aviso_id, "preco_por": "2.99",
                                                    "fim": _dia(10)}).get_json()
        self.assertFalse(r["ok"])                              # termina no vencimento
        r = cli.post("/vencidos/api/rebaixa", json={"aviso_id": self.aviso_id,
                                                    "preco_por": "2.99"}).get_json()
        self.assertTrue(r["ok"], r)
        rid = r["id"]
        # segunda rebaixa no mesmo aviso não
        r2 = cli.post("/vencidos/api/rebaixa", json={"aviso_id": self.aviso_id,
                                                     "preco_por": "2.49"}).get_json()
        self.assertFalse(r2["ok"])
        # sai das sugestões e o preço vai para o aviso
        self.assertFalse([s for s in self.rb.sugestoes() if s["aviso_id"] == self.aviso_id])
        aviso = self.v.buscar_aviso_ativo_por_barras("7890000000017")
        self.assertEqual(aviso["valor_promocional"], 2.99)

        reb = next(x for x in self.rb.listar() if x["id"] == rid)
        self.assertEqual(reb["status_efetivo"], "a_lancar")
        self.assertEqual(reb["desconto_pct"], 33.4)

        self.assertTrue(cli.post(f"/vencidos/api/rebaixa/{rid}/lancada",
                                 json={"referencia": "PROMO 12"}).get_json()["ok"])
        reb = next(x for x in self.rb.listar() if x["id"] == rid)
        self.assertEqual((reb["status_efetivo"], reb["lancada_ref"]), ("lancada", "PROMO 12"))

        r = cli.post(f"/vencidos/api/rebaixa/{rid}/cancelar", json={"motivo": "teste"}).get_json()
        self.assertTrue(r["ok"])
        self.assertIn("ERP", r["msg"])                         # lembra de voltar o preço lá
        aviso = self.v.buscar_aviso_ativo_por_barras("7890000000017")
        self.assertIsNone(aviso["valor_promocional"])
        # cancelada, volta a ser sugestão
        self.assertTrue([s for s in self.rb.sugestoes() if s["aviso_id"] == self.aviso_id])

    def test_pagina(self):
        r = self.amb.cliente("rebaixa").get("/vencidos/rebaixas")
        self.assertEqual(r.status_code, 200)
        self.assertIn("IOGURTE TESTE".encode(), r.data)


class TestPerdas(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.amb = montar()
        dt.base_padrao()
        cls.amb.criar_usuario("perdas")
        from scripts import perdas
        cls.p = perdas
        cls.mes = dt.mes_passado()
        cls.dia = cls.mes + "-10"

    def test_registro_puxa_do_catalogo(self):
        ok, msg, pid = self.p.registrar("", "3", "avaria", data=self.dia,
                                        codigo_barras="7890000000024", usuario="perdas")
        self.assertTrue(ok, msg)
        x = next(x for x in self.p.listar(mes=self.mes) if x["id"] == pid)
        self.assertEqual(x["produto"], "QUEIJO TESTE KG")
        self.assertEqual((x["custo"], x["venda"], x["codigo_interno"], x["dep"]),
                         (20.0, 39.9, "000102", "8"))
        self.assertEqual(x["valor"], 60.0)

    def test_validacoes(self):
        self.assertFalse(self.p.registrar("X", "1", "inventado")[0])
        self.assertFalse(self.p.registrar("X", "0", "avaria")[0])
        self.assertFalse(self.p.registrar("X", "1", "avaria", data=_dia(1))[0])
        self.assertFalse(self.p.registrar("", "1", "avaria", codigo_barras="999")[0])
        ok, msg, _ = self.p.registrar("SEM CUSTO", "1,5", "consumo", data=self.dia)
        self.assertTrue(ok)
        self.assertIn("Sem custo", msg)

    def test_painel_pct_da_venda_e_vencidos(self):
        mes = "2026-01" if self.mes != "2026-01" else "2025-12"
        dt.vendas(mes, [("000101", "7890000000017", "IOGURTE", 10, 1000.00),
                        ("000102", "7890000000024", "QUEIJO", 10, 3000.00)], total_geral=10000.0)
        self.p.registrar("", "10", "furto", data=mes + "-05", codigo_barras="7890000000017")
        self.p.registrar("", "1", "degustacao", data=mes + "-06", codigo_barras="7890000000024")
        pn = self.p.painel(mes)
        self.assertEqual(pn["total"], 40.0)                   # 10 x 2,00 + 1 x 20,00
        self.assertEqual(pn["pct_venda"], 0.4)                # 40 / 10.000
        dep6 = next(d for d in pn["por_dep"] if d["dep"] == "6")
        self.assertEqual((dep6["valor"], dep6["pct_venda"]), (20.0, 2.0))
        motivos = {m["motivo"]: m["valor"] for m in pn["por_motivo"]}
        self.assertEqual(motivos, {"furto": 20.0, "degustacao": 20.0})

    def test_vencido_baixado_como_perda_entra_no_painel(self):
        from scripts import vencidos as v
        ok, _ = v.registrar_vencido("QUEIJO TESTE KG", "7890000000024", 2, "X", "20,00", "fulano")
        self.assertTrue(ok)
        conn = v._conn()
        try:
            vid = conn.execute("SELECT id FROM vencidos ORDER BY criado_em DESC").fetchone()[0]
        finally:
            conn.close()
        self.assertTrue(v.dar_baixa(vid, "perda", "NP 1")[0])
        pn = self.p.painel(date.today().strftime("%Y-%m"))
        venc = next(m for m in pn["por_motivo"] if m["motivo"] == "vencido")
        self.assertGreaterEqual(venc["valor"], 40.0)

    def test_lancamento_em_lote_e_trava(self):
        cli = self.amb.cliente("perdas")
        ids = [self.p.registrar("ITEM LOTE", "1", "doacao", data=self.dia, custo="1")[2]
               for _ in range(3)]
        r = cli.post("/perdas/api/lancadas", json={"ids": ids}).get_json()
        self.assertFalse(r["ok"])                               # sem referência
        r = cli.post("/perdas/api/lancadas", json={"ids": ids, "referencia": "NP 77"}).get_json()
        self.assertEqual(r["n"], 3)
        r = cli.delete(f"/perdas/api/perda/{ids[0]}").get_json()
        self.assertFalse(r["ok"])                               # lançada não se exclui
        r = cli.post("/perdas/api/lancadas", json={"ids": [ids[0]], "desfazer": True}).get_json()
        self.assertEqual(r["n"], 1)
        self.assertTrue(cli.delete(f"/perdas/api/perda/{ids[0]}").get_json()["ok"])

    def test_pagina_e_excel(self):
        cli = self.amb.cliente("perdas")
        self.assertEqual(cli.get(f"/perdas/?mes={self.mes}").status_code, 200)
        r = cli.get(f"/perdas/excel?mes={self.mes}&lanc=pendente")
        self.assertEqual(r.status_code, 200)
        from openpyxl import load_workbook
        ws = load_workbook(io.BytesIO(r.data)).active
        self.assertEqual(ws["A2"].value, "Data")
        self.assertTrue(str(ws.cell(ws.max_row, 1).value).startswith("TOTAL"))
        r = cli.get("/perdas/api/produto?cb=7890000000017").get_json()
        self.assertEqual(r["descricao"], "IOGURTE TESTE 170G")


class TestPaginasCarregam(unittest.TestCase):
    def test_todas(self):
        amb = montar()
        amb.criar_usuario("visita", papel="admin")
        cli = amb.cliente("visita")
        for url in ["/", "/cadastro/calculadora", "/sistema/usuarios", "/sistema/backup/",
                    "/layouts/", "/layouts/super_oferta_novo/gerar", "/vencidos/",
                    "/vencidos/rebaixas", "/perdas/", "/debitos/", "/catalogo/",
                    "/relatorios/", "/fornecedores/", "/certificado", "/agente/",
                    "/reclassificacao/", "/reclassificacao/curadoria"]:
            r = cli.get(url)
            self.assertEqual(r.status_code, 200, url)
        d = cli.get("/agente/painel/api/estado").get_json()
        self.assertTrue(d["versao_atual"])


if __name__ == "__main__":
    unittest.main()
