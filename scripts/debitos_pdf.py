"""
scripts/debitos_pdf.py
======================
Relatório de débitos e bonificações em PDF — o documento de APRESENTAÇÃO.

Renderizador puro: os dados vêm de `debitos_relatorio.montar_relatorio()` e a
identidade visual de `relatorio_pdf`. O Excel (planilha de trabalho) e este PDF
são duas vistas do MESMO modelo — nenhuma regra de negócio mora aqui.

Estrutura:
  • Página 1 — CAPA EXECUTIVA: os quatro números do fechamento, a barra que
    separa o que já foi abatido do que está em aberto (e, dentro do aberto, o
    que já atrasou), os dois alertas e dois blocos — quem deve mais e de que
    forma a dívida está sendo quitada.
  • Depois, as TRÊS PARTES do relatório: débitos do mês, débitos do mês anterior
    e o consolidado; mais o crédito não aplicado, quando houver.

O que a capa precisa dizer, e por quê:
  • O número que importa não é o débito total, é o SALDO EM ABERTO — e, dentro
    dele, a fatia do MÊS ANTERIOR, porque essa já passou do prazo: as notas de
    junho deveriam ter sido pagas ao longo de julho.
  • O CRÉDITO LIVRE é dinheiro que a empresa já entregou e que ninguém aplicou
    a débito nenhum. Fica fora do "total abatido" de propósito (senão o
    abatimento pareceria maior do que é), então precisa de um alerta próprio.
  • A REGRA DE COMPETÊNCIA vai escrita na capa. É a coisa mais fácil de o leitor
    entender errado: uma bonificação digitada em agosto pode contar em julho,
    porque o pagamento pertence ao mês do débito que ele abate.
"""
from scripts import debitos as db
from scripts import relatorio_pdf as base
from scripts.relatorio_pdf import DISPONIVEL, ERRO_IMPORT, brl, esc, num

if DISPONIVEL:
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, LongTable, PageBreak, Paragraph,
                                    Spacer, Table, TableStyle)

_EPS = db._EPS

# Grades de coluna (em mm, somam 182 = a largura útil da folha)
_COLS_DEBITOS = [56, 34, 24, 22, 24, 22]   # dívida · documento · valor · pago · saldo · situação
_COLS_QUEBRA  = [74, 27, 27, 27, 27]       # dívida · mês · mês ant · pago · saldo
_COLS_CREDITO = [52, 70, 30, 30]           # natureza · pagamento · lançado · valor

_STATUS_LABEL = {"aberto": "Aberto", "parcial": "Parcial", "quitado": "Quitado"}
_STATUS_COR   = {"aberto": base.VERM, "parcial": base.AMBAR, "quitado": base.VERDE}
_TIPO_LABEL   = {"vencimento": "Vencimento", "rebaxa": "Rebaixa"}

TOPO_RANKING = 5   # linhas dos blocos da capa


# ── Coluna 1: quem é a dívida ─────────────────────────────────────────────────
# A unidade do relatório é a DÍVIDA = (empresa, vendedor). Quando o relatório é
# de uma empresa só e ela não tem vendedores, repetir o nome em toda linha não
# informa nada — aí a coluna passa a mostrar o tipo do débito. Mesma regra
# adaptativa do Excel.
def _rotulo_linha(rel, d):
    if rel["todas_empresas"]:
        return d["grupo"]
    if rel["tem_vendedor"]:
        return d["vendedor"] or "—"
    return _TIPO_LABEL.get(d["tipo"], d["tipo"])


def _titulo_col1(rel):
    if rel["todas_empresas"]:
        return "Empresa / vendedor"
    return "Vendedor" if rel["tem_vendedor"] else "Tipo"


# ── Capa ──────────────────────────────────────────────────────────────────────
def _cartoes(rel, est):
    t = rel["consolidado"]["totais"]
    return base.cartoes_kpi([
        (brl(t["total_debitos"]), "DÉBITO TOTAL DO FECHAMENTO",
         f"{t['qtd_debitos']} débito(s) nos dois meses", base.NAVY),
        (brl(t["total_pago"]), "TOTAL ABATIDO",
         f"{t['pct_quitado']}% do débito", base.VERDE),
        (brl(t["saldo"]), "SALDO EM ABERTO",
         "a receber dos fornecedores", base.VERM),
        (f"{t['dividas_abertas']}", "DÍVIDAS EM ABERTO",
         f"de {t['dividas']} no fechamento", base.INK),
    ], est)


def _alertas(rel, est):
    """Os dois pontos que exigem ação. Quando o número é zero isso é BOA notícia,
    não um alerta — a caixa vira verde e diz o que aconteceu de bom. Uma caixa
    vermelha escrita 'R$ 0,00' assusta à toa e ensina o leitor a ignorá-la."""
    t = rel["consolidado"]["totais"]
    saldo_ant = rel["partes"][1]["totais"]["saldo"]
    if saldo_ant > _EPS:
        atraso = (f"{brl(saldo_ant)} atrasado",
                  f"Saldo de {rel['mes_ant_label']} ainda em aberto. Essas notas "
                  f"deveriam ter sido pagas ao longo de {rel['mes_label']} — é o "
                  f"atraso do fechamento, não o total.",
                  base.VERM, base.VERM_BG)
    else:
        atraso = (f"{rel['mes_ant_label'].capitalize()} quitado",
                  f"Nenhum débito de {rel['mes_ant_label']} ficou em aberto. Tudo o "
                  f"que venceu no prazo do fechamento foi abatido.",
                  base.VERDE, base.VERDE_BG)
    if t["credito"] > _EPS:
        credito = (f"{brl(t['credito'])} de crédito livre",
                   "Já entregue pelas empresas e ainda não aplicado a nenhum débito. "
                   "Fica fora do total abatido enquanto não for alocado.",
                   base.AMBAR, base.AMBAR_BG)
    else:
        credito = ("Sem crédito parado",
                   "Todo pagamento recebido já foi aplicado a algum débito — não há "
                   "valor entregue esperando alocação.",
                   base.VERDE, base.VERDE_BG)
    return base.alertas([atraso, credito], est)


def _blocos(rel, est):
    """Quem deve mais e de que forma a dívida está sendo quitada."""
    saida = []
    if rel["mostrar_quebra"]:
        linhas = [e for e in rel["consolidado"]["por_empresa"]
                  if e["saldo"] > _EPS][:TOPO_RANKING]
        saida += base.tabela_ranking(
            "MAIORES SALDOS EM ABERTO",
            ["Empresa / vendedor", "Débito", "Pago", "Saldo"],
            [(e["grupo"], brl(e["total"]), brl(e["pago"]), brl(e["saldo"]))
             for e in linhas],
            [92, 30, 30, 30], est, vazio="Nenhuma dívida em aberto no fechamento.")
        saida.append(Spacer(1, 8 * mm))

    total = sum(x["valor"] for x in rel["por_tipo"]) or 1
    saida += base.tabela_ranking(
        "COMO A DÍVIDA ESTÁ SENDO ABATIDA",
        ["Forma de abatimento", "Lançamentos", "Valor", "% do abatido"],
        [(x["tipo_label"], str(x["qtd"]), brl(x["valor"]),
          f"{x['valor'] * 100 / total:.0f}%") for x in rel["por_tipo"]],
        [92, 30, 30, 30], est, vazio="Nenhum pagamento aplicado no fechamento.")
    return saida


def _capa(rel, est, f):
    t = rel["consolidado"]["totais"]
    story = base.cabecalho_documento(
        "DÉBITOS E BONIFICAÇÕES",
        f"Fechamento de {rel['mes_label']} · {rel['escopo']}",
        f"Relatório gerado em {rel['gerado_em']} · Sistema de Ferramentas", est)
    story += [
        Spacer(1, 8 * mm),
        _cartoes(rel, est),
        Spacer(1, 10 * mm),
        Paragraph("COMPOSIÇÃO DO DÉBITO DO FECHAMENTO", est["bloco"]),
        Spacer(1, 2 * mm),
        base.BarraEmpilhada(
            [("Abatido", t["total_pago"], base.VERDE),
             (f"Em aberto de {rel['mes_label']}",
              rel["partes"][0]["totais"]["saldo"], base.AMBAR),
             (f"Em aberto de {rel['mes_ant_label']}",
              rel["partes"][1]["totais"]["saldo"], base.VERM)],
            base.UTIL, f, vazio="Nenhum débito no fechamento."),
        Spacer(1, 9 * mm),
        _alertas(rel, est),
        Spacer(1, 10 * mm),
    ]
    story += _blocos(rel, est)
    story += [
        Spacer(1, 9 * mm),
        Paragraph(
            "<b>Como ler:</b> o fechamento junta os débitos de "
            f"{rel['mes_label']} com os de {rel['mes_ant_label']}, que são pagos "
            f"ao longo de {rel['mes_label']}. O débito conta no mês do seu período "
            "de referência, não na data em que foi digitado; e o pagamento conta no "
            "mês do débito que abate — por isso uma bonificação digitada depois do "
            "fechamento ainda aparece no mês certo. O crédito ainda não aplicado "
            "fica fora dos totais. As três partes a seguir fecham cada uma em si: "
            "nenhum número somado dos dois meses aparece antes da parte 3.",
            est["nota"]),
    ]
    return story


# ── Partes 1 e 2 — os débitos ─────────────────────────────────────────────────
def _detalhe_debito(d):
    """Contexto do débito: período de referência, lançamento e observação."""
    p = [f"{_TIPO_LABEL.get(d['tipo'], d['tipo'])}"]
    if d.get("periodo_label"):
        p.append(f"período {d['periodo_label']}")
    if d.get("tipo") == "rebaxa" and d.get("quantidade"):
        p.append(f"{num(d['quantidade'])} un × {brl(d.get('valor_unit'))}")
    if d.get("data_fmt"):
        p.append(f"lançado {d['data_fmt']}")
    if d.get("obs"):
        p.append(f"obs: {esc(d['obs'])}")
    return " · ".join(p)


def _detalhe_alocacao(a):
    """Um pagamento aplicado a este débito. `parcial` = o pagamento se repartiu
    entre mais de um débito, então mostra de quanto ele era no total."""
    texto = f"<b>{brl(a['valor'])}</b> — {esc(a['tipo_label'])}"
    if a.get("referencia"):
        texto += f" {esc(a['referencia'])}"
    if a.get("parcial"):
        texto += f" (parte de {brl(a['pag_total'])})"
    if a.get("lancado_em"):
        texto += f" · lançado {a['lancado_em'][:10]}"
    if a.get("obs"):
        texto += f" · obs: {esc(a['obs'])}"
    return texto


def _tabela_debitos(rel, debitos, est, f):
    dados = [base.cabecalho_tabela(
        [_titulo_col1(rel), "Documento", "Valor", "Pago", "Saldo", "Situação"], est, 2)]
    estilo = base.estilo_tabela(f)
    n = len(_COLS_DEBITOS)
    for pos, d in enumerate(debitos):
        zebra = pos % 2 == 1
        dados.append([
            Paragraph(esc(_rotulo_linha(rel, d)), est["td"]),
            Paragraph(esc(d["rotulo"]), est["td"]),
            brl(d["valor_total"]),
            brl(d["valor_pago"]),
            brl(d["saldo"]),
            _STATUS_LABEL.get(d["status"], d["status"]),
        ])
        r = len(dados) - 1
        cor_saldo = base.VERM if d["saldo"] > _EPS else base.VERDE
        estilo += [
            ("TEXTCOLOR", (3, r), (3, r), colors.HexColor(base.VERDE)),
            ("TEXTCOLOR", (4, r), (4, r), colors.HexColor(cor_saldo)),
            ("TEXTCOLOR", (5, r), (5, r),
             colors.HexColor(_STATUS_COR.get(d["status"], base.INK))),
            ("FONTNAME", (5, r), (5, r), f["b"]),
        ]
        # o contexto fica preso ao débito; os pagamentos fluem (um débito com
        # muitas alocações não pode virar um bloco mais alto que a página)
        ultimo = base.linha_detalhe(dados, estilo, r, _detalhe_debito(d), est, n,
                                    zebra=zebra,
                                    linha_abaixo=not d["alocacoes"])
        for i, a in enumerate(d["alocacoes"]):
            ultimo = base.linha_detalhe(
                dados, estilo, r, _detalhe_alocacao(a), est, n, zebra=zebra,
                nosplit=False, recuo=9 * mm,
                linha_abaixo=(i == len(d["alocacoes"]) - 1))
            estilo.append(("TEXTCOLOR", (0, ultimo), (0, ultimo),
                           colors.HexColor(base.VERDE)))
    t = LongTable(dados, colWidths=[c * mm for c in _COLS_DEBITOS], repeatRows=1)
    t.setStyle(TableStyle(estilo))
    return t


def _parte_debitos(ordem, parte, rel, est, f):
    t = parte["totais"]
    story = [
        base.barra_secao(f"Parte {ordem} — débitos de {parte['label']}", est),
        Spacer(1, 2 * mm),
        Paragraph(esc(parte["nota"]), est["nota"]),
        Spacer(1, 3 * mm),
    ]
    if parte["debitos"]:
        story.append(_tabela_debitos(rel, parte["debitos"], est, f))
        story.append(base.linha_total(
            [f"TOTAL {parte['label'].upper()} — {t['qtd']} débito(s)",
             f"{brl(t['total'])} · pago {brl(t['pago'])} · saldo {brl(t['saldo'])}"],
            est, proporcoes=(0.3, 0.7)))
    else:
        story.append(Paragraph(f"Nenhum débito referente a {parte['label']}.",
                               est["vazio"]))
    return story


# ── Parte 3 — o consolidado ───────────────────────────────────────────────────
def _linhas_resumo(itens, est, f):
    """Os números do fechamento, um por linha: rótulo à esquerda, valor à
    direita, com destaque nos dois que importam (débito total e saldo)."""
    dados, estilo = [], [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ALIGN", (1, 0), (1, -1), "RIGHT"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for i, (rotulo, valor, cor, destaque) in enumerate(itens):
        dados.append([esc(rotulo), brl(valor)])
        estilo += [
            ("FONTNAME", (0, i), (-1, i), f["b"] if destaque else f["n"]),
            ("FONTSIZE", (0, i), (-1, i), 10 if destaque else 8.6),
            ("TEXTCOLOR", (0, i), (-1, i), colors.HexColor(cor)),
            ("LINEBELOW", (0, i), (-1, i), 0.4, colors.HexColor(base.LINHA)),
        ]
        if destaque:
            estilo += [("BACKGROUND", (0, i), (-1, i), colors.HexColor(base.NAVY_2)),
                       ("TOPPADDING", (0, i), (-1, i), 3),
                       ("BOTTOMPADDING", (0, i), (-1, i), 3)]
    t = Table(dados, colWidths=[base.UTIL * 0.62, base.UTIL * 0.38])
    t.setStyle(TableStyle(estilo))
    return t


def _tabela_quebra(rel, est, f):
    """Fechamento por dívida, com as duas colunas de mês."""
    dados = [base.cabecalho_tabela(
        ["Empresa / vendedor", rel["mes_label"].capitalize(),
         rel["mes_ant_label"].capitalize(), "Pago", "Saldo"], est, 1)]
    estilo = base.estilo_tabela(f, num_a_partir=1)
    for pos, e in enumerate(rel["consolidado"]["por_empresa"]):
        dados.append([Paragraph(esc(e["grupo"]), est["td"]),
                      brl(e["deb_mes"]), brl(e["deb_ant"]),
                      brl(e["pago"]), brl(e["saldo"])])
        r = len(dados) - 1
        cor = base.VERM if e["saldo"] > _EPS else base.VERDE
        estilo += [
            ("TEXTCOLOR", (3, r), (3, r), colors.HexColor(base.VERDE)),
            ("TEXTCOLOR", (4, r), (4, r), colors.HexColor(cor)),
            ("FONTNAME", (4, r), (4, r), f["b"]),
            ("LINEBELOW", (0, r), (-1, r), 0.4, colors.HexColor(base.LINHA)),
        ]
        if pos % 2:
            estilo.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor(base.ZEBRA)))
    t = LongTable(dados, colWidths=[c * mm for c in _COLS_QUEBRA], repeatRows=1)
    t.setStyle(TableStyle(estilo))
    return t


def _parte_consolidado(rel, est, f):
    t = rel["consolidado"]["totais"]
    story = [
        base.barra_secao("Parte 3 — consolidado do fechamento", est),
        Spacer(1, 2 * mm),
        Paragraph("Só aqui os dois meses são somados. O crédito livre aparece "
                  "à parte: ainda não abateu nada, então não entra no total pago.",
                  est["nota"]),
        Spacer(1, 4 * mm),
        _linhas_resumo([
            (f"Débitos de {rel['mes_label']}", rel["partes"][0]["totais"]["total"],
             base.INK, False),
            (f"Débitos de {rel['mes_ant_label']}", rel["partes"][1]["totais"]["total"],
             base.INK, False),
            ("DÉBITO TOTAL", t["total_debitos"], base.NAVY, True),
            ("Total abatido", t["total_pago"], base.VERDE, False),
            ("SALDO EM ABERTO", t["saldo"],
             base.VERM if t["saldo"] > _EPS else base.VERDE, True),
            ("Crédito livre (fora do fechamento)", t["credito"], base.MUTED, False),
        ], est, f),
    ]
    if rel["mostrar_quebra"] and rel["consolidado"]["por_empresa"]:
        story += [
            Spacer(1, 7 * mm),
            Paragraph("FECHAMENTO POR DÍVIDA", est["bloco"]),
            Spacer(1, 2 * mm),
            _tabela_quebra(rel, est, f),
            base.linha_total(
                [f"TOTAL — {t['dividas']} dívida(s)",
                 f"{brl(t['total_debitos'])} · pago {brl(t['total_pago'])} · "
                 f"saldo {brl(t['saldo'])}"], est, proporcoes=(0.3, 0.7)),
        ]
    return story


# ── Crédito não aplicado ──────────────────────────────────────────────────────
def _parte_avulsos(rel, est, f):
    dados = [base.cabecalho_tabela(
        ["Natureza", "Pagamento", "Lançado", "Valor"], est, 2)]
    estilo = base.estilo_tabela(f, num_a_partir=2)
    for pos, a in enumerate(rel["avulsos"]):
        ref = f"{a['tipo_label']} {a['referencia']}".strip()
        if a.get("destino"):
            ref += f" → {a['destino']}"
        if rel["todas_empresas"]:
            ref = f"{a['grupo']} · {ref}"
        dados.append([Paragraph(esc(a["natureza_label"]), est["td"]),
                      Paragraph(esc(ref), est["td_sec"]),
                      a["lancado_em"][:10], brl(a["valor"])])
        r = len(dados) - 1
        estilo.append(("LINEBELOW", (0, r), (-1, r), 0.4, colors.HexColor(base.LINHA)))
        if pos % 2:
            estilo.append(("BACKGROUND", (0, r), (-1, r), colors.HexColor(base.ZEBRA)))
    t = LongTable(dados, colWidths=[c * mm for c in _COLS_CREDITO], repeatRows=1)
    t.setStyle(TableStyle(estilo))
    total = round(sum(a["valor"] for a in rel["avulsos"]), 2)
    return [
        base.barra_secao("Crédito não aplicado — fora dos totais do fechamento", est),
        Spacer(1, 2 * mm),
        Paragraph("Crédito ainda livre, sobra de pagamento e abatimento de débito "
                  "antigo sem período. Entra pelo mês em que foi lançado, porque "
                  "não tem mês de referência próprio.", est["nota"]),
        Spacer(1, 3 * mm),
        t,
        base.linha_total([f"TOTAL — {len(rel['avulsos'])} lançamento(s)", brl(total)],
                         est, proporcoes=(0.6, 0.4)),
    ]


# ── Montagem ──────────────────────────────────────────────────────────────────
def gerar_pdf_relatorio(rel, destino):
    """Monta o PDF a partir do dicionário de `montar_relatorio`.
    `destino` é caminho ou file-like."""
    base.verificar()
    f = base.registrar_fontes()
    est = base.estilos(f)

    story = _capa(rel, est, f)
    # Parte COM débitos abre folha nova; parte vazia flui abaixo da anterior
    # (num mês sem lançamento, não faz sentido gastar uma folha para dizer
    # "nenhum débito"). A parte 1 abre folha de qualquer jeito, para não colar
    # no rodapé da capa.
    for ordem, parte in enumerate(rel["partes"], 1):
        bloco = _parte_debitos(ordem, parte, rel, est, f)
        if parte["debitos"] or ordem == 1:
            story.append(PageBreak())
            story += bloco
        else:
            story.append(Spacer(1, 7 * mm))
            story.append(KeepTogether(bloco))

    story.append(PageBreak())
    story += _parte_consolidado(rel, est, f)

    if rel["avulsos"]:
        story.append(Spacer(1, 8 * mm))
        story += _parte_avulsos(rel, est, f)

    return base.construir(
        destino, story,
        titulo=f"Débitos e bonificações — fechamento de {rel['mes_label']}",
        rodape_esq=f"Fechamento de {rel['mes_label']} · {rel['escopo']}",
        gerado_em=rel["gerado_em"],
        assunto="Relatório de débitos e bonificações")
