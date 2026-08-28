"""
scripts/relatorio_pdf.py
========================
Base comum dos relatórios em PDF (vencidos, débitos, e o que vier).

Não sabe nada de nenhum módulo do sistema: só entrega os tijolos com a
identidade visual da casa — paleta, fontes, grade da página, cartões de KPI,
barra empilhada, caixas de alerta, tabelas e o rodapé numerado. Cada relatório
importa daqui e escreve só o que é dele.

Por que existe: dois relatórios que vão para a mesma mesa precisam parecer o
mesmo documento. Com a montagem duplicada eles divergem no primeiro ajuste que
alguém fizer num só. O preço é o acoplamento — mexer aqui mexe em todos os
relatórios, então mudança nesta base pede reteste de TODOS eles.

A dependência (`reportlab`) é importada de forma PROTEGIDA: sem ela,
`DISPONIVEL` fica False e quem chama devolve um aviso em vez de estourar. Um
pip esquecido no servidor não pode derrubar tela nenhuma.

Convenções da página (todas as tabelas obedecem):
  • A4 retrato, 14 mm de margem lateral → `UTIL` = 182 mm de largura útil.
  • A grade de colunas de cada relatório é declarada em MILÍMETROS e tem de
    somar `UTIL/mm` (182). Coluna que estoura empurra a tabela para fora da folha.
  • Texto que pode vir do usuário passa por `esc()` — `Paragraph` lê mini-HTML e
    um '&' num nome de empresa quebra a montagem inteira.
"""
import os

try:
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_CENTER, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas as _canvas
    from reportlab.platypus import (Flowable, KeepTogether, LongTable, PageBreak,
                                    Paragraph, SimpleDocTemplate, Spacer, Table,
                                    TableStyle)
    DISPONIVEL = True
    ERRO_IMPORT = ""
except ImportError as e:            # pragma: no cover - só sem a dependência
    DISPONIVEL = False
    ERRO_IMPORT = str(e)
    mm = 72 / 25.4                  # para os módulos importarem sem estourar
    A4 = (595.27, 841.89)


# ── Paleta (a mesma do app — ver base.html) ──────────────────────────────────
NAVY     = "#1F3A5F"
NAVY_2   = "#EAF0F6"
INK      = "#1F2933"
MUTED    = "#7A8794"
LINHA    = "#D8DEE6"
ZEBRA    = "#F3F6FA"
VERM     = "#A93F35"
VERM_BG  = "#F7E9E7"
VERDE    = "#3F7A52"
VERDE_BG = "#E7F0E9"
AMBAR    = "#9C6F1E"
AMBAR_BG = "#FBF3E2"
BRANCO   = "#FFFFFF"

# Página
MARGEM_X = 14 * mm
MARGEM_TOPO = 15 * mm
MARGEM_BASE = 16 * mm
UTIL = A4[0] - 2 * MARGEM_X


def verificar():
    """Chame antes de montar qualquer coisa."""
    if not DISPONIVEL:
        raise RuntimeError(
            "A biblioteca reportlab nao esta instalada neste Python. "
            "Instale com: python -m pip install -r requirements.txt "
            f"(detalhe: {ERRO_IMPORT})")


# ── Fontes ────────────────────────────────────────────────────────────────────
# Segoe UI é a fonte do sistema (e a do app) e está em qualquer Windows. Se por
# algum motivo não estiver, cai na Helvetica embutida do próprio ReportLab —
# feia, mas o relatório sai.
_FONTES = None


def registrar_fontes():
    global _FONTES
    if _FONTES is not None:
        return _FONTES
    base = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")
    arquivos = {"": "segoeui.ttf", "B": "segoeuib.ttf", "I": "segoeuii.ttf"}
    try:
        for sufixo, arq in arquivos.items():
            caminho = os.path.join(base, arq)
            if not os.path.exists(caminho):
                raise FileNotFoundError(caminho)
            pdfmetrics.registerFont(TTFont("SegoeUI" + sufixo, caminho))
        pdfmetrics.registerFontFamily("SegoeUI", normal="SegoeUI", bold="SegoeUIB",
                                      italic="SegoeUII", boldItalic="SegoeUIB")
        _FONTES = {"n": "SegoeUI", "b": "SegoeUIB", "i": "SegoeUII"}
    except Exception:
        _FONTES = {"n": "Helvetica", "b": "Helvetica-Bold", "i": "Helvetica-Oblique"}
    return _FONTES


def estilos(f):
    def p(nome, **kw):
        kw.setdefault("fontName", f["n"])
        kw.setdefault("textColor", colors.HexColor(INK))
        kw.setdefault("leading", (kw.get("fontSize", 9)) * 1.25)
        return ParagraphStyle(nome, **kw)

    return {
        "titulo":    p("titulo", fontName=f["b"], fontSize=21,
                       textColor=colors.HexColor(NAVY), leading=24),
        "subtitulo": p("subtitulo", fontName=f["n"], fontSize=13,
                       textColor=colors.HexColor(INK), leading=16),
        "carimbo":   p("carimbo", fontName=f["i"], fontSize=8,
                       textColor=colors.HexColor(MUTED)),
        "secao":     p("secao", fontName=f["b"], fontSize=10.5,
                       textColor=colors.HexColor(BRANCO), leading=13),
        "nota":      p("nota", fontName=f["i"], fontSize=7.6,
                       textColor=colors.HexColor(MUTED), leading=10),
        "bloco":     p("bloco", fontName=f["b"], fontSize=9,
                       textColor=colors.HexColor(NAVY), leading=12),
        "th":        p("th", fontName=f["b"], fontSize=7.6,
                       textColor=colors.HexColor(NAVY), leading=9.5),
        "th_dir":    p("th_dir", fontName=f["b"], fontSize=7.6, alignment=TA_RIGHT,
                       textColor=colors.HexColor(NAVY), leading=9.5),
        "td":        p("td", fontSize=8.2, leading=10),
        "td_sec":    p("td_sec", fontSize=8.2, leading=10,
                       textColor=colors.HexColor(MUTED)),
        "detalhe":   p("detalhe", fontName=f["i"], fontSize=6.9, leading=8.6,
                       textColor=colors.HexColor(MUTED)),
        "vazio":     p("vazio", fontName=f["i"], fontSize=8.5,
                       textColor=colors.HexColor(MUTED)),
        "kpi_num":   p("kpi_num", fontName=f["b"], fontSize=16, alignment=TA_CENTER,
                       leading=19),
        "kpi_lbl":   p("kpi_lbl", fontName=f["b"], fontSize=6.4, alignment=TA_CENTER,
                       textColor=colors.HexColor(MUTED), leading=8),
        "kpi_sub":   p("kpi_sub", fontSize=7, alignment=TA_CENTER,
                       textColor=colors.HexColor(MUTED), leading=9),
        "alerta":    p("alerta", fontName=f["b"], fontSize=10.5, leading=13),
        "alerta_sub": p("alerta_sub", fontSize=7.4, leading=9.5,
                        textColor=colors.HexColor(MUTED)),
    }


# ── Formatação pt-BR ──────────────────────────────────────────────────────────
def pt(texto):
    """1,234.56 (formato do Python) → 1.234,56 (formato daqui)."""
    return texto.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def brl(valor):
    return "R$ " + pt(f"{(valor or 0):,.2f}")


def num(valor, casas=None):
    """Quantidade: inteiro sai sem decimais, fracionário com uma casa."""
    valor = valor or 0
    if casas is None:
        casas = 0 if abs(valor - round(valor)) < 0.05 else 1
    return pt(f"{valor:,.{casas}f}")


def esc(texto):
    """Paragraph lê mini-HTML — um & ou < vindo de nome de produto/empresa
    ('DOCE & CIA') quebraria a montagem."""
    return (str(texto or "").replace("&", "&amp;")
            .replace("<", "&lt;").replace(">", "&gt;"))


# ── Barra empilhada ───────────────────────────────────────────────────────────
class BarraEmpilhada(Flowable):
    """Decompõe um total em partes, com percentual dentro e legenda embaixo.
    `segmentos` = [(rótulo, valor, cor)] — valor zero ou negativo não aparece.
    Desenhada no canvas: não vale a pena uma biblioteca de gráfico para isso."""

    ALTURA_BARRA = 9 * mm
    ALTURA_LEGENDA = 6.5 * mm

    def __init__(self, segmentos, largura, fontes, vazio="Sem dados no período."):
        super().__init__()
        self.segmentos = [s for s in segmentos if (s[1] or 0) > 0]
        self.largura = largura
        self.f = fontes
        self.vazio = vazio

    def wrap(self, *_):
        return self.largura, self.ALTURA_BARRA + self.ALTURA_LEGENDA

    def draw(self):
        c = self.canv
        y = self.ALTURA_LEGENDA
        total = sum(s[1] for s in self.segmentos)
        if not total:
            c.setFillColor(colors.HexColor(ZEBRA))
            c.rect(0, y, self.largura, self.ALTURA_BARRA, stroke=0, fill=1)
            c.setFillColor(colors.HexColor(MUTED))
            c.setFont(self.f["i"], 8)
            c.drawString(3 * mm, y + 3 * mm, self.vazio)
            return

        x = 0.0
        for _rotulo, valor, cor in self.segmentos:
            larg = self.largura * valor / total
            c.setFillColor(colors.HexColor(cor))
            c.rect(x, y, larg, self.ALTURA_BARRA, stroke=0, fill=1)
            texto = f"{valor * 100 / total:.0f}%"
            c.setFont(self.f["b"], 8)
            if c.stringWidth(texto, self.f["b"], 8) + 4 * mm < larg:
                c.setFillColor(colors.white)
                c.drawCentredString(x + larg / 2, y + 3.1 * mm, texto)
            x += larg

        # legenda: quadradinho + rótulo + valor, da esquerda para a direita
        x = 0.0
        for rotulo, valor, cor in self.segmentos:
            c.setFillColor(colors.HexColor(cor))
            c.rect(x, 1.4 * mm, 2.6 * mm, 2.6 * mm, stroke=0, fill=1)
            c.setFillColor(colors.HexColor(INK))
            c.setFont(self.f["n"], 7.6)
            texto = f"{rotulo}: {brl(valor)}"
            c.drawString(x + 3.8 * mm, 1.5 * mm, texto)
            x += 3.8 * mm + c.stringWidth(texto, self.f["n"], 7.6) + 7 * mm


# ── Blocos da capa ────────────────────────────────────────────────────────────
def cabecalho_documento(titulo, subtitulo, carimbo, est):
    """Título, filete navy e a linha de geração."""
    return [
        Paragraph(esc(titulo), est["titulo"]),
        Paragraph(esc(subtitulo), est["subtitulo"]),
        Spacer(1, 1.5 * mm),
        Table([[""]], colWidths=[UTIL], rowHeights=[1.6],
              style=TableStyle([("BACKGROUND", (0, 0), (-1, -1),
                                 colors.HexColor(NAVY))])),
        Spacer(1, 1.5 * mm),
        Paragraph(esc(carimbo), est["carimbo"]),
    ]


def cartoes_kpi(cartoes, est, altura=26 * mm):
    """Os números do mês em caixas lado a lado.
    `cartoes` = [(número, rótulo, sublinha, cor)] — 3 ou 4 cabem bem."""
    linha = []
    for numero, rotulo, sub, cor in cartoes:
        est_num = ParagraphStyle("n", parent=est["kpi_num"],
                                 textColor=colors.HexColor(cor))
        linha.append([Paragraph(esc(numero), est_num),
                      Paragraph(esc(rotulo), est["kpi_lbl"]),
                      Paragraph(esc(sub), est["kpi_sub"])])
    larg = UTIL / len(cartoes)
    t = Table([linha], colWidths=[larg] * len(cartoes), rowHeights=[altura])
    t.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(NAVY_2)),
        ("BOX", (0, 0), (-1, -1), 0.6, colors.HexColor(LINHA)),
        ("INNERGRID", (0, 0), (-1, -1), 0.6, colors.HexColor(BRANCO)),
        ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3 * mm),
    ]))
    return t


def alertas(caixas, est):
    """As coisas que exigem ação, em caixas com barra colorida à esquerda.
    `caixas` = [(título, texto, cor, fundo)] — duas, lado a lado."""
    celulas, estilo = [], [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3.5 * mm),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3.5 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 3 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3 * mm),
    ]
    for i, (titulo, sub, cor, fundo) in enumerate(caixas):
        est_t = ParagraphStyle("a", parent=est["alerta"], textColor=colors.HexColor(cor))
        celulas.append([Paragraph(esc(titulo), est_t),
                        Spacer(1, 1.2 * mm),
                        Paragraph(esc(sub), est["alerta_sub"])])
        estilo.append(("BACKGROUND", (i, 0), (i, 0), colors.HexColor(fundo)))
        estilo.append(("LINEBEFORE", (i, 0), (i, 0), 2.2, colors.HexColor(cor)))
    larg = (UTIL - 4 * mm) / len(caixas)
    dentro = Table([celulas], colWidths=[larg] * len(caixas), style=TableStyle(estilo))
    # a folga entre as caixas vem do wrapper, para não virar padding interno
    return Table([[dentro]], colWidths=[UTIL], style=TableStyle([
        ("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))


def tabela_ranking(titulo, cabecalhos, linhas, larguras, est, vazio="Sem dados."):
    """Mini-tabela de ranking da capa: 1ª coluna é texto, o resto vai à direita.
    `linhas` = [(texto, valor, valor, ...)] já formatados. `larguras` em mm."""
    if not linhas:
        return [Paragraph(esc(titulo), est["bloco"]), Paragraph(esc(vazio), est["vazio"])]
    dados = [[Paragraph(esc(c), est["th"] if i == 0 else est["th_dir"])
              for i, c in enumerate(cabecalhos)]]
    for ln in linhas:
        dados.append([Paragraph(esc(ln[0]), est["td"])] + list(ln[1:]))
    t = LongTable(dados, colWidths=[w * mm for w in larguras], repeatRows=1)
    estilo = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (-1, -1), "RIGHT"),
        ("FONTNAME", (1, 1), (-1, -1), registrar_fontes()["n"]),
        ("FONTSIZE", (1, 1), (-1, -1), 8.2),
        ("TEXTCOLOR", (1, 1), (-1, -1), colors.HexColor(INK)),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(NAVY)),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor(LINHA)),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.6),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]
    for i in range(1, len(dados)):
        if i % 2 == 0:
            estilo.append(("BACKGROUND", (0, i), (-1, i), colors.HexColor(ZEBRA)))
    t.setStyle(TableStyle(estilo))
    return [Paragraph(esc(titulo), est["bloco"]), Spacer(1, 1.5 * mm), t]


# ── Blocos das partes ─────────────────────────────────────────────────────────
def barra_secao(texto, est):
    """Faixa navy que abre cada parte — é o que guia a leitura no papel."""
    t = Table([[Paragraph(esc(texto.upper()), est["secao"])]], colWidths=[UTIL])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(NAVY)),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3 * mm),
        ("TOPPADDING", (0, 0), (-1, -1), 2.2 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.2 * mm),
    ]))
    return t


def cabecalho_tabela(titulos, est, texto_ate=1):
    """Linha de cabeçalho: as `texto_ate` primeiras colunas à esquerda, o resto
    à direita (são números)."""
    return [Paragraph(esc(c), est["th"] if i < texto_ate else est["th_dir"])
            for i, c in enumerate(titulos)]


def estilo_tabela(f, num_a_partir=2):
    """Base das tabelas de item. `num_a_partir` = índice (0-based) da primeira
    coluna numérica, que alinha à direita daí em diante."""
    return [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTNAME", (0, 1), (-1, -1), f["n"]),
        ("FONTSIZE", (0, 1), (-1, -1), 8.2),
        ("ALIGN", (num_a_partir, 0), (-1, -1), "RIGHT"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor(NAVY)),
        ("TOPPADDING", (0, 0), (-1, -1), 1.6),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 1),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ]


def linha_detalhe(dados, estilo, linha_item, texto, est, n_cols, zebra=False,
                  nosplit=True, recuo=5 * mm, linha_abaixo=True):
    """Sub-linha do item: mescla a largura toda e leva o que não cabe na
    horizontal. Com `nosplit`, fica amarrada ao item — o par não se separa numa
    virada de página, senão o detalhe aparece órfão no alto da folha seguinte.

    ATENÇÃO: um bloco preso por NOSPLIT que fique mais alto que a página não tem
    como ser quebrado. Quando um item pode ter MUITAS sub-linhas (os pagamentos
    de um débito, por exemplo), amarre só a primeira e deixe as demais fluírem."""
    dados.append([Paragraph(texto, est["detalhe"])] + [""] * (n_cols - 1))
    r = len(dados) - 1
    estilo += [
        ("SPAN", (0, r), (-1, r)),
        ("BOTTOMPADDING", (0, r), (-1, r), 2.6),
        ("LEFTPADDING", (0, r), (0, r), recuo),
    ]
    if linha_abaixo:
        estilo.append(("LINEBELOW", (0, r), (-1, r), 0.4, colors.HexColor(LINHA)))
    if nosplit:
        estilo.append(("NOSPLIT", (0, linha_item), (-1, r)))
    if zebra:
        estilo.append(("BACKGROUND", (0, linha_item), (-1, r), colors.HexColor(ZEBRA)))
    return r


def linha_total(celulas, est, proporcoes=(0.35, 0.65)):
    """Faixa de fechamento de uma parte: rótulo à esquerda, números à direita."""
    f = registrar_fontes()
    t = Table([list(celulas)], colWidths=[UTIL * p for p in proporcoes])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor(NAVY_2)),
        ("LINEABOVE", (0, 0), (-1, -1), 0.8, colors.HexColor(NAVY)),
        ("LINEBELOW", (0, 0), (-1, -1), 0.8, colors.HexColor(NAVY)),
        ("FONTNAME", (0, 0), (-1, -1), f["b"]),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (-1, -1), colors.HexColor(NAVY)),
        ("ALIGN", (1, 0), (-1, 0), "RIGHT"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2.4 * mm),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.4 * mm),
    ]))
    return t


# ── Documento ─────────────────────────────────────────────────────────────────
def fabrica_canvas(rodape_esq, gerado_em):
    """Canvas de DUAS PASSADAS: só no fim se sabe o total de páginas para
    escrever 'Página X de Y'. Desenha também o cabeçalho discreto a partir da
    2ª página — a capa não precisa dele."""
    f = registrar_fontes()

    class _CanvasNumerado(_canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._estados = []

        def showPage(self):
            self._estados.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._estados)
            for estado in self._estados:
                self.__dict__.update(estado)
                self._moldura(total)
                super().showPage()
            super().save()

        def _moldura(self, total):
            pagina = self._pageNumber
            largura, altura = A4
            if pagina > 1:
                self.setFont(f["n"], 7.5)
                self.setFillColor(colors.HexColor(MUTED))
                self.drawString(MARGEM_X, altura - 10 * mm, rodape_esq)
                self.setStrokeColor(colors.HexColor(LINHA))
                self.setLineWidth(0.5)
                self.line(MARGEM_X, altura - 11.5 * mm,
                          largura - MARGEM_X, altura - 11.5 * mm)
            y = 9 * mm
            self.setStrokeColor(colors.HexColor(LINHA))
            self.setLineWidth(0.5)
            self.line(MARGEM_X, y + 4 * mm, largura - MARGEM_X, y + 4 * mm)
            self.setFont(f["n"], 7)
            self.setFillColor(colors.HexColor(MUTED))
            self.drawString(MARGEM_X, y, rodape_esq)
            self.drawCentredString(largura / 2, y, f"Página {pagina} de {total}")
            self.drawRightString(largura - MARGEM_X, y, f"Gerado em {gerado_em}")

    return _CanvasNumerado


def documento(destino, titulo, assunto=""):
    return SimpleDocTemplate(
        destino, pagesize=A4,
        leftMargin=MARGEM_X, rightMargin=MARGEM_X,
        topMargin=MARGEM_TOPO, bottomMargin=MARGEM_BASE,
        title=titulo, author="Sistema de Ferramentas", subject=assunto or titulo)


def construir(destino, story, titulo, rodape_esq, gerado_em, assunto=""):
    """Fecha o documento. `story` é a lista de flowables já montada."""
    doc = documento(destino, titulo, assunto)
    doc.build(story, canvasmaker=fabrica_canvas(rodape_esq, gerado_em))
    return destino
