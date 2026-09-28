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


class ErroItem(erp_base.ErroERP):
    """Este item não entrou; a linha é cancelada e o próximo pode entrar."""


class ItemPulado(ErroItem):
    """O ERP disse que o produto não existe (não cadastrado / código errado)."""


# Caixas da busca que querem dizer "este código não é um produto" (sem acento,
# minúsculas — comparadas com erp.normalizar).
NAO_CADASTRADO = ("nao cadastrad", "nao encontrad", "inexistente", "invalid",
                  "nao existe")
SIM = ["Sim", "&Sim", "Yes", "&Yes"]


def num_br(s):
    """'1.234,50' / '4' / '4,000' -> float; None se não for número."""
    s = str(s or "").strip().replace("R$", "").replace(" ", "")
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def fmt_qtd(q):
    """Como digitar a quantidade no ERP: '4', '2,5'."""
    q = float(q)
    return str(int(q)) if q == int(q) else f"{q:.3f}".rstrip("0").replace(".", ",")


def mesmo_codigo(a, b):
    """Código do ERP comparado sem zeros à esquerda ('006859' = '6859')."""
    return (str(a or "").strip().lstrip("0") or "0") == (str(b or "").strip().lstrip("0") or "0")


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
        # O ERP minimizado (a pessoa está no navegador pedindo a tarefa) deixa
        # TODO controle invisível e nada é achado ("não achei o campo
        # 'limpar'", visto em 28/09/2026). O conectar() restaura, mas só roda
        # uma vez: cada pedido traz o ERP para a frente antes de começar.
        self.j.trazer_para_frente()
        time.sleep(0.3)
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
        """Volta ao formulário. Os botões Grade/Form se alternam: com o Form à
        vista só o Grade aparece (conferido no RADGe em 28/09/2026). Sem o
        botão Form e com o Grade visível, já estamos no formulário."""
        try:
            self.j.achar("form", self._spec("form"), visivel=True)
        except erp_base.ErroERP:
            try:
                self.j.achar("grade", self._spec("grade"), visivel=True)
            except erp_base.ErroERP:
                raise erp_base.ErroERP("não achei nem o botão Form nem o Grade na tela "
                                       "Entradas; a tela está no estado esperado?")
            return
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

    # ── itens da nota (nf.incluir_itens) ─────────────────────────────────────
    # Fluxo manual confirmado pelo usuário (28/09/2026): Incluir -> modo de
    # inclusão -> F9 abre uma janela de busca com o campo já focado -> código de
    # barras + Enter -> se o ERP não conhecer o código, aparece uma caixa. A
    # quantidade vai em `qtd_caixa`, como veio no pedido. Gravar / Cancelar.
    def nota_atual(self):
        """A nota carregada no Form: número, lançamento, fornecedor, datas,
        valor e se está aberta."""
        self.abrir_form()
        v = self.ler_campos(["numero", "lancamento", "fornecedor", "entrada", "valor"])
        nota = {k: (v.get(k) or "") for k in v}
        nota["aberta"] = self.aberta() if (nota["numero"] or nota["lancamento"]) else False
        return nota

    def _travado(self):
        """Caixa modal aberta desabilita a janela principal inteira (visto com
        a TfrmMensagens): botão nenhum responde e `habilitado` mente."""
        try:
            return not self.j.win.is_enabled()
        except Exception:
            return False

    def _destravar(self, momento):
        """Fecha caixas pendentes (Não/OK). Se a janela continuar travada, para
        o pedido com uma mensagem que diz o porquê."""
        textos = self._caixas_item(momento) if self.j.dialogos() else []
        if self._travado():
            time.sleep(0.5)
            textos += self._caixas_item(momento)
        if self._travado():
            raise ErroTela("o ERP está travado por uma janela que eu não reconheço como "
                           "caixa de mensagem; feche-a na tela e peça de novo")
        return textos

    def em_inclusao(self):
        """Em modo de inclusão o Gravar fica habilitado; fora dele, não."""
        self._destravar("conferindo o modo de inclusao")
        return self.j.habilitado("item_gravar", self._spec("item_gravar"))

    def ler_linha_item(self):
        v = self.ler_campos(["item_codigo", "item_descricao"])
        return {"codigo": v.get("item_codigo") or "", "descricao": v.get("item_descricao") or ""}

    def _caixas_item(self, momento):
        """Caixas do ERP durante o item. NUNCA responde Sim: fecha com Não ou OK
        e devolve os textos. Caixa sem Não/OK trava tudo (ErroTela)."""
        textos = []
        for _ in range(5):
            dlg = self.j.dialogos()
            if not dlg:
                break
            for d in dlg:
                textos.append(d["texto"])
                if not self.j.responder(d, NEGATIVOS + FECHAR):
                    raise ErroTela(f"caixa do ERP ({momento}) sem botão que eu possa apertar: "
                                   f"'{d['texto']}' ({d['botoes']})")
                self.log(f"  caixa do ERP ({momento}): {d['texto']}")
            time.sleep(float(self.m.get("espera_aviso_curta_s", 0.4)))
        return textos

    def entrar_inclusao(self):
        if self.em_inclusao():
            return
        self.j.clicar("item_incluir", self._spec("item_incluir"))
        self._espera("espera_incluir_s", 1.0)
        caixas = self._caixas_item("Incluir")
        if caixas:
            raise ErroTela(f"o ERP respondeu ao Incluir: {' / '.join(caixas)}")
        if not self.em_inclusao():
            raise ErroTela("cliquei em Incluir e o ERP não entrou em modo de inclusão")
        self.log("Modo de inclusao de itens.")

    def _janelas_processo(self):
        """Janelas visíveis do processo do ERP que não são a principal nem
        caixa de diálogo — a janela de busca do F9 aparece aqui."""
        pid = self.j.win.process_id()
        saida = {}
        for w in erp_base.Desktop(backend="win32").windows(process=pid, visible_only=True):
            if w.handle == self.j.win.handle or w.class_name() in self.j.classes_dialogo:
                continue
            saida[w.handle] = w
        return saida

    def _campo_busca(self, janela):
        spec = self.c.get("busca_codigo") or {"class_name": "TEdit", "ordinal": 0}
        cands = janela.descendants(class_name=spec.get("class_name", "TEdit"))
        i = int(spec.get("ordinal", 0))
        return cands[i] if 0 <= i < len(cands) else None

    @staticmethod
    def _existe(w):
        try:
            return w.is_visible()
        except Exception:
            return False

    def _fechar_busca(self, busca):
        if self._existe(busca):
            try:
                busca.type_keys("{ESC}", set_foreground=False)
            except Exception:
                pass
            time.sleep(0.5)

    def buscar_produto(self, barras):
        """F9 -> janela de busca -> código + Enter. ItemPulado se o ERP disser
        que o código não existe; ErroItem se a busca não abrir ou não fechar."""
        antes = set(self._janelas_processo())
        self.j.teclas(self.m.get("abrir_busca_produto", "{F9}"))
        busca = None
        fim = time.time() + float(self.m.get("espera_busca_produto_s", 4))
        while time.time() < fim and busca is None:
            time.sleep(0.2)
            novas = [w for h, w in self._janelas_processo().items() if h not in antes]
            if novas:
                busca = novas[0]
            elif self.j.dialogos():
                raise ErroItem(f"o ERP respondeu ao F9: {' / '.join(self._caixas_item('F9'))}")
        if busca is None:
            raise ErroItem("apertei F9 e a janela de busca de produto não abriu")
        if not getattr(self, "_busca_logada", False):
            self.log(f"Janela de busca: {busca.class_name()} '{busca.window_text()}'")
            self._busca_logada = True
        campo = self._campo_busca(busca)
        if campo is None:
            self._fechar_busca(busca)
            raise ErroItem("não achei o campo de código na janela de busca")
        try:
            campo.set_focus()
        except Exception:
            pass
        campo.type_keys(barras, set_foreground=False, pause=0.03)
        time.sleep(float(self.m.get("espera_enter_s", 0.35)))
        campo.type_keys("{ENTER}", set_foreground=False)
        # espera a janela fechar; caixa no meio = o ERP não achou o código
        fim = time.time() + float(self.m.get("espera_selecao_s", 4))
        segundo_enter = False
        while True:
            time.sleep(0.25)
            if self.j.dialogos():
                caixas = self._caixas_item("busca")
                self._fechar_busca(busca)
                txt = " / ".join(caixas)
                if any(p in erp_base.normalizar(txt) for p in NAO_CADASTRADO):
                    raise ItemPulado(f"o ERP não achou o código: {txt}")
                raise ErroItem(f"o ERP respondeu à busca: {txt}")
            if not self._existe(busca):
                # A caixa "não cadastrado" aparece DEPOIS que a busca fecha
                # (visto no RADGe em 28/09/2026): ler a linha nessa hora dava
                # "linha sem produto", a caixa ficava aberta e travava o
                # Cancelar. Espera `espera_aviso_s` por ela antes de seguir.
                self._aviso_pos_busca()
                return
            if time.time() > fim:
                if segundo_enter:
                    break
                # a busca pode ter mostrado a lista e esperar Enter para escolher
                segundo_enter = True
                self.log("  a busca continuou aberta; Enter de novo para selecionar.")
                try:
                    campo.type_keys("{ENTER}", set_foreground=False)
                except Exception:
                    pass
                fim = time.time() + float(self.m.get("espera_fechar_busca_s", 3))
        self._fechar_busca(busca)
        raise ErroItem("a janela de busca não fechou depois do Enter")

    def _aviso_pos_busca(self):
        """Caixa que o ERP abre logo depois da busca fechar. Responde Não (ou
        OK), e o item vira pulado (código não cadastrado) ou falha."""
        fim = time.time() + float(self.m.get("espera_aviso_s", 1.2))
        while time.time() < fim:
            if self.j.dialogos():
                txt = " / ".join(self._caixas_item("busca"))
                if any(p in erp_base.normalizar(txt) for p in NAO_CADASTRADO):
                    raise ItemPulado(f"o ERP não achou o código: {txt}")
                raise ErroItem(f"o ERP respondeu à busca: {txt}")
            time.sleep(0.15)

    def _ler_numero(self, nome):
        """Número de um campo cx: o texto do controle ou, se vier vazio, o da
        caixa de edição interna (a "casca" do agente antigo)."""
        c = self.j.achar(nome, self._spec(nome), visivel=True)
        textos = [c.window_text()]
        try:
            textos += [f.window_text() for f in c.children()]
        except Exception:
            pass
        for t in textos:
            n = num_br(t)
            if n is not None:
                return n, textos
        return None, textos

    def escrever_qtd(self, qtd):
        texto = fmt_qtd(qtd)
        lido, textos = None, []
        for tentativa in range(1, 4):
            self.j.escrever("qtd_caixa", self._spec("qtd_caixa"), texto)
            time.sleep(float(self.m.get("espera_qtd_s", 0.2)))
            caixas = self._caixas_item("quantidade")
            if caixas:
                raise ErroItem(f"o ERP reclamou da quantidade {texto}: {' / '.join(caixas)}")
            lido, textos = self._ler_numero("qtd_caixa")
            if lido is not None and abs(lido - float(qtd)) < 1e-6:
                return
            self.log(f"  quantidade: digitei {texto}, o campo ficou {textos} "
                     f"(tentativa {tentativa}).")
        raise ErroItem(f"a quantidade nao entrou: pedi {texto} e o campo ficou {lido} "
                       f"({textos})")

    def gravar_item(self):
        """Gravar e conferir que a linha esvaziou (o registro foi salvo e o ERP
        abriu a próxima). Caixa + linha cheia = recusa (o item falha). Linha
        cheia sem caixa = não sei se gravou: ErroTela, que para o pedido sem
        cancelar nada."""
        self._destravar("antes do Gravar")
        self.j.clicar("item_gravar", self._spec("item_gravar"))
        self._espera("espera_gravar_s", 1.0)
        caixas = self._caixas_item("Gravar")
        depois = self.ler_linha_item()
        cheia = bool(depois["codigo"] or depois["descricao"])
        if caixas and cheia:
            raise ErroItem(f"o ERP recusou a gravacao: {' / '.join(caixas)}")
        if cheia:
            raise ErroTela(f"cliquei em Gravar e a linha continuou preenchida ({depois}); "
                           "confira a nota no ERP antes de continuar")
        return depois

    def cancelar_item(self):
        """Descarta a linha em edição e sai do modo de inclusão. A pergunta de
        confirmação do cancelamento é a ÚNICA que leva Sim (descartar a linha
        não salva é a intenção); qualquer outra leva Não/OK."""
        textos = []
        for tentativa in range(1, 3):
            # caixa aberta bloqueia o formulário e o clique no Canc é ignorado
            # (foi o que deixou o ERP em modo de inclusão no pedido #72)
            textos += self._caixas_item("antes do Cancelar")
            if not self.em_inclusao():
                return {"cancelado": True, "caixa": " / ".join(textos)}
            self.j.clicar("item_cancelar", self._spec("item_cancelar"))
            self._espera("espera_cancelar_s", 0.8)
            for d in self.j.dialogos():
                textos.append(d["texto"])
                if "cancel" in erp_base.normalizar(d["texto"]) and "?" in d["texto"]:
                    self.j.responder(d, SIM)
                else:
                    self.j.responder(d, NEGATIVOS + FECHAR)
                time.sleep(0.4)
            if not self.em_inclusao():
                return {"cancelado": True, "caixa": " / ".join(textos)}
            self.log(f"  Cancelar nao tirou do modo de inclusao (tentativa {tentativa}).")
        return {"cancelado": False, "caixa": " / ".join(textos)}
