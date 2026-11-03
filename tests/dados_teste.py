"""Dados de apoio dos testes: catálogo e vendas na pasta temporária.

Tudo idempotente por processo — os testes rodam num ambiente só (ambiente.py).
"""
from datetime import date

from tests.ambiente import montar

_feito = set()


def mes_passado():
    hoje = date.today()
    return f"{hoje.year - (hoje.month == 1):04d}-{(hoje.month - 2) % 12 + 1:02d}"


def produto(cb, descricao, custo, venda, codigo_interno, dep="6"):
    """Põe (ou troca) um produto no catálogo."""
    montar()
    from scripts import catalogo
    conn = catalogo._conn()
    try:
        conn.execute("INSERT OR REPLACE INTO produtos (codigo_barras, codigo_interno, descricao,"
                     " venda, custo, cb_chave, dep) VALUES (?,?,?,?,?,?,?)",
                     (cb, codigo_interno, descricao, venda, custo, cb.lstrip("0") or "0", dep))
        if not conn.execute("SELECT 1 FROM importacoes").fetchone():
            conn.execute("INSERT INTO importacoes (quando, usuario, arquivo) VALUES"
                         " ('2026-10-30 10:00:00', 'teste', 'teste.txt')")
        conn.commit()
    finally:
        conn.close()


def vendas(mes, linhas, total_geral):
    """linhas = [(codigo_interno, codigo_barras, descricao, qtd, valor)]. Substitui o mês."""
    montar()
    from scripts import relatorios_vendas as rv
    import calendar
    y, m = int(mes[:4]), int(mes[5:7])
    ult = calendar.monthrange(y, m)[1]
    dados = {"periodo": f"01/{m:02d}/{y} a {ult:02d}/{m:02d}/{y}", "loja": "teste",
             "total_geral": total_geral,
             "produtos": [{"Intervalo": "", "Código": c, "Código de Barras": cb,
                           "Descrição": d, "Qtd": q, "Valor Total": v, "Perc %": 0,
                           "Classe": "A"} for c, cb, d, q, v in linhas]}
    conn = rv._conn()
    try:
        rv._gravar_no_banco(conn, mes, dados, "teste.pdf")
    finally:
        conn.close()


def base_padrao():
    """Catálogo + um mês de vendas usados por rebaixa e perdas."""
    if "base" in _feito:
        return
    produto("7890000000017", "IOGURTE TESTE 170G", 2.00, 4.49, "000101", dep="6")
    produto("7890000000024", "QUEIJO TESTE KG", 20.00, 39.90, "000102", dep="8")
    produto("7890000000031", "PAO TESTE UN", 0.50, None, "000103", dep="6")
    # 30 un/mês do iogurte = 1 por dia; queijo vende bastante
    vendas(mes_passado(), [("000101", "7890000000017", "IOGURTE TESTE 170G", 30, 134.70),
                           ("000102", "7890000000024", "QUEIJO TESTE KG", 300, 11970.00),
                           ("000999", "", "OUTRO", 10, 7895.30)],
           total_geral=20000.00)
    _feito.add("base")
