"""
scripts/debitos_relatorio.py
Relatório mensal de débitos e pagamentos (leitura pura — não grava nada).

Regra de competência (a que dá sentido ao fechamento do mês):
  • O DÉBITO pertence ao mês do seu PERÍODO DE REFERÊNCIA (`periodo_inicio`/
    `periodo_fim`), nunca ao `debitos.data` — as notas de julho são lançadas no
    começo de agosto, então a data de digitação diria "agosto" e mentiria.
  • O PAGAMENTO herda o mês do DÉBITO que ele abate. Uma bonificação que chegou
    em 28/07 e só foi digitada em 02/08 continua contando em julho/junho, junto
    do débito que quitou.
  • Como um pagamento pode se repartir entre débitos de meses diferentes (o
    `alocar_automatico` faz FIFO), a unidade do relatório é a ALOCAÇÃO, não o
    pagamento. Cada linha traz o valor aplicado e o total do pagamento de origem.

Dinheiro sem débito não tem mês por essa regra (crédito avulso, a sobra de um
pagamento maior que o saldo, e o que abateu débito antigo sem período). Esse
resto NÃO some do relatório: cai no mês em que foi lançado, numa seção à parte
("crédito não aplicado"), fora dos totais do fechamento.

Consequência aceita: o relatório é retroativo — reimprimir julho depois de novos
lançamentos dá números diferentes. Por isso todo relatório é carimbado com a
data/hora de geração.

Leitura em TRÊS PARTES, e nenhum número somado dos dois meses aparece antes da
terceira: (1) débitos do mês, (2) débitos do mês anterior — onde estão os
pagamentos mandados ao longo do mês —, (3) consolidado.
"""
import calendar
import re
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from scripts import debitos as db

_EPS = db._EPS
_MES_RE = re.compile(r'^(\d{4})-(\d{2})$')

NATUREZAS = {
    "credito":     "Crédito livre (não aplicado)",
    "sem_periodo": "Aplicado a débito sem período",
}


# ── Meses ─────────────────────────────────────────────────────────────────────
def mes_valido(mes):
    m = _MES_RE.match((mes or "").strip())
    return bool(m) and 1 <= int(m.group(2)) <= 12


def mes_anterior(mes):
    """'2026-01' → '2025-12'."""
    y, mo = int(mes[:4]), int(mes[5:7])
    return f"{y - 1:04d}-12" if mo == 1 else f"{y:04d}-{mo - 1:02d}"


def mes_padrao():
    """Mês anterior ao corrente — em 04/08 o fechamento em pauta é o de julho."""
    return mes_anterior(datetime.now().strftime("%Y-%m"))


def _limites_mes(mes):
    y, mo = int(mes[:4]), int(mes[5:7])
    return f"{y:04d}-{mo:02d}-01", f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"


def meses_disponiveis(cnpj=None):
    """Meses cobertos por algum débito (para o seletor da tela), do mais recente
    ao mais antigo. Sempre inclui o mês padrão, mesmo que ainda esteja vazio."""
    conn = db._conn()
    try:
        sql = ("SELECT periodo_inicio, periodo_fim FROM debitos "
               "WHERE excluido_em IS NULL AND periodo_inicio IS NOT NULL")
        params = []
        if cnpj:
            sql += " AND cnpj = ?"
            params.append(cnpj)
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()
    meses = set()
    for r in rows:
        meses.update(db._meses_entre(r["periodo_inicio"], r["periodo_fim"]))
    meses.add(mes_padrao())
    return [{"mes": m, "rotulo": db.rotulo_mes(m)} for m in sorted(meses, reverse=True)]


# ── Consultas ─────────────────────────────────────────────────────────────────
def _rotulo_debito(tipo, nf_numero, produto):
    return f"NF {nf_numero}" if tipo == "vencimento" else (produto or "rebaixa")


def _grupo(razao, vendedor):
    """Identidade da dívida no relatório. Uma empresa com mais de um vendedor
    tem dívidas SEPARADAS — cada um paga a sua —, então o grupo é
    'MARQUES E MELO (VANUSA)'. Sem vendedor, é só a razão social."""
    v = db.limpar_vendedor(vendedor)
    return f"{razao} ({v})" if v else razao


def _debitos_do_mes(conn, mes, cnpj=None):
    """Débitos cujo período cobre `mes` (mesma regra de sobreposição da tela)."""
    primeiro, ultimo = _limites_mes(mes)
    sql = ("SELECT d.*, e.razao_social FROM debitos d "
           "LEFT JOIN empresas e ON e.cnpj = d.cnpj "
           "WHERE d.excluido_em IS NULL AND d.periodo_inicio IS NOT NULL "
           "AND d.periodo_inicio <= ? AND d.periodo_fim >= ?")
    params = [ultimo, primeiro]
    if cnpj:
        sql += " AND d.cnpj = ?"
        params.append(cnpj)
    sql += " ORDER BY e.razao_social, d.data"

    saida = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        vp = round(d.get("valor_pago") or 0, 2)
        d["valor_pago"]    = vp
        d["saldo"]         = round(d["valor_total"] - vp, 2)
        d["status"]        = db._status(d["valor_total"], vp)
        d["data_fmt"]      = db._fmt_data(d["data"])
        d["periodo_label"] = db._periodo_label(d.get("periodo_tipo"),
                                               d.get("periodo_inicio"), d.get("periodo_fim"))
        d["razao_social"]  = d.get("razao_social") or d["cnpj"]
        d["vendedor"]      = db.limpar_vendedor(d.get("vendedor"))
        d["vendedor_chave"] = db.vendedor_chave(d["vendedor"])
        d["grupo"]         = _grupo(d["razao_social"], d["vendedor"])
        d["rotulo"]        = _rotulo_debito(d["tipo"], d.get("nf_numero"), d.get("produto"))
        d["alocacoes"]     = []
        saida.append(d)
    return saida


def _anexar_alocacoes(conn, debitos):
    """Preenche `alocacoes` de cada débito numa consulta só (evita N+1)."""
    if not debitos:
        return
    por_id = {d["id"]: d for d in debitos}
    marcas = ",".join("?" * len(por_id))
    sql = (f"SELECT a.id, a.valor, a.criado_em, a.debito_id, "
           f"p.id AS pagamento_id, p.tipo, p.referencia, p.data AS pag_data, "
           f"p.valor_total AS pag_total, p.obs AS pag_obs "
           f"FROM alocacoes a JOIN pagamentos p ON p.id = a.pagamento_id "
           f"WHERE a.excluido_em IS NULL AND a.debito_id IN ({marcas}) "
           f"ORDER BY a.criado_em")
    for r in conn.execute(sql, list(por_id)).fetchall():
        por_id[r["debito_id"]]["alocacoes"].append({
            "id":            r["id"],
            "valor":         round(r["valor"], 2),
            "pagamento_id":  r["pagamento_id"],
            "tipo":          r["tipo"],
            "tipo_label":    db.TIPOS_PAGAMENTO.get(r["tipo"], r["tipo"]),
            "referencia":    r["referencia"] or "",
            "pag_total":     round(r["pag_total"], 2),
            # o pagamento se repartiu entre mais de um débito?
            "parcial":       round(r["pag_total"], 2) - round(r["valor"], 2) > _EPS,
            "lancado_em":    db._fmt_data(r["pag_data"]),
            "aplicado_em":   db._fmt_data(r["criado_em"]),
            "obs":           r["pag_obs"] or "",
        })


def _campos_vendedor(vendedor, razao):
    v = db.limpar_vendedor(vendedor)
    return {"vendedor": v, "vendedor_chave": db.vendedor_chave(v),
            "grupo": _grupo(razao, v)}


def _avulsos_do_mes(conn, mes, cnpj=None):
    """Dinheiro sem mês de referência próprio, pelo mês de LANÇAMENTO:
    crédito ainda livre + o que abateu débito antigo sem período."""
    like = f"{mes}%"
    saida = []

    # (a) crédito livre — pagamento com saldo não alocado
    sql = ("SELECT p.*, e.razao_social FROM pagamentos p "
           "LEFT JOIN empresas e ON e.cnpj = p.cnpj "
           "WHERE p.excluido_em IS NULL AND p.data LIKE ?")
    params = [like]
    if cnpj:
        sql += " AND p.cnpj = ?"
        params.append(cnpj)
    sql += " ORDER BY e.razao_social, p.data"
    for r in conn.execute(sql, params).fetchall():
        disp = round(r["valor_total"] - (r["valor_alocado"] or 0), 2)
        if disp <= _EPS:
            continue
        saida.append({
            "natureza":       "credito",
            "natureza_label": NATUREZAS["credito"],
            "cnpj":           r["cnpj"],
            "razao_social":   r["razao_social"] or r["cnpj"],
            **_campos_vendedor(r["vendedor"], r["razao_social"] or r["cnpj"]),
            "tipo":           r["tipo"],
            "tipo_label":     db.TIPOS_PAGAMENTO.get(r["tipo"], r["tipo"]),
            "referencia":     r["referencia"] or "",
            "valor":          disp,
            "pag_total":      round(r["valor_total"], 2),
            "lancado_em":     db._fmt_data(r["data"]),
            "destino":        "",
            "obs":            r["obs"] or "",
        })

    # (b) alocações em débitos sem período (lançamentos antigos)
    sql = ("SELECT a.valor, p.cnpj, p.tipo, p.referencia, p.data, p.valor_total, p.obs, "
           "p.vendedor, d.tipo AS d_tipo, d.nf_numero, d.produto, e.razao_social "
           "FROM alocacoes a "
           "JOIN pagamentos p ON p.id = a.pagamento_id "
           "JOIN debitos d    ON d.id = a.debito_id "
           "LEFT JOIN empresas e ON e.cnpj = p.cnpj "
           "WHERE a.excluido_em IS NULL AND p.excluido_em IS NULL "
           "AND d.excluido_em IS NULL AND d.periodo_inicio IS NULL AND p.data LIKE ?")
    params = [like]
    if cnpj:
        sql += " AND p.cnpj = ?"
        params.append(cnpj)
    sql += " ORDER BY e.razao_social, p.data"
    for r in conn.execute(sql, params).fetchall():
        saida.append({
            "natureza":       "sem_periodo",
            "natureza_label": NATUREZAS["sem_periodo"],
            "cnpj":           r["cnpj"],
            "razao_social":   r["razao_social"] or r["cnpj"],
            **_campos_vendedor(r["vendedor"], r["razao_social"] or r["cnpj"]),
            "tipo":           r["tipo"],
            "tipo_label":     db.TIPOS_PAGAMENTO.get(r["tipo"], r["tipo"]),
            "referencia":     r["referencia"] or "",
            "valor":          round(r["valor"], 2),
            "pag_total":      round(r["valor_total"], 2),
            "lancado_em":     db._fmt_data(r["data"]),
            "destino":        _rotulo_debito(r["d_tipo"], r["nf_numero"], r["produto"]),
            "obs":            r["obs"] or "",
        })
    return saida


# ── Montagem ──────────────────────────────────────────────────────────────────
def _totais(lista):
    total = round(sum(d["valor_total"] for d in lista), 2)
    pago  = round(sum(d["valor_pago"] for d in lista), 2)
    return {
        "qtd":     len(lista),
        "total":   total,
        "pago":    pago,
        "saldo":   round(total - pago, 2),
        "abertos": sum(1 for d in lista if d["status"] != "quitado"),
    }


def _novo_slot(item):
    return {"cnpj": item["cnpj"], "razao_social": item["razao_social"],
            "vendedor": item.get("vendedor") or "",
            "vendedor_chave": item.get("vendedor_chave") or "",
            "grupo": item.get("grupo") or item["razao_social"]}


def _chave_divida(item):
    """A dívida é (empresa, vendedor): a mesma empresa com dois vendedores são
    duas dívidas separadas, porque cada um responde pelos seus débitos."""
    return (item["cnpj"], item.get("vendedor_chave") or "")


def _por_empresa_parte(debitos):
    """Quebra de UMA parte (um mês) por dívida: débitos, pago e saldo."""
    emp = {}
    for d in debitos:
        s = emp.setdefault(_chave_divida(d), {**_novo_slot(d), "total": 0.0, "pago": 0.0})
        s["total"] += d["valor_total"]
        s["pago"]  += d["valor_pago"]
    saida = []
    for s in emp.values():
        s["total"] = round(s["total"], 2)
        s["pago"]  = round(s["pago"], 2)
        s["saldo"] = round(s["total"] - s["pago"], 2)
        saida.append(s)
    return sorted(saida, key=lambda s: (-s["saldo"], s["grupo"]))


def _por_empresa_consolidado(debs_mes, debs_ant, avulsos):
    """Quebra do fechamento INTEIRO por dívida, com as duas colunas de mês."""
    emp = {}

    def _slot(item):
        return emp.setdefault(_chave_divida(item),
                              {**_novo_slot(item), "deb_mes": 0.0, "pago_mes": 0.0,
                               "deb_ant": 0.0, "pago_ant": 0.0, "credito": 0.0})

    for d in debs_mes:
        s = _slot(d)
        s["deb_mes"]  += d["valor_total"]
        s["pago_mes"] += d["valor_pago"]
    for d in debs_ant:
        s = _slot(d)
        s["deb_ant"]  += d["valor_total"]
        s["pago_ant"] += d["valor_pago"]
    for a in avulsos:
        s = _slot(a)
        if a["natureza"] == "credito":
            s["credito"] += a["valor"]

    saida = []
    for s in emp.values():
        for k in ("deb_mes", "pago_mes", "deb_ant", "pago_ant", "credito"):
            s[k] = round(s[k], 2)
        s["total"] = round(s["deb_mes"] + s["deb_ant"], 2)
        s["pago"]  = round(s["pago_mes"] + s["pago_ant"], 2)
        s["saldo"] = round(s["total"] - s["pago"], 2)
        saida.append(s)
    return sorted(saida, key=lambda s: (-s["saldo"], s["grupo"]))


def _abatimento_por_tipo(partes):
    """Como a dívida está sendo quitada: bonificação, troca ou desconto em
    boleto. A unidade é a ALOCAÇÃO (o valor efetivamente aplicado a um débito),
    não o pagamento — crédito ainda livre não abateu nada e fica de fora."""
    agg = {}
    for parte in partes:
        for d in parte["debitos"]:
            for a in d["alocacoes"]:
                s = agg.setdefault(a["tipo"], {"tipo": a["tipo"],
                                               "tipo_label": a["tipo_label"],
                                               "valor": 0.0, "qtd": 0})
                s["valor"] += a["valor"]
                s["qtd"] += 1
    saida = []
    for s in agg.values():
        s["valor"] = round(s["valor"], 2)
        saida.append(s)
    return sorted(saida, key=lambda s: -s["valor"])


def montar_relatorio(mes=None, cnpj=None):
    """Relatório do fechamento, lido em três partes: os débitos do mês, os do mês
    anterior (que são pagos ao longo dele) e só então o consolidado. Nenhum
    número somado dos dois meses aparece antes da terceira parte — cada mês se
    fecha sozinho. `cnpj` None = todas as empresas."""
    mes = mes if mes_valido(mes) else mes_padrao()
    ant = mes_anterior(mes)
    cnpj = (cnpj or "").strip() or None

    conn = db._conn()
    try:
        debs_mes = _debitos_do_mes(conn, mes, cnpj)
        debs_ant = _debitos_do_mes(conn, ant, cnpj)
        # Débito com período em intervalo pode cobrir os dois meses; conta uma
        # vez só, no mês do relatório — senão o total soma o mesmo débito duas vezes.
        ids_mes = {d["id"] for d in debs_mes}
        debs_ant = [d for d in debs_ant if d["id"] not in ids_mes]
        _anexar_alocacoes(conn, debs_mes + debs_ant)
        avulsos = _avulsos_do_mes(conn, mes, cnpj)
        empresa = db.buscar_empresa(cnpj) if cnpj else None
    finally:
        conn.close()

    lbl_mes, lbl_ant = db.rotulo_mes(mes), db.rotulo_mes(ant)
    partes = [
        {"chave": "mes", "mes": mes, "label": lbl_mes,
         "titulo": f"Débitos de {lbl_mes}",
         "nota": (f"Lançados no fechamento, referentes a {lbl_mes}. "
                  f"São pagos ao longo do mês seguinte."),
         "debitos": debs_mes, "totais": _totais(debs_mes),
         "por_empresa": _por_empresa_parte(debs_mes)},
        {"chave": "ant", "mes": ant, "label": lbl_ant,
         "titulo": f"Débitos de {lbl_ant}",
         "nota": (f"Mês anterior — é aqui que aparecem os pagamentos mandados ao "
                  f"longo de {lbl_mes}, inclusive os digitados depois do fechamento."),
         "debitos": debs_ant, "totais": _totais(debs_ant),
         "por_empresa": _por_empresa_parte(debs_ant)},
    ]

    credito = round(sum(a["valor"] for a in avulsos if a["natureza"] == "credito"), 2)
    sem_per = round(sum(a["valor"] for a in avulsos if a["natureza"] == "sem_periodo"), 2)
    t_mes, t_ant = partes[0]["totais"], partes[1]["totais"]
    por_empresa = _por_empresa_consolidado(debs_mes, debs_ant, avulsos)
    total_deb = round(t_mes["total"] + t_ant["total"], 2)
    total_pago = round(t_mes["pago"] + t_ant["pago"], 2)
    consolidado = {
        "totais": {
            "total_debitos": total_deb,
            "total_pago":    total_pago,
            "saldo":         round(t_mes["saldo"] + t_ant["saldo"], 2),
            # crédito livre NÃO entra no "pago": ainda não abateu nada, somá-lo
            # daria um abatimento maior que o real.
            "credito":       credito,
            "sem_periodo":   sem_per,
            "qtd_debitos":   t_mes["qtd"] + t_ant["qtd"],
            "pct_quitado":   round(total_pago * 100 / total_deb) if total_deb > _EPS else 0,
            # a dívida é (empresa, vendedor): quantas ainda devem alguma coisa
            "dividas":       len(por_empresa),
            "dividas_abertas": sum(1 for e in por_empresa if e["saldo"] > _EPS),
        },
        "por_empresa": por_empresa,
    }

    escopo = (f"{empresa['razao_social']} — {empresa['cnpj']}" if empresa
              else ("Empresa não encontrada" if cnpj else "Todas as empresas"))
    tem_vendedor = any(x["vendedor"] for x in (debs_mes + debs_ant + avulsos))

    return {
        "mes":            mes,
        "mes_label":      lbl_mes,
        "mes_ant":        ant,
        "mes_ant_label":  lbl_ant,
        "cnpj":           cnpj or "",
        "empresa":        empresa,
        "todas_empresas": cnpj is None,
        "tem_vendedor":   tem_vendedor,
        # a quebra por dívida só informa quando há mais de uma no relatório
        "mostrar_quebra": cnpj is None or tem_vendedor,
        "escopo":         escopo,
        "gerado_em":      datetime.now().strftime("%d/%m/%Y %H:%M"),
        "partes":         partes,
        "consolidado":    consolidado,
        "avulsos":        avulsos,
        # de que forma a dívida está sendo quitada (alimenta a capa do PDF)
        "por_tipo":       _abatimento_por_tipo(partes),
    }


def alocacoes_planas(rel):
    """Todas as alocações das duas partes, uma por linha, com o mês do débito.
    É a visão 'de onde saiu o dinheiro que abateu o fechamento'."""
    linhas = []
    for parte in rel["partes"]:
        for d in parte["debitos"]:
            for a in d["alocacoes"]:
                linhas.append({**a,
                               "mes_debito":   parte["label"],
                               "debito":       d["rotulo"],
                               "cnpj":         d["cnpj"],
                               "razao_social": d["razao_social"],
                               # o pagamento é da dívida do vendedor do débito
                               "vendedor":     d["vendedor"],
                               "grupo":        d["grupo"]})
    return linhas


# ── Excel: a PLANILHA DE TRABALHO ─────────────────────────────────────────────
# Tabela PLANA — uma linha por registro, autofiltro, painel congelado e linha de
# total. Nada de mesclagem, sub-linha ou quebra de página: quem apresenta é o PDF
# (`scripts/debitos_pdf.py`), e é justamente a formatação de impressão que
# estragaria o filtro aqui.
#
# Quatro abas, cada uma respondendo uma pergunta:
#   Débitos              — o que cada empresa deve (as duas partes numa tabela só,
#                          com a coluna "Mês de referência" para separar no filtro)
#   Pagamentos aplicados — de onde saiu o dinheiro que abateu (uma linha por
#                          ALOCAÇÃO, que é a unidade real: um pagamento pode se
#                          repartir entre débitos de meses diferentes)
#   Resumo por dívida    — o fechamento por (empresa, vendedor)
#   Crédito não aplicado — o que entrou e ainda não abateu nada
_XL_HEADER = "1F3A5F"
_XL_ZEBRA  = "F3F6FA"
_XL_TOTAL  = "EAF0F6"
_XL_BORDA  = Border(left=Side("thin", color="D8DEE6"), right=Side("thin", color="D8DEE6"),
                    top=Side("thin", color="D8DEE6"), bottom=Side("thin", color="D8DEE6"))
_XL_MOEDA  = 'R$ #,##0.00'
_XL_FONTE  = "Segoe UI"

STATUS_LABEL = {"aberto": "Aberto", "parcial": "Parcial", "quitado": "Quitado"}
TIPO_LABEL   = {"vencimento": "Vencimento", "rebaxa": "Rebaixa"}


# (título, extrator, largura, formato numérico)
COLUNAS_DEBITOS = [
    ("Mês de referência", lambda d: d["_mes_label"],                     16, None),
    ("Empresa",           lambda d: d.get("razao_social") or "",         30, None),
    ("CNPJ",              lambda d: d.get("cnpj") or "",                 20, None),
    ("Vendedor",          lambda d: d.get("vendedor") or "",             16, None),
    ("Tipo",              lambda d: TIPO_LABEL.get(d["tipo"], d["tipo"]), 13, None),
    ("Documento",         lambda d: d.get("rotulo") or "",               24, None),
    ("Período",           lambda d: d.get("periodo_label") or "",        22, None),
    ("Valor",             lambda d: d.get("valor_total"),                14, _XL_MOEDA),
    ("Pago",              lambda d: d.get("valor_pago"),                 14, _XL_MOEDA),
    ("Saldo",             lambda d: d.get("saldo"),                      14, _XL_MOEDA),
    ("Situação",          lambda d: STATUS_LABEL.get(d["status"], d["status"]), 12, None),
    ("Pagamentos",        lambda d: len(d.get("alocacoes") or []),       12, "#,##0"),
    ("Lançado em",        lambda d: d.get("data_fmt") or "",             17, None),
    ("Observação",        lambda d: d.get("obs") or "",                  34, None),
]

COLUNAS_ALOCACOES = [
    ("Mês do débito",   lambda a: a.get("mes_debito") or "",       16, None),
    ("Empresa",         lambda a: a.get("razao_social") or "",     30, None),
    ("CNPJ",            lambda a: a.get("cnpj") or "",             20, None),
    ("Vendedor",        lambda a: a.get("vendedor") or "",         16, None),
    ("Débito abatido",  lambda a: a.get("debito") or "",           22, None),
    ("Forma",           lambda a: a.get("tipo_label") or "",       20, None),
    ("Referência",      lambda a: a.get("referencia") or "",       18, None),
    ("Valor aplicado",  lambda a: a.get("valor"),                  15, _XL_MOEDA),
    ("Total do pagamento", lambda a: a.get("pag_total"),           18, _XL_MOEDA),
    ("Repartido",       lambda a: "Sim" if a.get("parcial") else "Não", 11, None),
    ("Lançado em",      lambda a: a.get("lancado_em") or "",       17, None),
    ("Aplicado em",     lambda a: a.get("aplicado_em") or "",      17, None),
    ("Observação",      lambda a: a.get("obs") or "",              34, None),
]

COLUNAS_RESUMO = [
    ("Empresa",       lambda e: e.get("razao_social") or "", 30, None),
    ("CNPJ",          lambda e: e.get("cnpj") or "",         20, None),
    ("Vendedor",      lambda e: e.get("vendedor") or "",     16, None),
    ("Débito do mês", lambda e: e.get("deb_mes"),            16, _XL_MOEDA),
    ("Débito do mês anterior", lambda e: e.get("deb_ant"),   22, _XL_MOEDA),
    ("Débito total",  lambda e: e.get("total"),              15, _XL_MOEDA),
    ("Pago",          lambda e: e.get("pago"),               14, _XL_MOEDA),
    ("Saldo",         lambda e: e.get("saldo"),              14, _XL_MOEDA),
    ("Crédito livre", lambda e: e.get("credito"),            14, _XL_MOEDA),
]

COLUNAS_AVULSOS = [
    ("Natureza",    lambda a: a.get("natureza_label") or "", 30, None),
    ("Empresa",     lambda a: a.get("razao_social") or "",   30, None),
    ("CNPJ",        lambda a: a.get("cnpj") or "",           20, None),
    ("Vendedor",    lambda a: a.get("vendedor") or "",       16, None),
    ("Forma",       lambda a: a.get("tipo_label") or "",     20, None),
    ("Referência",  lambda a: a.get("referencia") or "",     18, None),
    ("Valor",       lambda a: a.get("valor"),                14, _XL_MOEDA),
    ("Total do pagamento", lambda a: a.get("pag_total"),     18, _XL_MOEDA),
    ("Aplicado em", lambda a: a.get("destino") or "",        22, None),
    ("Lançado em",  lambda a: a.get("lancado_em") or "",     17, None),
    ("Observação",  lambda a: a.get("obs") or "",            34, None),
]


def _escrever_aba(ws, linhas, colunas, titulo, somar=()):
    n = len(colunas)
    ws.append([titulo])
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=n)
    c = ws.cell(1, 1)
    c.font = Font(bold=True, name=_XL_FONTE, size=11, color=_XL_HEADER)
    c.alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 22

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
        ws.cell(r, 1).value = f"TOTAL ({len(linhas)} linha(s))"
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


def _debitos_planos(rel):
    """As duas partes numa lista só, carimbando o mês de referência de cada uma
    — é o que permite filtrar por mês na planilha sem separar em abas."""
    linhas = []
    for parte in rel["partes"]:
        for d in parte["debitos"]:
            linhas.append({**d, "_mes_label": parte["label"]})
    return linhas


def gerar_excel_relatorio(rel, destino):
    """Planilha de trabalho do fechamento, em quatro abas planas com autofiltro.
    `rel` é a saída de `montar_relatorio`; `destino` é caminho ou file-like."""
    wb = Workbook()
    escopo = f"{rel['escopo']} — fechamento de {rel['mes_label']}"

    ws = wb.active
    ws.title = "Débitos"
    debitos = _debitos_planos(rel)
    _escrever_aba(ws, debitos, COLUNAS_DEBITOS,
                  f"Débitos — {escopo} (com {rel['mes_ant_label']}) — "
                  f"{len(debitos)} débito(s)",
                  somar=("Valor", "Pago", "Saldo"))

    ws = wb.create_sheet("Pagamentos aplicados")
    alocacoes = alocacoes_planas(rel)
    _escrever_aba(ws, alocacoes, COLUNAS_ALOCACOES,
                  f"Pagamentos aplicados — {escopo} — {len(alocacoes)} alocação(ões)",
                  somar=("Valor aplicado",))

    ws = wb.create_sheet("Resumo por dívida")
    resumo = rel["consolidado"]["por_empresa"]
    _escrever_aba(ws, resumo, COLUNAS_RESUMO,
                  f"Resumo por dívida (empresa + vendedor) — {escopo} — "
                  f"{len(resumo)} dívida(s)",
                  somar=("Débito do mês", "Débito do mês anterior", "Débito total",
                         "Pago", "Saldo", "Crédito livre"))

    ws = wb.create_sheet("Crédito não aplicado")
    _escrever_aba(ws, rel["avulsos"], COLUNAS_AVULSOS,
                  f"Crédito não aplicado — {escopo} — "
                  f"{len(rel['avulsos'])} lançamento(s)",
                  somar=("Valor",))

    wb.save(destino)
    return destino
