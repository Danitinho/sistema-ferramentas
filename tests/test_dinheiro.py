"""Débitos (alocação e quitação) e a calculadora de preço."""
import unittest

from tests.ambiente import montar


class TestDebitos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        montar()
        from scripts import debitos
        cls.d = debitos
        debitos.adicionar_empresa("11.111.111/0001-11", "EMPRESA TESTE")

    def _debito(self, nf, valor):
        ok, msg = self.d.adicionar_debito_vencimento(
            "11.111.111/0001-11", nf, valor, periodo_tipo="mes", periodo_inicio="2026-09")
        self.assertTrue(ok, msg)
        return next(x for x in self.d.listar_debitos("11.111.111/0001-11")
                    if x["nf_numero"] == nf)

    def test_muitos_pagamentos_pequenos_quitam_sem_centavo_fantasma(self):
        deb = self._debito("NF-CENT", "10,00")
        for i in range(100):     # 100 x 0,10: em float puro daria 9,99999...
            ok, msg = self.d.adicionar_pagamento("11.111.111/0001-11", "0,10", tipo="troca",
                                                 referencia=f"t{i}", debito_id=deb["id"])
            self.assertTrue(ok, msg)
        deb = next(x for x in self.d.listar_debitos("11.111.111/0001-11") if x["id"] == deb["id"])
        self.assertEqual(deb["status"], "quitado")
        self.assertEqual(deb["saldo"], 0)

    def test_excedente_vira_credito_e_alocacoes_batem(self):
        deb = self._debito("NF-EXC", "50,00")
        ok, _ = self.d.adicionar_pagamento("11.111.111/0001-11", "80,00", referencia="BON-1",
                                           debito_id=deb["id"])
        self.assertTrue(ok)
        creditos = self.d.listar_creditos("11.111.111/0001-11")
        self.assertTrue(any(abs(c["disponivel"] - 30.0) < 0.001 for c in creditos))
        # consistência: valor_pago == soma das alocações ativas, em todo débito
        conn = self.d._conn()
        try:
            ruins = conn.execute(
                "SELECT d.id FROM debitos d WHERE d.excluido_em IS NULL AND ABS(d.valor_pago -"
                " COALESCE((SELECT SUM(valor) FROM alocacoes a WHERE a.debito_id=d.id"
                " AND a.excluido_em IS NULL),0)) > 0.005").fetchall()
        finally:
            conn.close()
        self.assertEqual(ruins, [])

    def test_excluir_pagamento_reverte_alocacao(self):
        deb = self._debito("NF-REV", "20,00")
        self.d.adicionar_pagamento("11.111.111/0001-11", "20,00", referencia="BON-REV",
                                   debito_id=deb["id"])
        pag = next(p for p in self.d.listar_pagamentos("11.111.111/0001-11")
                   if p["referencia"] == "BON-REV")
        ok, _ = self.d.excluir_pagamento(pag["id"])
        self.assertTrue(ok)
        deb = next(x for x in self.d.listar_debitos("11.111.111/0001-11") if x["id"] == deb["id"])
        self.assertEqual(deb["status"], "aberto")


class TestCalculadora(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.amb = montar()
        cls.amb.criar_usuario("calc")
        cls.cli = cls.amb.cliente("calc")

    def calc(self, **k):
        r = self.cli.post("/cadastro/calculadora/calcular", json=k)
        return r.status_code, r.get_json()

    def test_markup_padrao_igual_ao_de_sempre(self):
        st, d = self.calc(custo=10, margem=30, origem="sem")
        self.assertEqual(st, 200)
        self.assertEqual(d["preco"], 13.0)
        self.assertEqual(d["markup_pct"], 30.0)
        self.assertAlmostEqual(d["margem_venda_pct"], 23.08, places=2)

    def test_margem_sobre_a_venda(self):
        st, d = self.calc(custo=10, margem=30, origem="sem", tipo_margem="venda")
        self.assertEqual(d["preco"], 14.29)
        self.assertAlmostEqual(d["margem_venda_pct"], 30.0, places=1)
        st, d = self.calc(custo=10, margem=100, origem="sem", tipo_margem="venda")
        self.assertEqual(st, 400)

    def test_arredondamento_sempre_para_cima(self):
        from app import arredondar_preco
        self.assertEqual(arredondar_preco(13.00, "99"), 13.99)
        self.assertEqual(arredondar_preco(13.99, "99"), 13.99)
        self.assertEqual(arredondar_preco(14.00, "99"), 14.99)
        self.assertEqual(arredondar_preco(13.20, "49_99"), 13.49)
        self.assertEqual(arredondar_preco(13.50, "49_99"), 13.99)
        self.assertEqual(arredondar_preco(13.995, "90"), 14.90)
        self.assertEqual(arredondar_preco(13.37, ""), 13.37)
        st, d = self.calc(custo=10, margem=30, origem="sem", arredondamento="99")
        self.assertEqual(d["preco"], 13.99)
        self.assertEqual(d["valor_arredondamento"], 0.99)

    def test_custo_zero_400_e_nao_500(self):
        st, _ = self.calc(custo=0, margem=30)
        self.assertEqual(st, 400)


if __name__ == "__main__":
    unittest.main()
