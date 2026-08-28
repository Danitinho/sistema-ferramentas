"""
scripts/vencidos_pdf.py
=======================
Relatório de vencidos em PDF — o documento de APRESENTAÇÃO.

É um renderizador, não um relatório: os dados vêm prontos de
`vencidos_relatorio.montar_relatorio()` e a identidade visual vem de
`relatorio_pdf` (base comum a todos os relatórios em PDF). O Excel (planilha de
trabalho, para filtrar e cruzar número) e este PDF são duas vistas do MESMO
modelo — nenhuma regra de negócio mora aqui.

Por que PDF e não Excel para apresentar: o Excel muda de cara conforme a versão,
o zoom e o aparelho de quem abre (e some a formatação no celular ou no Google
Sheets). O PDF é fixo — imprime e é lido igual em qualquer lugar.

Estrutura do documento:
  • Página 1 — CAPA EXECUTIVA. É a página que o chefe lê: os quatro números do
    mês, a barra que decompõe o valor total em devolvido / perdido / ainda
    pendente, os dois alertas (baixas paradas e o valor em risco no mês que
    vem) e os dois rankings de 6 meses, que respondem "de onde vem o problema".
  • Páginas seguintes — as CINCO PARTES, como detalhamento.

Decisões de renderização que não são gosto:
  • Cada item ocupa duas linhas — a de números e uma sub-linha com o que não
    cabe na horizontal (código de barras, quem entregou, a baixa, obs).
  • Parte VAZIA não gasta uma folha: só parte com item abre página nova. Num mês
    bom, três partes vazias custariam três folhas em branco.

A dependência (`reportlab`) vem protegida da base: se faltar no servidor,
`DISPONIVEL` fica False e só o botão de PDF sai do ar — a tela de vencidos
continua funcionando.
"""
from scripts import vencidos as v
from scripts import relatorio_pdf as base
from scripts.relatorio_pdf import DISPONIVEL, ERRO_IMPORT, brl, esc, num

if DISPONIVEL:
    from reportlab.lib import colors
    from reportlab.lib.units import mm
    from reportlab.platypus import (KeepTogether, LongTable, PageBreak, Paragraph,
                                    Spacer, TableStyle)

# Grades de coluna (em mm, somam 182 = a largura útil da folha)
_COLS_VENCIDOS = [62, 34, 16, 22, 24, 24]
_COLS_AVISOS   = [62, 32, 20, 16, 26, 26]
_N_COLS = 6

_NIVEL_COR = {"alto": base.VERM, "medio": base.AMBAR, "baixo": base.VERDE}
_NIVEL_LABEL = {"alto": "ALTO", "medio": "MÉDIO", "baixo": "baixo"}


# ── Capa ──────────────────────────────────────────────────────────────────────
def _cartoes(rel, est):
    r = rel["resumo"]
    return base.cartoes_kpi([
        (brl(r["valor"]), "VALOR TOTAL DOS VENCIDOS", f"{r['total']} item(ns)", base.NAVY),
        (brl(r["perda"]), "PERDA EFETIVA (SEM TROCA)",
         f"{brl(r['perda_sem_aviso'])} sem aviso", base.VERM),
        (brl(r["devolucao"]), "DEVOLVIDO AO FORNECEDOR", "voltou como troca", base.VERDE),
        (f"{r['pct_avisado']}%", "AVISADO PREVIAMENTE",
         f"{r['avisados']} de {r['total']} itens", base.INK),
    ], est)


def _alertas(rel, est):
    r = rel["resumo"]
    ta = rel["avisos"]["totais"]
    seg = v.rotulo_mes(rel["mes_avisos_seg"])
    return base.alertas([
        (f"{r['pendentes']} baixa(s) pendente(s)",
         f"{brl(r['valor_pendente'])} sem destino definido — nem devolução, nem "
         f"nota de perda. Enquanto não baixa, o valor fica no limbo.",
         base.AMBAR, base.AMBAR_BG),
        (f"{brl(ta['valor_sobra'])} em risco",
         f"Sobra estimada de {ta['qtd']} aviso(s) ativo(s) até o fim de {seg}, "
         f"no ritmo de venda atual. {ta['alto']} produto(s) em risco alto.",
         base.VERM, base.VERM_BG),
    ], est)


def _rankings(rel, est):
    """De onde vem o problema: fornecedor e produto que mais repetem. A janela
    é FIXA em 6 meses (não é o mês do relatório) — por isso o rótulo diz."""
    rk = rel["rankings"]
    saida = base.tabela_ranking(
        "FORNECEDORES COM MAIOR PERDA — ÚLTIMOS 6 MESES",
        ["Fornecedor", "Itens", "Qtd", "Valor"],
        [(x["fornecedor"], str(x["itens"]), num(x["qtd"]), brl(x["valor"]))
         for x in rk["fornecedores"]],
        [104, 22, 22, 34], est, vazio="Sem perdas registradas na janela.")
    saida.append(Spacer(1, 8 * mm))
    saida += base.tabela_ranking(
        "PRODUTOS REINCIDENTES — ÚLTIMOS 6 MESES (2+ OCORRÊNCIAS)",
        ["Produto", "Vezes", "Qtd", "Valor"],
        [(x["produto"], str(x["vezes"]), num(x["qtd"]), brl(x["valor"]))
         for x in rk["reincidencia"]],
        [104, 22, 22, 34], est,
        vazio="Nenhum produto vencido duas vezes ou mais na janela.")
    return saida


def _capa(rel, est, f):
    r = rel["resumo"]
    story = base.cabecalho_documento(
        "CONTROLE DE VENCIDOS", rel["escopo"].capitalize(),
        f"Relatório gerado em {rel['gerado_em']} · Sistema de Ferramentas", est)
    story += [
        Spacer(1, 8 * mm),
        _cartoes(rel, est),
        Spacer(1, 10 * mm),
        Paragraph("COMPOSIÇÃO DO VALOR VENCIDO NO PERÍODO", est["bloco"]),
        Spacer(1, 2 * mm),
        base.BarraEmpilhada([("Devolvido", r["devolucao"], base.VERDE),
                             ("Perda", r["perda"], base.VERM),
                             ("Baixa pendente", r["valor_pendente"], base.AMBAR)],
                            base.UTIL, f, vazio="Sem vencidos no período."),
        Spacer(1, 9 * mm),
        _alertas(rel, est),
        Spacer(1, 10 * mm),
    ]
    story += _rankings(rel, est)
    story += [
        Spacer(1, 9 * mm),
        Paragraph(
            "Este relatório segue em cinco partes: (1) todos os vencidos do período, "
            "(2) os que voltaram ao fornecedor, (3) os que viraram perda, (4) a perda "
            "que a seção não avisou com antecedência e (5) os avisos do que ainda vai "
            f"vencer, com a estimativa de sobra. A regra da casa é avisar com "
            f"{v.DIAS_MINIMO} dias ou mais.",
            est["nota"]),
    ]
    return story


# ── Partes ────────────────────────────────────────────────────────────────────
def _situacao(x):
    if x.get("baixa_status") != "baixado":
        return "Pendente", base.AMBAR
    if x.get("baixa_tipo") == "devolucao":
        return "Devolução", base.VERDE
    if x.get("baixa_tipo") == "perda":
        return "Perda", base.VERM
    return "Baixado", base.INK


def _detalhe_vencido(x):
    """O que não cabe na horizontal, na sub-linha logo abaixo do item."""
    p = []
    if x.get("codigo_barras"):
        p.append(f"cód {x['codigo_barras']}")
    p.append("avisado" if x.get("foi_avisado") else "<b>SEM AVISO PRÉVIO</b>")
    vin = x.get("vinculo")
    if vin:
        p.append(f"avisadas {num(vin['avisadas'])} un, aproveitadas "
                 f"{num(vin['aproveitadas'])} ({vin['pct_aproveitado']}%)")
    if x.get("responsavel_entrega"):
        p.append(f"entregue por {esc(x['responsavel_entrega'])}")
    if x.get("criado_fmt"):
        p.append(f"registrado {x['criado_fmt']}")
    if x.get("baixa_status") == "baixado":
        baixa = f"baixa: {esc(x.get('baixa_tipo_label') or '')}"
        if x.get("baixa_ref"):
            baixa += f" {esc(x['baixa_ref'])}"
        if x.get("baixa_fmt"):
            baixa += f" em {x['baixa_fmt']}"
        p.append(baixa)
    if x.get("obs"):
        p.append(f"obs: {esc(x['obs'])}")
    return " · ".join(p)


def _detalhe_aviso(a):
    p = []
    risco = a.get("risco")
    if risco:
        p.append(f"<b>risco {_NIVEL_LABEL.get(risco['nivel'], risco['nivel'])}</b>: "
                 f"sobra estimada {num(risco['sobra'], 1)} un "
                 f"({brl(risco['valor_sobra'])}), venda esperada até o vencimento "
                 f"{num(risco['venda_esperada'], 1)} un")
    else:
        p.append("sem venda registrada no período — sem estimativa de sobra")
    if a.get("dias_para_vencer") is not None:
        p.append(f"vence em {a['dias_para_vencer']} dia(s)")
    if a.get("codigo_barras"):
        p.append(f"cód {a['codigo_barras']}")
    if a.get("responsavel"):
        texto = f"avisado por {esc(a['responsavel'])}"
        if a.get("dias_antecedencia") is not None:
            texto += f" com {a['dias_antecedencia']} dia(s) de antecedência"
            if not a.get("no_prazo"):
                texto += f" — <b>ABAIXO dos {v.DIAS_MINIMO}</b>"
        p.append(texto)
    if a.get("promocao"):
        p.append(f"em promoção a {brl(a['valor_promocional'])}")
    if a.get("obs"):
        p.append(f"obs: {esc(a['obs'])}")
    return " · ".join(p)


def _tabela_vencidos(itens, est, f):
    dados = [base.cabecalho_tabela(
        ["Produto", "Fornecedor", "Qtd", "Custo un.", "Valor", "Situação"], est, 2)]
    estilo = base.estilo_tabela(f)
    for pos, x in enumerate(itens):
        rotulo, cor = _situacao(x)
        dados.append([
            Paragraph(esc(x.get("produto")), est["td"]),
            Paragraph(esc(x.get("fornecedor")), est["td_sec"]),
            num(x.get("quantidade")),
            brl(x.get("custo")),
            brl(x.get("valor_perdido")),
            rotulo,
        ])
        r = len(dados) - 1
        estilo += [("TEXTCOLOR", (5, r), (5, r), colors.HexColor(cor)),
                   ("FONTNAME", (5, r), (5, r), f["b"])]
        base.linha_detalhe(dados, estilo, r, _detalhe_vencido(x), est, _N_COLS,
                           zebra=(pos % 2 == 1))
    t = LongTable(dados, colWidths=[c * mm for c in _COLS_VENCIDOS], repeatRows=1)
    t.setStyle(TableStyle(estilo))
    return t


def _tabela_avisos(itens, est, f):
    dados = [base.cabecalho_tabela(
        ["Produto", "Fornecedor", "Vence", "Qtd", "Venda/mês", "Sobra est."], est, 2)]
    estilo = base.estilo_tabela(f)
    for pos, a in enumerate(itens):
        risco = a.get("risco")
        cor = _NIVEL_COR.get(risco["nivel"], base.INK) if risco else base.MUTED
        dias = a.get("dias_para_vencer")
        dados.append([
            Paragraph(esc(a.get("produto")), est["td"]),
            Paragraph(esc(a.get("fornecedor")), est["td_sec"]),
            a.get("data_venc_fmt") or "",
            num(a.get("quantidade")),
            num(risco["media_mensal"], 1) if risco else "—",
            num(risco["sobra"], 1) if risco else "—",
        ])
        r = len(dados) - 1
        estilo += [("TEXTCOLOR", (5, r), (5, r), colors.HexColor(cor)),
                   ("FONTNAME", (5, r), (5, r), f["b"])]
        if not risco:
            estilo.append(("TEXTCOLOR", (4, r), (4, r), colors.HexColor(base.MUTED)))
        if dias is not None and dias <= 7:
            estilo += [("TEXTCOLOR", (2, r), (2, r), colors.HexColor(base.VERM)),
                       ("FONTNAME", (2, r), (2, r), f["b"])]
        base.linha_detalhe(dados, estilo, r, _detalhe_aviso(a), est, _N_COLS,
                           zebra=(pos % 2 == 1))
    t = LongTable(dados, colWidths=[c * mm for c in _COLS_AVISOS], repeatRows=1)
    t.setStyle(TableStyle(estilo))
    return t


def _parte_vencidos(ordem, parte, est, f):
    t = parte["totais"]
    story = [
        base.barra_secao(f"Parte {ordem} — {parte['titulo']}", est),
        Spacer(1, 2 * mm),
        Paragraph(esc(parte["nota"]), est["nota"]),
        Spacer(1, 3 * mm),
    ]
    if parte["itens"]:
        story.append(_tabela_vencidos(parte["itens"], est, f))
        story.append(base.linha_total(
            [f"TOTAL — {t['qtd']} item(ns)",
             f"{num(t['quantidade'])} un · {brl(t['valor'])}"], est))
    else:
        story.append(Paragraph("Nenhum item nesta parte.", est["vazio"]))
    return story


def _parte_avisos(rel, est, f):
    parte = rel["avisos"]
    t = parte["totais"]
    story = [
        base.barra_secao(f"Parte 5 — {parte['titulo']}", est),
        Spacer(1, 2 * mm),
        Paragraph(esc(parte["nota"]), est["nota"]),
        Spacer(1, 3 * mm),
    ]
    if parte["itens"]:
        story.append(_tabela_avisos(parte["itens"], est, f))
        story.append(base.linha_total(
            [f"TOTAL — {t['qtd']} aviso(s)",
             f"{num(t['quantidade'])} un avisadas · sobra estimada "
             f"{num(t['sobra'], 1)} un ({brl(t['valor_sobra'])})"], est))
        story += [
            Spacer(1, 3 * mm),
            Paragraph(
                f"Estoque avisado: {brl(t['valor'])} · {t['alto']} produto(s) em risco "
                f"ALTO · {t['sem_estimativa']} sem estimativa de venda (produto sem "
                f"histórico no banco de vendas).", est["nota"]),
        ]
    else:
        story.append(Paragraph("Nenhum aviso ativo vencendo no período.", est["vazio"]))
    return story


# ── Montagem ──────────────────────────────────────────────────────────────────
def gerar_pdf_relatorio(rel, destino):
    """Monta o PDF a partir do dicionário de `montar_relatorio`.
    `destino` é caminho ou file-like."""
    base.verificar()
    f = base.registrar_fontes()
    est = base.estilos(f)

    story = _capa(rel, est, f)
    # Uma parte COM itens sempre abre em folha nova; uma parte VAZIA flui logo
    # abaixo da anterior. A parte 1 abre folha de qualquer jeito, para não colar
    # no rodapé da capa.
    for ordem, parte in enumerate(rel["partes"], 1):
        bloco = _parte_vencidos(ordem, parte, est, f)
        if parte["itens"] or ordem == 1:
            story.append(PageBreak())
            story += bloco
        else:
            story.append(Spacer(1, 7 * mm))
            story.append(KeepTogether(bloco))
    bloco = _parte_avisos(rel, est, f)
    if rel["avisos"]["itens"]:
        story.append(PageBreak())
        story += bloco
    else:
        story.append(Spacer(1, 7 * mm))
        story.append(KeepTogether(bloco))

    return base.construir(destino, story,
                          titulo=f"Controle de vencidos — {rel['escopo']}",
                          rodape_esq=f"Controle de vencidos · {rel['escopo']}",
                          gerado_em=rel["gerado_em"],
                          assunto="Relatório de vencidos")
