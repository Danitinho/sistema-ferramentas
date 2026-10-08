"""
scripts/vencidos_relatorio.py
Relatório de vencidos em CINCO PARTES — MONTAGEM DOS DADOS + a planilha de
trabalho em Excel (leitura pura, não grava nada).

Este módulo é a fonte única: `montar_relatorio()` devolve o modelo, e sobre ele
existem DUAS vistas, cada uma boa numa coisa:
  • `scripts/vencidos_pdf.py` — o documento de APRESENTAÇÃO (capa executiva +
    as cinco partes, A4, para imprimir ou mandar ao chefe);
  • `gerar_excel_vencidos()` aqui — a PLANILHA DE TRABALHO: tabela plana, uma
    linha por item, com autofiltro e totais, para quem quiser cruzar número.
Nada de formatação de impressão no Excel: quem apresenta é o PDF. Foi uma
escolha — a planilha que tenta ser documento perde o autofiltro (mesclagem e
sub-linha quebram o filtro) e não ganha a fidelidade de um PDF.

As partes, nesta ordem:
  1. Geral                 — todos os vencidos do mês.
  2. Com troca             — os que voltaram ao fornecedor (NF de devolução).
  3. Sem troca             — os que viraram perda (nota de perda).
  4. Sem troca e sem aviso — a perda que a seção NÃO avisou: a que poderia ter
     sido evitada. É o número que cobra a regra dos 30 dias.
  5. Avisos de vencimento  — o que AINDA VAI vencer, no mês do relatório e no
     seguinte, com a média de venda mensal do produto, a sobra estimada no dia
     do vencimento e o risco daí decorrente.

Regras que dão sentido ao documento:
  • Partes 2, 3 e 4 são recortes da parte 1 — quem ainda está com baixa
    PENDENTE não tem tipo definido e por isso só aparece na parte 1. A soma das
    partes 2 e 3 é menor que a parte 1 exatamente pelo tanto que falta baixar.
  • A parte 5 olha para FRENTE: só entram avisos ainda não resolvidos e que
    vencem DEPOIS da data de geração — o que já venceu não é mais aviso, é
    vencido (ou uma pendência do estágio anterior).
  • O risco vem do cruzamento com o histórico de venda (vendas.db do módulo de
    relatórios): sobra = quantidade avisada − venda esperada até o vencimento.
    Produto sem venda registrada fica sem estimativa — em branco, nunca zero,
    porque "não sei" e "não vende" são coisas diferentes.

Consequência aceita: o relatório é uma fotografia. Reimprimir o mesmo mês
amanhã dá outra parte 5 (um dia a menos de prazo, outra venda esperada), por
isso todo relatório é carimbado com a data/hora de geração.
"""
import calendar
import re

from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from scripts import vencidos as v

_MES_RE = re.compile(r'^(\d{4})-(\d{2})$')


# ── Meses ─────────────────────────────────────────────────────────────────────
def mes_valido(mes):
    m = _MES_RE.match((mes or "").strip())
    return bool(m) and 1 <= int(m.group(2)) <= 12


def mes_seguinte(mes):
    """'2026-12' → '2027-01'."""
    y, mo = int(mes[:4]), int(mes[5:7])
    return f"{y + 1:04d}-01" if mo == 12 else f"{y:04d}-{mo + 1:02d}"


def _ultimo_dia(mes):
    y, mo = int(mes[:4]), int(mes[5:7])
    return f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"


# ── As quatro partes de vencidos (recortes da mesma lista) ───────────────────
# (chave, título, nota, filtro) — o filtro roda sobre o vencido já enriquecido.
PARTES_VENCIDOS = [
    ("geral", "Geral — todos os vencidos",
     "Tudo que chegou ao escritório no período, baixado ou não. As partes "
     "seguintes são recortes desta: quem ainda está com baixa pendente não tem "
     "tipo definido e só aparece aqui.",
     lambda x: True),
    ("devolucao", "Com troca — devolução ao fornecedor",
     "Baixados como devolução: a mercadoria voltou ao fornecedor e o valor não "
     "virou prejuízo.",
     lambda x: x.get("baixa_tipo") == "devolucao"),
    ("perda", "Sem troca — perda",
     "Baixados como perda: não houve troca, o valor saiu do caixa.",
     lambda x: x.get("baixa_tipo") == "perda"),
    ("perda_sem_aviso", "Sem troca e sem aviso de vencimento",
     "Perda que a seção não avisou com antecedência. É a perda que poderia ter "
     "sido evitada — o número que cobra a regra dos "
     f"{v.DIAS_MINIMO} dias.",
     lambda x: x.get("baixa_tipo") == "perda" and not x.get("foi_avisado")),
]

NIVEL_LABEL = {"alto": "Alto", "medio": "Médio", "baixo": "Baixo"}

RANKING_LIMITE = 5   # quantas linhas de cada ranking vão para a capa do PDF


# ── Montagem ──────────────────────────────────────────────────────────────────
def _totais(itens):
    return {
        "qtd":        len(itens),
        "quantidade": round(sum(x.get("quantidade") or 0 for x in itens), 3),
        "valor":      round(sum(x.get("valor_perdido") or 0 for x in itens), 2),
        "avisados":   sum(1 for x in itens if x.get("foi_avisado")),
        "pendentes":  sum(1 for x in itens if x.get("baixa_status") != "baixado"),
    }


def _avisos_futuros(mes_base, mes_seg, hoje):
    """Avisos ativos que vencem do dia seguinte à geração até o fim do mês
    seguinte. Dois meses numa lista só, sem repetir (um aviso pertence a um mês
    de vencimento, mas a consulta é feita duas vezes)."""
    vistos, saida = set(), []
    for m in (mes_base, mes_seg):
        for a in v.listar_avisos(mes=m, incluir_resolvidos=False, limite=0,
                                 ordem="vencimento"):
            if a["id"] in vistos:
                continue
            # só o que ainda VAI vencer: o que venceu antes da geração já é
            # assunto do estágio de vencidos, não de aviso.
            if (a.get("data_vencimento") or "") <= hoje:
                continue
            vistos.add(a["id"])
            saida.append(a)
    # mais urgente primeiro; empatou no dia, quem tem mais sobra estimada sobe
    saida.sort(key=lambda a: (a["data_vencimento"],
                              -((a.get("risco") or {}).get("sobra") or 0)))
    return saida


def _totais_avisos(avisos):
    com_risco = [a for a in avisos if a.get("risco")]
    return {
        "qtd":          len(avisos),
        "quantidade":   round(sum(a.get("quantidade") or 0 for a in avisos), 3),
        # valor do estoque avisado (o que está em jogo) e o que a estimativa diz
        # que deve sobrar de fato no dia do vencimento
        "valor":        round(sum((a.get("quantidade") or 0) * (a.get("custo") or 0)
                                  for a in avisos), 2),
        "sobra":        round(sum(a["risco"]["sobra"] for a in com_risco), 1),
        "valor_sobra":  round(sum(a["risco"]["valor_sobra"] for a in com_risco), 2),
        "com_risco":    len(com_risco),
        "alto":         sum(1 for a in com_risco if a["risco"]["nivel"] == "alto"),
        "sem_estimativa": len(avisos) - len(com_risco),
    }


def montar_relatorio(mes=None, ordem=v.ORDEM_PADRAO):
    """Relatório do mês em cinco partes. `mes` = 'AAAA-MM'; '' (ou None) traz
    todos os meses nas partes de vencidos — a parte 5, que olha para frente,
    continua ancorada no mês corrente."""
    hoje = v._hoje()
    mes = (mes or "").strip()
    todos = (mes == "")
    if not todos and not mes_valido(mes):
        mes = hoje[:7]
        todos = False
    base = hoje[:7] if todos else mes          # âncora da parte 5
    seg = mes_seguinte(base)

    if ordem not in v.ORDENS_VENCIDOS:
        ordem = v.ORDEM_PADRAO
    lista = v.listar_vencidos(mes=(None if todos else mes), limite=0, ordem=ordem)

    partes = []
    for chave, titulo, nota, filtro in PARTES_VENCIDOS:
        itens = [x for x in lista if filtro(x)]
        partes.append({"chave": chave, "titulo": titulo, "nota": nota,
                       "itens": itens, "totais": _totais(itens)})

    avisos = _avisos_futuros(base, seg, hoje)
    parte_avisos = {
        "chave": "avisos",
        "titulo": f"Avisos de vencimento — {v.rotulo_mes(base)} e {v.rotulo_mes(seg)}",
        "nota": (f"O que ainda vai vencer, de {v._fmt_dia(hoje)} (exclusive) até "
                 f"{v._fmt_dia(_ultimo_dia(seg))}. 'Venda/mês' é a média dos últimos "
                 f"meses fechados; 'Sobra est.' é o que deve restar no dia do "
                 f"vencimento no ritmo atual. Sem venda registrada, sem estimativa."),
        "itens": avisos,
        "totais": _totais_avisos(avisos),
    }

    escopo = "todos os meses" if todos else v.rotulo_mes(mes)
    geral = partes[0]["totais"]
    # o que já foi decidido (devolução/perda) e o que ainda não tem destino:
    # baixa só existe com tipo, então o resto é exatamente o pendente.
    dev, perda = partes[1]["totais"]["valor"], partes[2]["totais"]["valor"]
    return {
        "mes":            "" if todos else mes,
        "mes_label":      escopo,
        "todos_meses":    todos,
        "mes_avisos":     base,
        "mes_avisos_seg": seg,
        "escopo":         escopo,
        "hoje":           hoje,
        "gerado_em":      datetime.now().strftime("%d/%m/%Y %H:%M"),
        "partes":         partes,
        "avisos":         parte_avisos,
        "resumo": {
            "total":       geral["qtd"],
            "valor":       geral["valor"],
            "avisados":    geral["avisados"],
            "pct_avisado": round(geral["avisados"] * 100 / geral["qtd"]) if geral["qtd"] else 0,
            "pendentes":   geral["pendentes"],
            "devolucao":   dev,
            "perda":       perda,
            "perda_sem_aviso": partes[3]["totais"]["valor"],
            "valor_pendente":  round(geral["valor"] - dev - perda, 2),
            "risco_proximo":   parte_avisos["totais"]["valor_sobra"],
        },
        # janela FIXA de 6 meses (não é do mês do relatório) — a capa rotula assim
        "rankings": {
            "fornecedores": v.ranking_fornecedores(RANKING_LIMITE),
            "reincidencia": v.ranking_reincidencia(RANKING_LIMITE),
        },
    }


# ── Excel: a PLANILHA DE TRABALHO ─────────────────────────────────────────────
# Tabela PLANA — uma linha por item, autofiltro, painel congelado e linha de
# total. Nada de mesclagem, sub-linha ou quebra de página: isso é papel do PDF,
# e é justamente o que estragaria o filtro aqui. Cada parte do relatório vira
# uma aba (nome ≤ 31 caracteres, limite do Excel).
_XL_HEADER = "1F3A5F"
_XL_ZEBRA  = "F3F6FA"
_XL_TOTAL  = "EAF0F6"
_XL_BORDA  = Border(left=Side("thin", color="D8DEE6"), right=Side("thin", color="D8DEE6"),
                    top=Side("thin", color="D8DEE6"), bottom=Side("thin", color="D8DEE6"))
_XL_MOEDA  = 'R$ #,##0.00'
_XL_QTD    = '#,##0.###'
_XL_FONTE  = "Segoe UI"


def _sim_nao(v_):
    return "Sim" if v_ else "Não"


def _risco(a, campo):
    r = a.get("risco")
    return r[campo] if r else None


# (título, extrator, largura, formato numérico)
COLUNAS_VENCIDOS = [
    ("Produto",           lambda x: x.get("produto") or "",                 38, None),
    ("Código de barras",  lambda x: x.get("codigo_barras") or "",           20, None),
    ("Quantidade",        lambda x: x.get("quantidade"),                    12, _XL_QTD),
    ("Fornecedor",        lambda x: x.get("fornecedor") or "",              26, None),
    ("Custo unit.",       lambda x: x.get("custo"),                         13, _XL_MOEDA),
    ("Valor perdido",     lambda x: x.get("valor_perdido"),                 14, _XL_MOEDA),
    ("Foi avisado",       lambda x: _sim_nao(x.get("foi_avisado")),         12, None),
    ("Entregue por",      lambda x: x.get("responsavel_entrega") or "",     20, None),
    ("Registrado em",     lambda x: x.get("criado_fmt") or "",              17, None),
    ("Registrado por",    lambda x: x.get("registrado_por") or "",          16, None),
    ("Situação da baixa", lambda x: "Baixado" if x.get("baixa_status") == "baixado" else "Pendente", 16, None),
    ("Tipo de baixa",     lambda x: x.get("baixa_tipo_label") or "",        21, None),
    ("Nº nota / NF",      lambda x: x.get("baixa_ref") or "",               16, None),
    ("Baixa em",          lambda x: x.get("baixa_fmt") or "",               17, None),
    ("Baixa por",         lambda x: x.get("baixa_por") or "",               14, None),
    ("Observação",        lambda x: x.get("obs") or "",                     32, None),
]

# A aba de avisos tem colunas próprias: é o que ainda VAI vencer, então traz o
# prazo e a estimativa de sobra em vez dos campos de baixa.
COLUNAS_AVISOS = [
    ("Produto",            lambda a: a.get("produto") or "",                38, None),
    ("Código de barras",   lambda a: a.get("codigo_barras") or "",          20, None),
    ("Quantidade",         lambda a: a.get("quantidade"),                   12, _XL_QTD),
    ("Fornecedor",         lambda a: a.get("fornecedor") or "",             26, None),
    ("Vencimento",         lambda a: a.get("data_venc_fmt") or "",          13, None),
    ("Dias p/ vencer",     lambda a: a.get("dias_para_vencer"),             13, "#,##0"),
    ("Custo unit.",        lambda a: a.get("custo"),                        13, _XL_MOEDA),
    ("Valor do estoque",   lambda a: round((a.get("quantidade") or 0) * (a.get("custo") or 0), 2), 16, _XL_MOEDA),
    ("Venda/mês (média)",  lambda a: _risco(a, "media_mensal"),             17, '#,##0.0'),
    ("Venda esperada",     lambda a: _risco(a, "venda_esperada"),           15, '#,##0.0'),
    ("Sobra estimada",     lambda a: _risco(a, "sobra"),                    15, '#,##0.0'),
    ("Valor da sobra",     lambda a: _risco(a, "valor_sobra"),              15, _XL_MOEDA),
    ("Risco",              lambda a: NIVEL_LABEL.get((a.get("risco") or {}).get("nivel"), "Sem estimativa"), 15, None),
    ("Avisado por",        lambda a: a.get("responsavel") or "",            20, None),
    ("Avisado em",         lambda a: a.get("criado_fmt") or "",             17, None),
    ("Antecedência (dias)", lambda a: a.get("dias_antecedencia"),           18, "#,##0"),
    ("No prazo",           lambda a: _sim_nao(a.get("no_prazo")),           10, None),
    ("Promoção",           lambda a: a.get("valor_promocional"),            12, _XL_MOEDA),
    ("Observação",         lambda a: a.get("obs") or "",                    32, None),
]

# Em qual coluna somar na linha de TOTAL (por título — a posição pode mudar).
_SOMAR_VENCIDOS = ("Quantidade", "Valor perdido")
_SOMAR_AVISOS = ("Quantidade", "Valor do estoque", "Sobra estimada", "Valor da sobra")


def _escrever_aba(ws, linhas, colunas, titulo, somar):
    n = len(colunas)
    # linha 1: escopo (o que esta aba é, e quantos itens tem)
    ws.append([titulo])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
    c = ws.cell(1, 1)
    c.font = Font(bold=True, name=_XL_FONTE, size=11, color=_XL_HEADER)
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 22

    # linha 2: cabeçalho (congelado e com autofiltro)
    ws.append([col[0] for col in colunas])
    for i in range(1, n + 1):
        cell = ws.cell(2, i)
        cell.font      = Font(bold=True, color="FFFFFF", name=_XL_FONTE, size=9.5)
        cell.fill      = PatternFill("solid", start_color=_XL_HEADER)
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border    = _XL_BORDA
    ws.row_dimensions[2].height = 28

    idx_soma = [i for i, col in enumerate(colunas, 1) if col[0] in somar]
    totais = {i: 0.0 for i in idx_soma}
    for pos, item in enumerate(linhas):
        ws.append([col[1](item) for col in colunas])
        r = ws.max_row
        fill = PatternFill("solid", start_color=_XL_ZEBRA) if pos % 2 else None
        for i, col in enumerate(colunas, 1):
            cell = ws.cell(r, i)
            cell.font   = Font(name=_XL_FONTE, size=9)
            cell.border = _XL_BORDA
            if fill:
                cell.fill = fill
            if col[3]:
                cell.alignment = Alignment(horizontal="right")
                cell.number_format = col[3]
            if i in totais and isinstance(cell.value, (int, float)):
                totais[i] += cell.value

    if linhas:
        ws.append([""] * n)
        r = ws.max_row
        ws.cell(r, 1).value = f"TOTAL ({len(linhas)} item(ns))"
        for i in idx_soma:
            ws.cell(r, i).value = round(totais[i], 2)
        for i, col in enumerate(colunas, 1):
            cell = ws.cell(r, i)
            cell.font   = Font(bold=True, name=_XL_FONTE, size=9.5, color=_XL_HEADER)
            cell.fill   = PatternFill("solid", start_color=_XL_TOTAL)
            cell.border = _XL_BORDA
            if i in idx_soma:
                cell.alignment = Alignment(horizontal="right")
                cell.number_format = col[3]
    else:
        ws.append(["Nenhum registro nesta aba."])
        ws.cell(ws.max_row, 1).font = Font(name=_XL_FONTE, size=9, italic=True,
                                           color="7A8794")

    for i, col in enumerate(colunas, 1):
        ws.column_dimensions[get_column_letter(i)].width = col[2]
    ws.freeze_panes = "A3"
    ws.auto_filter.ref = f"A2:{get_column_letter(n)}{max(ws.max_row, 2)}"


# Nome da aba por parte (≤31 caracteres) — a chave casa com `PARTES_VENCIDOS`.
ABAS = {
    "geral":           "Todos",
    "devolucao":       "Com troca (devolução)",
    "perda":           "Sem troca (perda)",
    "perda_sem_aviso": "Sem troca e sem aviso",
}
ABA_AVISOS = "Avisos de vencimento"


def gerar_excel_vencidos(rel, destino):
    """Planilha de trabalho: uma aba por parte do relatório, tabela plana com
    autofiltro. `rel` é a saída de `montar_relatorio`; `destino` é caminho ou
    file-like (ex.: BytesIO)."""
    wb = Workbook()
    for pos, parte in enumerate(rel["partes"]):
        ws = wb.active if pos == 0 else wb.create_sheet()
        ws.title = ABAS.get(parte["chave"], parte["chave"])[:31]
        titulo = (f"Vencidos — {parte['titulo']} — {rel['escopo']} — "
                  f"{parte['totais']['qtd']} item(ns)")
        _escrever_aba(ws, parte["itens"], COLUNAS_VENCIDOS, titulo, _SOMAR_VENCIDOS)

    avisos = rel["avisos"]
    ws = wb.create_sheet()
    ws.title = ABA_AVISOS[:31]
    titulo = (f"{avisos['titulo']} — {avisos['totais']['qtd']} aviso(s) — "
              f"gerado em {rel['gerado_em']}")
    _escrever_aba(ws, avisos["itens"], COLUNAS_AVISOS, titulo, _SOMAR_AVISOS)

    wb.save(destino)
    return destino


# ══════════════════════════════════════════════════════════════════════════════
# Relatório de AVISOS DE VENCIMENTO — o controle da vigília
# ══════════════════════════════════════════════════════════════════════════════
# Documento à parte do relatório mensal: aquele fecha o mês (o que venceu e que
# destino teve); este olha para FRENTE e responde "o que foi avisado e está
# para vencer, e o que fazer com cada um". Não depende do mês da tela — é
# ancorado no dia de hoje, com um horizonte em dias.
#
# Regras:
#   • Só avisos ATIVOS (não resolvidos): o resolvido já virou vencido e está no
#     relatório mensal.
#   • O aviso que JÁ VENCEU e não foi registrado como vencido entra SEMPRE,
#     qualquer que seja o horizonte, numa parte própria no topo: é a falha do
#     controle — ou o produto vendeu (e o aviso pede limpeza), ou está na
#     gôndola vencido.
#   • As partes seguem as faixas de urgência de `vencidos._enriquecer_aviso`
#     (crítico ≤7d · atenção 8–30d · programado 31–90d · antecipado >90d) e só
#     aparecem as que cabem no horizonte.

HORIZONTES = [7, 15, 30, 60, 90]     # None = sem limite
HORIZONTE_PADRAO = 30

# (chave = status do aviso, menor nº de dias da faixa, título, nota)
FAIXAS_AVISO = [
    ("vencido", None, "Já venceram — sem registro de vencido",
     "O prazo passou e o produto não chegou ao escritório como vencido. Conferir "
     "na loja: se ainda está na gôndola, retirar e registrar; se vendeu tudo, o "
     "aviso pode ser excluído."),
    ("critico", 0, "Críticos — vencem em até 7 dias",
     "Última janela para agir: rebaixa de preço, troca com o fornecedor ou "
     "transferência. O que sobrar vira vencido."),
    ("atencao", 8, "Atenção — vencem de 8 a 30 dias",
     "Ainda dá para escoar no ritmo normal ou com promoção. Os de risco alto "
     "pedem ação já."),
    ("programado", 31, "Programados — vencem de 31 a 90 dias",
     "Acompanhar. A estimativa de sobra indica quem pode precisar de promoção."),
    ("antecipado", 91, "Antecipados — vencem em mais de 90 dias",
     "Avisados com bastante folga; só monitorar."),
]
FAIXA_LABEL = {
    "vencido": "Já venceu", "critico": "Crítico (≤7d)", "atencao": "Atenção (8–30d)",
    "programado": "Programado (31–90d)", "antecipado": "Antecipado (>90d)",
}


def horizonte_valido(dias):
    """Converte o `?dias=` da query: vazio = padrão; 'todos'/0 = sem limite."""
    bruto = str(dias if dias is not None else "").strip().lower()
    if not bruto:
        return HORIZONTE_PADRAO
    if bruto == "todos":
        return None
    try:
        n = int(bruto)
    except ValueError:
        return HORIZONTE_PADRAO
    if n < 0:
        return HORIZONTE_PADRAO
    return n or None


def _valor_estoque(a):
    return round((a.get("quantidade") or 0) * (a.get("custo") or 0), 2)


def _por_fornecedor_avisos(avisos):
    grupos = {}
    for a in avisos:
        nome = (a.get("fornecedor") or "").strip() or "(sem fornecedor)"
        g = grupos.setdefault(nome.upper(), {
            "fornecedor": nome, "avisos": 0, "quantidade": 0.0, "valor": 0.0,
            "sobra": 0.0, "valor_sobra": 0.0, "criticos": 0, "vencidos": 0})
        g["avisos"] += 1
        g["quantidade"] += a.get("quantidade") or 0
        g["valor"] += _valor_estoque(a)
        if a.get("risco"):
            g["sobra"] += a["risco"]["sobra"]
            g["valor_sobra"] += a["risco"]["valor_sobra"]
        if a.get("status") == "critico":
            g["criticos"] += 1
        elif a.get("status") == "vencido":
            g["vencidos"] += 1
    for g in grupos.values():
        g["quantidade"] = round(g["quantidade"], 3)
        g["sobra"] = round(g["sobra"], 1)
        g["valor"] = round(g["valor"], 2)
        g["valor_sobra"] = round(g["valor_sobra"], 2)
    return sorted(grupos.values(), key=lambda g: (-g["valor"], g["fornecedor"]))


def montar_relatorio_avisos(horizonte=HORIZONTE_PADRAO):
    """Avisos ativos que vencem em até `horizonte` dias a partir de hoje (None =
    todos), mais os que já venceram sem registro. Leitura pura."""
    hoje = v._hoje()
    ativos_todos = v.listar_avisos(incluir_resolvidos=False, limite=0, ordem="vencimento")

    def entra(a):
        if a["status"] == "vencido" or horizonte is None:
            return True
        d = a.get("dias_para_vencer")
        return d is not None and d <= horizonte

    avisos = [a for a in ativos_todos if entra(a)]
    for a in avisos:
        a["valor_estoque"] = _valor_estoque(a)
        a["faixa_label"] = FAIXA_LABEL.get(a["status"], a["status"])
    # mais urgente primeiro; empatou no dia, quem tem mais sobra sobe
    avisos.sort(key=lambda a: (a.get("data_vencimento") or "",
                               -((a.get("risco") or {}).get("sobra") or 0)))

    partes = []
    for chave, desde, titulo, nota in FAIXAS_AVISO:
        # faixa inteira além do horizonte não é parte deste relatório
        if desde is not None and horizonte is not None and desde > horizonte:
            continue
        itens = [a for a in avisos if a["status"] == chave]
        partes.append({"chave": chave, "titulo": titulo, "nota": nota,
                       "itens": itens, "totais": _totais_avisos(itens)})

    # "a vencer" = o que ainda dá para salvar; o já vencido tem conta própria
    a_vencer = [a for a in avisos if a["status"] != "vencido"]
    t = _totais_avisos(a_vencer)
    ja_venceram = [a for a in avisos if a["status"] == "vencido"]
    criticos = [a for a in avisos if a["status"] == "critico"]

    sobra_rk = sorted((a for a in a_vencer
                       if a.get("risco") and a["risco"]["valor_sobra"] > 0),
                      key=lambda a: -a["risco"]["valor_sobra"])
    por_forn = _por_fornecedor_avisos(avisos)
    return {
        "hoje":       hoje,
        "horizonte":  horizonte,
        "escopo":     (f"vencendo nos próximos {horizonte} dias" if horizonte
                       else "todos os avisos ativos"),
        "gerado_em":  datetime.now().strftime("%d/%m/%Y %H:%M"),
        "itens":      avisos,
        "partes":     partes,
        "por_fornecedor": por_forn,
        "resumo": {
            "a_vencer":       t["qtd"],
            "quantidade":     t["quantidade"],
            "valor":          t["valor"],
            "sobra":          t["sobra"],
            "valor_sobra":    t["valor_sobra"],
            "alto":           t["alto"],
            "sem_estimativa": t["sem_estimativa"],
            "criticos":       len(criticos),
            "criticos_valor_sobra": round(sum(a["risco"]["valor_sobra"]
                                              for a in criticos if a.get("risco")), 2),
            "criticos_alto":  sum(1 for a in criticos
                                  if (a.get("risco") or {}).get("nivel") == "alto"),
            "ja_venceram":    len(ja_venceram),
            "ja_venceram_valor": round(sum(a["valor_estoque"] for a in ja_venceram), 2),
            "fora_prazo":     sum(1 for a in avisos if not a.get("no_prazo")),
        },
        "rankings": {
            "fornecedores": por_forn[:RANKING_LIMITE],
            "sobra":        sobra_rk[:RANKING_LIMITE],
        },
    }


# Excel do relatório de avisos: a lista inteira (com a faixa, para filtrar) e o
# resumo por fornecedor — o que se leva para negociar a troca.
COLUNAS_RELATORIO_AVISOS = (COLUNAS_AVISOS[:5]
                            + [("Faixa", lambda a: a.get("faixa_label") or "", 20, None)]
                            + COLUNAS_AVISOS[5:])

COLUNAS_FORN_AVISOS = [
    ("Fornecedor",        lambda g: g["fornecedor"],    36, None),
    ("Avisos",            lambda g: g["avisos"],        10, "#,##0"),
    ("Quantidade",        lambda g: g["quantidade"],    12, _XL_QTD),
    ("Valor do estoque",  lambda g: g["valor"],         16, _XL_MOEDA),
    ("Sobra estimada",    lambda g: g["sobra"],         15, '#,##0.0'),
    ("Valor da sobra",    lambda g: g["valor_sobra"],   15, _XL_MOEDA),
    ("Críticos (≤7d)",    lambda g: g["criticos"],      13, "#,##0"),
    ("Já venceram",       lambda g: g["vencidos"],      13, "#,##0"),
]
_SOMAR_FORN = ("Avisos", "Quantidade", "Valor do estoque", "Sobra estimada",
               "Valor da sobra", "Críticos (≤7d)", "Já venceram")


def gerar_excel_avisos(rel, destino):
    """Planilha do relatório de avisos (`rel` = `montar_relatorio_avisos`)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Avisos"
    titulo = (f"Avisos de vencimento — {rel['escopo']} — {len(rel['itens'])} aviso(s) "
              f"— gerado em {rel['gerado_em']}")
    _escrever_aba(ws, rel["itens"], COLUNAS_RELATORIO_AVISOS, titulo, _SOMAR_AVISOS)

    ws = wb.create_sheet()
    ws.title = "Por fornecedor"
    titulo = (f"Avisos por fornecedor — {rel['escopo']} — "
              f"{len(rel['por_fornecedor'])} fornecedor(es)")
    _escrever_aba(ws, rel["por_fornecedor"], COLUNAS_FORN_AVISOS, titulo, _SOMAR_FORN)

    wb.save(destino)
    return destino
