# -*- coding: utf-8 -*-
"""
agente/entradas.py
Driver da tela ENTRADAS (notas fiscais de entrada) do RADGe. Operações
primitivas; quem decide o que fazer é o agente geral (`agente/geral.py`).

O mapa vem do painel (/agente), em `mapa_erp.telas.nota_fiscal`:
  titulo_contem   texto que o título da janela precisa ter ("Entradas") — o
                  RADGe mostra a tela ativa entre colchetes no título
  controles       numero, lancamento, tipo, fornecedor_cod, fornecedor (rótulo
                  com o nome), emissao, entrada, valor, limpar, grade, form,
                  grade_notas, situacao_aberto, fechar_nf...
  buscar          tecla da busca ("{F5}")
  conferir        campos que precisam estar vazios depois do Limpar
  espera_*_s      pausas calibradas na máquina real
  grade_dx/dy     ponto da PRIMEIRA linha dentro da grade (pixels do canto dela)

Regra que não pode afrouxar: **nada é digitado com uma nota carregada.** Os
campos do filtro (tipo, fornecedor) são os mesmos campos da nota — digitar
neles com o formulário cheio EDITA a nota aberta. Por isso o Limpar é
conferido antes, e formulário que não esvazia para tudo.

Varredura da grade: nunca se aperta ↓ na última linha. No cxGrid isso cria um
registro novo (modo de inclusão) — já aconteceu, com "linha sem número, data de
hoje". A varredura primeiro vai ao fim (Ctrl+End), guarda qual é a última nota
e desce só até ela.
"""
import time
from datetime import datetime

from agente import erp as erp_base

NEGATIVOS = ["Não", "&Não", "Nao", "&Nao", "No", "&No"]
FECHAR = ["OK", "&OK", "Ok", "Fechar", "&Fechar"]
BM_GETCHECK = 0x00F0
WS_TABSTOP = 0x00010000
MAX_LINHAS = 500


class ErroTela(erp_base.ErroERP):
    """O ERP está num estado em que continuar poderia mexer em dado."""


def _data(s):
    try:
        return datetime.strptime((s or "").strip()[:10], "%d/%m/%Y")
    except ValueError:
        return datetime.min


def _int(s):
    d = "".join(ch for ch in str(s or "") if ch.isdigit())
    return int(d) if d else 0


class DriverEntradas:
    def __init__(self, mapa, log):
        self.raiz = mapa
        self.m = (mapa.get("telas") or {}).get("nota_fiscal") or {}
        if not self.m.get("controles"):
            raise erp_base.ErroERP("o mapa do ERP não tem a tela 'nota_fiscal' "
                                   "(telas.nota_fiscal.controles) — confira no painel /agente")
        self.c = self.m["controles"]
        self.log = log
        self.checar = lambda: None          # o agente troca: ESC segurado interrompe
        # Só dentro da tela ativa: com Produtos aberta ao lado, "Limpar" existe
        # nas duas, e o clique não pode cair na do agente da reclassificação.
        self.j = erp_base.Janela(mapa.get("janela_titulo_regex", "RADGe"),
                                 mapa.get("classes_dialogo"),
                                 formulario=self.m.get("formulario") or erp_base.MDI_ATIVO)

    # ── apoio ────────────────────────────────────────────────────────────────
    def _espera(self, chave, padrao):
        time.sleep(float(self.m.get(chave, padrao)))

    def _spec(self, nome):
        s = self.c.get(nome)
        if not s:
            raise erp_base.ErroERP(f"o mapa do ERP não diz onde fica '{nome}' na tela Entradas")
        return s

    def ler(self, nome):
        return self.j.ler(nome, self._spec(nome))

    def ler_campos(self, nomes):
        saida = {}
        for n in nomes:
            try:
                saida[n] = self.ler(n)
            except erp_base.ErroERP:
                saida[n] = None
        return saida

    def _fechar_dialogos(self, preferidos):
        textos = []
        for _ in range(5):
            dlg = self.j.dialogos()
            if not dlg:
                break
            for d in dlg:
                textos.append(d["texto"])
                if not self.j.responder(d, preferidos + FECHAR):
                    raise ErroTela(f"caixa do ERP sem botão que eu possa apertar: "
                                   f"'{d['texto']}' ({d['botoes']})")
            time.sleep(0.4)
        return textos

    # ── conexão e tela ───────────────────────────────────────────────────────
    def conectar(self):
        return self.j.conectar()

    def viva(self):
        return self.j.viva()

    def conferir_tela(self):
        alvo = self.m.get("titulo_contem", "Entradas")
        titulo = self.j.titulo()
        if erp_base.normalizar(alvo) not in erp_base.normalizar(titulo):
            raise ErroTela(f"o ERP está em '{titulo}'. Abra a tela {alvo} no RADGe "
                           "e peça de novo.")
        return titulo

    # ── formulário ───────────────────────────────────────────────────────────
    def _campos_nota(self):
        nomes = [n for n in (self.m.get("conferir") or ["fornecedor", "emissao", "entrada"])]
        for extra in ("numero", "lancamento"):
            if extra in self.c and extra not in nomes:
                nomes.append(extra)
        return nomes

    def formulario_vazio(self):
        campos = self.ler_campos(self._campos_nota())
        return not any((v or "").strip() for v in campos.values()), campos

    def limpar(self, obrigatorio=True):
        """Limpar + conferir. Com `obrigatorio`, formulário que não esvazia para
        tudo (é antes de digitar o filtro). Sem, só avisa (é a arrumação final)."""
        campos = {}
        for tentativa in range(1, 4):
            self.j.clicar("limpar", self._spec("limpar"))
            self._espera("espera_limpar_s", 0.4)
            # "Deseja salvar as alterações?" -> Não. Limpar nunca salva.
            for t in self._fechar_dialogos(NEGATIVOS):
                self.log(f"Dialogo do ERP ao limpar: {t}")
            vazio, campos = self.formulario_vazio()
            if vazio:
                self.log("Formulário limpo.")
                return True
            time.sleep(0.4)
        cheios = {k: v for k, v in campos.items() if v}
        if obrigatorio:
            raise ErroTela(f"o formulário de Entradas não esvaziou ({cheios}); não vou "
                           "digitar o filtro por cima de uma nota carregada")
        self.log(f"Atenção: o formulário não esvaziou ({cheios}).")
        return False

    def filtrar(self, tipo, fornecedor):
        self.j.escrever("tipo", self._spec("tipo"), tipo)
        self.j.escrever("fornecedor_cod", self._spec("fornecedor_cod"), fornecedor)
        time.sleep(0.3)
        caixas = self._fechar_dialogos(NEGATIVOS)
        if caixas:
            raise erp_base.ErroERP(f"o ERP reclamou do filtro: {' / '.join(caixas)}")
        lido = {"tipo": self.ler("tipo"), "fornecedor": self.ler("fornecedor_cod")}
        if _int(lido["tipo"]) != _int(tipo) or _int(lido["fornecedor"]) != _int(fornecedor):
            raise erp_base.ErroERP(f"o filtro não entrou: pedi tipo {tipo}, fornecedor "
                                   f"{fornecedor}; o ERP ficou com {lido}")
        nome = ""
        try:
            nome = self.ler("fornecedor")
        except erp_base.ErroERP:
            pass
        self.log(f"Filtro: tipo {tipo}, fornecedor {fornecedor}" + (f" ({nome})" if nome else ""))
        return nome

    def buscar(self):
        """Dispara a busca. Devolve o texto de uma caixa que o ERP abrir
        ("nenhum registro"...), ou ''."""
        self.j.teclas(self.m.get("buscar", "{F5}"))
        self._espera("espera_busca_s", 1.5)
        caixas = self._fechar_dialogos(NEGATIVOS)
        return " / ".join(caixas)

    # ── grade ────────────────────────────────────────────────────────────────
    def _preparar_campos(self):
        """Resolve os campos lidos na varredura ENQUANTO o Form está à vista.
        Com a grade na frente o formulário fica escondido, e um controle
        invisível só é achado se já estiver guardado (foi assim que o valor
        saiu vazio em todas as linhas)."""
        for nome in ("numero", "lancamento", "entrada", "emissao", "valor"):
            if nome in self.c:
                try:
                    self.j.achar(nome, self.c[nome], visivel=True)
                except erp_base.ErroERP as e:
                    self.log(f"Campo '{nome}' nao localizado: {e}")
        for nome, spec in self._specs_situacao():
            self.j.achar(nome, spec)

    def abrir_grade(self):
        self._preparar_campos()
        self.j.clicar("grade", self._spec("grade"))
        self._espera("espera_grade_s", 1.5)
        self.log("Grade aberta.")

    def abrir_form(self):
        self.j.clicar("form", self._spec("form"))
        self._espera("espera_grade_s", 1.5)

    def _grade(self):
        return self.j.achar("grade_notas", self._spec("grade_notas"), visivel=True)

    def _tecla_grade(self, teclas):
        g = self._grade()
        g.type_keys(teclas, set_foreground=False)
        self._espera("espera_linha_s", 0.18)

    def _focar_grade(self):
        g = self._grade()
        self.j.trazer_para_frente()
        # clique por mensagem na primeira linha: dá o foco à grade sem mover o
        # mouse de quem está na máquina
        g.click(coords=(int(self.m.get("grade_dx", 40)), int(self.m.get("grade_dy", 70))))
        self._espera("espera_linha_s", 0.18)

    def _specs_situacao(self):
        aberto = self._spec("situacao_aberto")
        fechado = self.c.get("situacao_fechado") or {
            "class_name": aberto.get("class_name", "TcxDBRadioGroupButton"),
            "texto": "2-Fechado"}
        return (("situacao_aberto", aberto), ("situacao_fechado", fechado))

    def _marcado(self, nome, spec):
        """O botão de opção DevExpress não responde a BM_GETCHECK (sempre 0,
        medido no RADGe em 28/09/2026). O que muda é o WS_TABSTOP: o Delphi
        liga a tabulação só no botão marcado de cada grupo — conferido em
        quatro grupos da tela Entradas, exatamente um com o bit em cada."""
        return bool(self.j.achar(nome, spec).style() & WS_TABSTOP)

    def aberta(self):
        """A nota do registro atual está em aberto? Pelo grupo Situação:
        "1-Aberto" marcado E "2-Fechado" não. Qualquer outra combinação é
        leitura que não se sustenta — erro, em vez de chutar "fechada" (foi
        esse chute que fez notas abertas sumirem). `aberta_por` no mapa troca
        o método: "fechar_nf" (botão habilitado) ou "radio" (BM_GETCHECK)."""
        por = self.m.get("aberta_por", "tabstop")
        if por == "fechar_nf":
            return self.j.habilitado("fechar_nf", self._spec("fechar_nf"))
        if por == "radio":
            c = self.j.achar("situacao_aberto", self._spec("situacao_aberto"))
            return bool(c.send_message(BM_GETCHECK, 0, 0) & 1)
        (na, sa), (nf, sf) = self._specs_situacao()
        aberto, fechado = self._marcado(na, sa), self._marcado(nf, sf)
        if aberto == fechado:
            raise ErroTela(f"não consegui ler a situação da nota (1-Aberto "
                           f"{'marcado' if aberto else 'desmarcado'}, 2-Fechado "
                           f"{'marcado' if fechado else 'desmarcado'})")
        return aberto

    def _diagnosticar_situacao(self):
        """Uma linha de log com o que se leu da situação no 1º registro, para
        conferir o método sem abrir o banco."""
        leituras = []
        try:
            for nome, spec in self._specs_situacao():
                c = self.j.achar(nome, spec)
                leituras.append(f"{spec.get('texto', nome)} tabstop={int(bool(c.style() & WS_TABSTOP))}")
        except Exception as e:
            leituras.append(f"situacao ilegivel ({e})")
        if "fechar_nf" in self.c:
            leituras.append(f"FECHAR NOTA habilitado={self.j.habilitado('fechar_nf', self.c['fechar_nf'])}")
        self.log(f"Situacao da 1a linha: {', '.join(leituras)} "
                 f"(usando {self.m.get('aberta_por', 'tabstop')}).")

    def ler_linha(self, i):
        v = self.ler_campos(["numero", "lancamento", "entrada", "emissao", "valor"])
        return {"numero": v["numero"] or "", "lancamento": v["lancamento"] or "",
                "entrada": v["entrada"] or "", "emissao": v["emissao"] or "",
                "valor": v["valor"] or "", "aberta": self.aberta(), "i": i}

    @staticmethod
    def _chave(l):
        return (l["lancamento"], l["numero"], l["entrada"], l["emissao"])

    def _guarda_inclusao(self, linha):
        """Registro sem número e sem lançamento com data de hoje = o cxGrid
        abriu uma inclusão. ESC desfaz (registro novo ainda não tocado)."""
        hoje = time.strftime("%d/%m/%Y")
        if not linha["numero"] and not linha["lancamento"] and linha["entrada"] in ("", hoje):
            try:
                self._tecla_grade("{ESC}")
            except Exception:
                pass
            raise ErroTela("o formulário entrou em modo de inclusão durante a varredura "
                           "(linha sem número, data de hoje); desfeito com ESC — confira a tela")

    def varrer(self):
        """Lê todas as linhas da grade, de cima para baixo, sem nunca passar da
        última. Devolve a lista de linhas (vazia se a busca não trouxe nada)."""
        self._focar_grade()
        self._tecla_grade("^{END}")
        ultima = self.ler_linha(-1)
        if not ultima["numero"] and not ultima["lancamento"]:
            return []                       # busca vazia: não há registro nenhum
        chave_ultima = self._chave(ultima)
        self._tecla_grade("^{HOME}")
        self._diagnosticar_situacao()
        linhas = []
        anterior = None
        for i in range(MAX_LINHAS):
            self.checar()
            linha = self.ler_linha(i)
            self._guarda_inclusao(linha)
            if anterior is not None and self._chave(linha) == anterior:
                break                       # ↓ não andou: fim da lista
            linhas.append(linha)
            if self._chave(linha) == chave_ultima:
                break
            anterior = self._chave(linha)
            self._tecla_grade("{DOWN}")
        else:
            raise ErroTela(f"a grade passou de {MAX_LINHAS} linhas sem chegar à última nota")
        return linhas

    def ir_para(self, linha):
        """Posiciona a grade na linha `i` e confere que é a nota esperada."""
        self._focar_grade()
        self._tecla_grade("^{HOME}")
        for _ in range(linha["i"]):
            self._tecla_grade("{DOWN}")
        atual = self.ler_linha(linha["i"])
        if self._chave(atual) != self._chave(linha):
            raise ErroTela(f"a grade não parou na nota esperada (linha {linha['i']}: "
                           f"esperava lançamento {linha['lancamento']}, está em "
                           f"{atual['lancamento'] or 'vazio'})")
