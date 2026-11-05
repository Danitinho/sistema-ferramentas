"""Criar a nota de vencido / de perda pelo agente (nf.criar_nota).

Dois lados: o servidor (regra do mês, pedido, conferência antes de incluir) e
o roteiro do agente (agente/geral.py) com um driver de mentira no lugar do
RADGe — procurar antes, criar, simular, pendência no Salvar.
"""
import unittest
from datetime import date

from tests.ambiente import montar

MAQ = "PC-CRIAR-NOTA"


def _br(d: date) -> str:
    return d.strftime("%d/%m/%Y")


class TestRegraDoMes(unittest.TestCase):
    def setUp(self):
        montar()
        from scripts import vencidos_notas as vn
        self.vn = vn

    def test_mes_corrente_e_anterior(self):
        self.assertEqual(self.vn.meses_aceitos(date(2026, 11, 5)), ["2026-10", "2026-11"])
        self.assertEqual(self.vn.meses_aceitos(date(2027, 1, 2)), ["2026-12", "2027-01"])
        hoje = date(2026, 11, 5)
        self.assertTrue(self.vn.nota_no_prazo({"entrada": "28/10/2026"}, hoje))
        self.assertTrue(self.vn.nota_no_prazo({"entrada": "05/11/2026"}, hoje))
        self.assertFalse(self.vn.nota_no_prazo({"entrada": "30/09/2026"}, hoje))
        self.assertFalse(self.vn.nota_no_prazo({"entrada": ""}, hoje))

    def test_dcpoa_da_perda(self):
        self.assertEqual(self.vn.dcpoa_perda("2026-10"), "perda vencidos 10/2026")


class TestServidor(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.amb = montar()
        from scripts import agente_erp, fornecedores, vencidos, vencidos_notas
        cls.ag, cls.vn, cls.v = agente_erp, vencidos_notas, vencidos
        ok, _, f = fornecedores.criar("FORNECEDOR CRIAR NOTA")
        assert ok, _
        cls.fid = f["id"]
        fornecedores.definir_numero(cls.fid, "77", usuario="teste")
        ok, _ = vencidos.registrar_vencido("PRODUTO CRIAR NOTA", "7890000000555", 2,
                                           "FORNECEDOR CRIAR NOTA", "3,00", "teste",
                                           fornecedor_id=cls.fid)
        assert ok, _
        cls.mes = date.today().strftime("%Y-%m")
        agente_erp.banco().criar_maquina(MAQ)

    def _executar(self, pid, resultado, ok=True, erro=""):
        b = self.ag.banco()
        p = b.pegar(MAQ, ["nf.criar_nota", "nf.abrir_ultima", "nf.abrir_perda",
                          "nf.incluir_itens"])
        self.assertEqual(p["id"], pid)
        b.concluir(MAQ, pid, ok, resultado, erro)
        return p

    def _nota(self, entrada, lanc="900"):
        return {"numero": "1" + lanc, "lancamento": lanc, "entrada": entrada,
                "emissao": entrada, "valor": "", "aberta": True, "i": -1}

    def test_criar_exige_autorizacao(self):
        with self.assertRaises(ValueError):
            self.ag.banco().criar_pedido("nf.criar_nota", {"fornecedor": "77",
                                                           "meses": ["2026-11"]}, "x")

    def test_pedido_da_troca_e_da_perda(self):
        ok, msg, p = self.vn.criar_nota(self.fid, self.mes, "teste", maquina=MAQ, simular=True)
        self.assertTrue(ok, msg)
        prm = p["params"]
        self.assertEqual((prm["tipo"], prm["fornecedor"], prm["estoque"], prm["serie"],
                          prm["modelo"], prm["modelo_setas"]), ("3", "77", "1", "1", "1B", 9))
        self.assertEqual(prm["meses"], self.vn.meses_aceitos())
        self.assertTrue(prm["simular"])
        self.assertNotIn("dcpoa", prm)
        self.assertEqual(p["autorizado_por"], "teste")
        self._executar(p["id"], {"encontrada": False, "simulado": True})

        ok, msg, p = self.vn.criar_nota_perda("2026-10", "teste", maquina=MAQ)
        self.assertTrue(ok, msg)
        prm = p["params"]
        self.assertEqual((prm["fornecedor"], prm["dcpoa"], prm["busca_dcpoa"]),
                         ("3100", "perda vencidos 10/2026", "%perda%vencidos%"))
        self._executar(p["id"], {"encontrada": False, "simulado": True})

    def test_sem_numero_ou_mes_nao_pede(self):
        ok, msg, _ = self.vn.criar_nota(self.fid, "", "teste")
        self.assertFalse(ok)
        ok, msg, _ = self.vn.criar_nota_perda("2026-13", "teste")
        self.assertFalse(ok)

    def test_nota_velha_nao_recebe_itens_e_criada_recebe(self):
        b = self.ag.banco()
        # abriu a última nota, mas ela é de três meses atrás
        p = b.criar_pedido("nf.abrir_ultima", {"fornecedor": "77", "tipo": "3"}, "teste",
                           maquina_alvo=MAQ)
        self._executar(p["id"], {"encontrada": True, "nota": self._nota("10/07/2026")})
        estado = self.vn.aplicar_resultado(p["id"])
        self.assertFalse(estado["nota_no_prazo"])
        ok, msg, _ = self.vn.incluir(self.fid, self.mes, p["id"], "teste", maquina=MAQ)
        self.assertFalse(ok)
        self.assertIn("Crie uma nota nova", msg)

        # criada pelo agente: serve, e a inclusão sai para a nota nova
        ok, msg, pc = self.vn.criar_nota(self.fid, self.mes, "teste", maquina=MAQ)
        self.assertTrue(ok, msg)
        self._executar(pc["id"], {"encontrada": True, "criada": True,
                                  "nota": self._nota(_br(date.today()), "901")})
        self.assertTrue(self.vn.aplicar_resultado(pc["id"])["nota_no_prazo"])
        ok, msg, pi = self.vn.incluir(self.fid, self.mes, pc["id"], "teste", maquina=MAQ,
                                      simular=True)
        self.assertTrue(ok, msg)
        self.assertEqual(pi["params"]["nota"], "901")
        self._executar(pi["id"], {"itens": [{"situacao": "simulado"}]})
        self.vn.aplicar_resultado(pi["id"])        # devolve o vencido à rodada

    def test_criacao_da_troca_nao_vale_para_a_perda(self):
        ok, msg, pc = self.vn.criar_nota(self.fid, self.mes, "teste", maquina=MAQ)
        self._executar(pc["id"], {"encontrada": True, "criada": True,
                                  "nota": self._nota(_br(date.today()), "902")})
        ok, msg, _ = self.vn.incluir_perda(self.mes, pc["id"], "teste", maquina=MAQ)
        self.assertFalse(ok)
        self.assertIn("ainda não foi aberta", msg)

    def test_rotas_exigem_acesso_ao_agente(self):
        self.amb.criar_usuario("semagente_cn")
        cli = self.amb.cliente("semagente_cn")
        r = cli.post(f"/vencidos/api/rodada/{self.fid}/criar", json={"mes": self.mes})
        self.assertEqual(r.status_code, 403)
        r = cli.post("/vencidos/api/rodada-perda/criar", json={"mes": self.mes})
        self.assertEqual(r.status_code, 403)
        self.amb.criar_usuario("comagente_cn", agente=True)
        cli = self.amb.cliente("comagente_cn")
        r = cli.post("/vencidos/api/rodada-perda/criar",
                     json={"mes": self.mes, "maquina": MAQ, "simular": True}).get_json()
        self.assertTrue(r["ok"], r)
        self._executar(r["pedido"]["id"], {"encontrada": False, "simulado": True})
        rod = cli.get("/vencidos/api/rodada").get_json()
        self.assertEqual(rod["prazo_nota"]["meses"], self.vn.meses_aceitos())


# ── o roteiro do agente, com o RADGe de mentira ──────────────────────────────
class DriverFalso:
    """Só o que nf_abrir_ultima e nf_criar_nota usam. `notas` = as abertas
    que a busca acha; `recusar_salvar` = texto da caixa no Salvar."""

    def __init__(self, notas=(), recusar_salvar=""):
        from agente import entradas
        self.E = entradas
        self.notas = list(notas)
        self.recusar_salvar = recusar_salvar
        self.passos = []
        self.editando = False

    # busca (nf.abrir_ultima)
    def conferir_tela(self): self.passos.append("tela")
    def limpar(self, obrigatorio=True): self.passos.append("limpar")
    def filtrar(self, tipo, forn): self.passos.append(f"filtro {tipo}/{forn}"); return "FORN"
    def filtrar_texto(self, campo, texto): self.passos.append(f"filtro {campo}={texto}")
    def buscar(self): return ""
    def abrir_grade(self): pass
    def varrer(self): return [dict(n) for n in self.notas]
    def abrir_form(self): pass
    def ir_para(self, linha): pass

    def ler_campos(self, nomes):
        n = self.notas[-1] if self.notas else {}
        base = {"fornecedor": "", "emissao": n.get("emissao", ""), "entrada": n.get("entrada", ""),
                "lancamento": n.get("lancamento", ""), "numero": n.get("numero", "")}
        return {k: base.get(k) for k in nomes}

    # criação (nf.criar_nota)
    def incluir_nota(self): self.passos.append("incluir"); self.editando = True
    def tipo_nf(self, tipo): self.passos.append(f"tipo {tipo}"); return tipo

    def escrever_campo(self, nome, rotulo, texto, depois="{ENTER}"):
        self.passos.append(f"{nome}={texto}")
        return nome

    def fornecedor_nota(self, forn): self.passos.append(f"fornecedor {forn}"); return "NOME"
    def modelo_nota(self, setas, alvo): self.passos.append(f"modelo {setas}x"); return "1B - NF Avulsa"

    def salvar_nota(self):
        self.passos.append("salvar")
        if self.recusar_salvar:
            raise self.E.NotaRecusada(self.recusar_salvar)
        self.editando = False
        hoje = _br(date.today())
        self.notas.append({"numero": "555", "lancamento": "1234", "entrada": hoje,
                           "emissao": hoje, "aberta": True, "valor": "", "i": 0})
        return {"numero": "555", "lancamento": "1234", "fornecedor": "NOME", "entrada": hoje,
                "valor": "", "aberta": True}

    def cancelar_nota(self):
        self.passos.append("cancelar")
        self.editando = False
        return {"cancelado": True, "caixa": ""}


class AgenteFalso:
    def __init__(self, drv, autorizado="teste", forcar_simulacao=False):
        self.drv = drv
        self.pedido_atual = {"autorizado_por": autorizado}
        self.forcar_simulacao = forcar_simulacao
        self.linhas = []

    def driver(self): return self.drv
    def log(self, t): self.linhas.append(t)
    def _checar_interrupcao(self): pass


class TestRoteiroDoAgente(unittest.TestCase):
    def setUp(self):
        from agente import geral
        self.g = geral
        hoje = date.today()
        self.meses = [hoje.replace(day=1).strftime("%Y-%m")]
        self.prm = {"tipo": "3", "fornecedor": "77", "meses": self.meses, "estoque": "1",
                    "serie": "1", "modelo": "1B", "modelo_setas": 9}

    def test_ja_existe_nota_do_mes_nao_cria(self):
        hoje = _br(date.today())
        drv = DriverFalso([{"numero": "10", "lancamento": "20", "entrada": hoje,
                            "emissao": hoje, "aberta": True, "valor": "", "i": 0}])
        res = self.g.nf_criar_nota(AgenteFalso(drv), self.prm)
        self.assertTrue(res["encontrada"])
        self.assertFalse(res["criada"])
        self.assertNotIn("incluir", drv.passos)

    def test_nota_velha_cria_nova_na_ordem_certa(self):
        drv = DriverFalso([{"numero": "10", "lancamento": "20", "entrada": "10/01/2020",
                            "emissao": "10/01/2020", "aberta": True, "valor": "", "i": 0}])
        res = self.g.nf_criar_nota(AgenteFalso(drv), self.prm)
        self.assertTrue(res["criada"])
        self.assertEqual(res["nota"]["lancamento"], "1234")
        i = drv.passos.index("incluir")
        self.assertEqual(drv.passos[i - 1], "limpar")
        self.assertEqual(drv.passos[i:], ["incluir", "tipo 3", "estoque_origem=1",
                                          "fornecedor 77", "modelo 9x", "serie=1", "salvar"])

    def test_perda_leva_dcpoa_e_procura_pelo_dcpoa(self):
        drv = DriverFalso()
        prm = dict(self.prm, fornecedor="3100", dcpoa="perda vencidos 10/2026",
                   busca_dcpoa="%perda%vencidos%")
        res = self.g.nf_criar_nota(AgenteFalso(drv), prm)
        self.assertTrue(res["criada"])
        self.assertIn("filtro dcpoa=%perda%vencidos%", drv.passos)
        self.assertIn("dcpoa=perda vencidos 10/2026", drv.passos)
        self.assertLess(drv.passos.index("dcpoa=perda vencidos 10/2026"),
                        drv.passos.index("salvar"))

    def test_simular_cancela_no_lugar_do_salvar(self):
        drv = DriverFalso()
        res = self.g.nf_criar_nota(AgenteFalso(drv), dict(self.prm, simular=True))
        self.assertTrue(res["simulado"])
        self.assertFalse(res["criada"])
        self.assertNotIn("salvar", drv.passos)
        self.assertEqual(drv.passos[-1], "cancelar")
        # a trava da máquina vence o pedido
        drv = DriverFalso()
        res = self.g.nf_criar_nota(AgenteFalso(drv, forcar_simulacao=True), self.prm)
        self.assertTrue(res["simulado"])
        self.assertNotIn("salvar", drv.passos)

    def test_pendencia_no_salvar_cancela_e_avisa(self):
        drv = DriverFalso(recusar_salvar="Informe a série")
        with self.assertRaises(self.g.ErroPedido) as cm:
            self.g.nf_criar_nota(AgenteFalso(drv), self.prm)
        self.assertTrue(str(cm.exception).startswith(self.g.NADA_GRAVADO))
        self.assertIn("Informe a série", str(cm.exception))
        self.assertEqual(drv.passos[-1], "cancelar")

    def test_sem_autorizacao_nao_toca_no_erp(self):
        drv = DriverFalso()
        with self.assertRaises(self.g.ErroPedido):
            self.g.nf_criar_nota(AgenteFalso(drv, autorizado=""), self.prm)
        self.assertEqual(drv.passos, [])


if __name__ == "__main__":
    unittest.main()
