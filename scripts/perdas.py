"""
scripts/perdas.py
=================
Registro das PERDAS da loja que não são vencido: avaria, furto/desaparecido,
consumo interno, degustação, uso na produção, doação.

O vencido continua no módulo próprio (/vencidos), com aviso, troca e nota. Aqui
fica o resto da quebra, que antes não era registrada em lugar nenhum — e é a
maior parte da perda de um supermercado em hortifrúti, padaria e açougue.

Como o resto do sistema, isto é um REGISTRO DO ESCRITÓRIO: a perda nasce
`pendente` e só vira `lancada` quando alguém marca que a lançou no ERP (nota de
perda / ajuste de estoque), com a referência. A lista dos pendentes sai em
Excel para digitar lá.

Indicador: perda em % da venda do mês — o número que o varejo usa. A venda vem
do banco de relatórios (Curva ABC, `relatorios.total_geral`); por departamento,
a venda de cada produto é ligada ao departamento pelo CÓDIGO INTERNO do ERP
(`vendas.codigo` = `catalogo.codigo_interno`, sem depender de código de barras).
O departamento é o do catálogo HOJE — durante a reclassificação ele muda, e a
tela diz isso. A perda por vencido (baixa `perda` no /vencidos) entra no
painel como mais um motivo, lida de lá, para o total ser a perda inteira.

Persistência: `dados/perdas.db`. ISO, soft-delete, auditoria, busy_timeout
(convenções da seção 6).

Camada de lógica: NÃO importa Flask.
"""
import io
import os
import re
import sqlite3
import uuid
from datetime import date, datetime

from scripts import db_util

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DADOS_DIR = os.path.join(BASE_DIR, "dados")
BANCO = os.path.join(DADOS_DIR, "perdas.db")

MOTIVOS = {
    "avaria":     "Avaria / quebra",
    "furto":      "Furto / desaparecido",
    "consumo":    "Consumo interno",
    "degustacao": "Degustação",
    "producao":   "Uso na produção",
    "doacao":     "Doação",
    "outro":      "Outro",
}
# Motivo que vem do /vencidos (só leitura; não se registra aqui).
MOTIVO_VENCIDO = "vencido"
ROTULO_VENCIDO = "Vencido (baixa como perda)"
SEM_DEP = "sem departamento"


# ── Banco ─────────────────────────────────────────────────────────────────────
def _conn():
    os.makedirs(os.path.dirname(BANCO), exist_ok=True)
    conn = sqlite3.connect(BANCO)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    db_util.preparar(conn, BANCO, _init_schema)
    return conn


def _init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS perdas (
            id            TEXT PRIMARY KEY,
            data          TEXT NOT NULL,        -- AAAA-MM-DD (dia da perda)
            codigo_barras TEXT,
            codigo_interno TEXT,                -- código no ERP (do catálogo)
            produto       TEXT NOT NULL,
            quantidade    REAL NOT NULL,
            custo         REAL,                 -- unitário
            venda         REAL,                 -- unitário, referência
            motivo        TEXT NOT NULL,
            dep           TEXT,                 -- departamento no ERP (catálogo) no registro
            responsavel   TEXT,                 -- quem informou (seção)
            obs           TEXT,
            lanc_status   TEXT NOT NULL DEFAULT 'pendente',   -- pendente | lancada
            lanc_ref      TEXT,
            lanc_em       TEXT,
            lanc_por      TEXT,
            registrado_por TEXT,
            criado_em     TEXT NOT NULL,
            atualizado_em TEXT,
            excluido_em   TEXT,
            excluido_por  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_perda_data ON perdas(data);
        CREATE INDEX IF NOT EXISTS idx_perda_lanc ON perdas(lanc_status);
        CREATE TABLE IF NOT EXISTS auditoria (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            quando      TEXT NOT NULL,
            usuario     TEXT,
            entidade    TEXT NOT NULL,
            entidade_id TEXT,
            acao        TEXT NOT NULL,
            detalhe     TEXT
        );
    """)
    conn.commit()


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hoje():
    return date.today().isoformat()


def _auditar(conn, entidade_id, acao, detalhe="", usuario=None):
    conn.execute("INSERT INTO auditoria (quando, usuario, entidade, entidade_id, acao, detalhe)"
                 " VALUES (?,?,?,?,?,?)", (_agora(), usuario, "perda", entidade_id, acao, detalhe))


_DATA_BR = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
_MES = re.compile(r"^\d{4}-\d{2}$")


def _norm_dia(s):
    s = (s or "").strip()
    m = _DATA_BR.match(s)
    if m:
        s = f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    try:
        return date.fromisoformat(s).isoformat()
    except ValueError:
        return None


def _num(v, positivo=False):
    """Número digitado em pt-BR ("1.234,5") ou com ponto ("1.5"). Negativo, ou
    zero quando `positivo`, é inválido (None)."""
    if v is None:
        return None
    s = str(v).strip()
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        x = round(float(s), 4)
    except ValueError:
        return None
    if x < 0 or (positivo and x == 0):
        return None
    return x


def _fmt_dia(s):
    try:
        return date.fromisoformat((s or "")[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return s or ""


def _fmt_dt(s):
    try:
        return datetime.strptime((s or "")[:16], "%Y-%m-%d %H:%M").strftime("%d/%m/%Y %H:%M")
    except ValueError:
        return s or ""


def rotulo_mes(mes):
    nomes = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
             "setembro", "outubro", "novembro", "dezembro"]
    try:
        return f"{nomes[int(mes[5:7]) - 1]}/{mes[:4]}"
    except (ValueError, IndexError, TypeError):
        return mes or ""


def _limites_mes(mes):
    y, m = int(mes[:4]), int(mes[5:7])
    fim = f"{y + (m == 12):04d}-{m % 12 + 1:02d}-01"
    return f"{y:04d}-{m:02d}-01", fim


def nome_dep(cod):
    if not cod:
        return SEM_DEP
    try:
        from scripts.reclassificacao_estrutura import estrutura
        nome = estrutura().nome_dep(cod)
    except Exception:  # noqa: BLE001
        nome = ""
    return nome or f"Departamento {cod}"


# ── Catálogo (fonte primária de produto, seção 17) ────────────────────────────
def produto_do_catalogo(codigo_barras):
    """Mesma regra do catálogo (exato; variante única de zeros marcada).
    Import protegido: catálogo fora do ar não impede registrar a perda."""
    try:
        from scripts import catalogo
        return catalogo.consultar(codigo_barras)
    except Exception:  # noqa: BLE001
        return {"encontrado": False}


# ── Escrita ───────────────────────────────────────────────────────────────────
def registrar(produto, quantidade, motivo, data=None, codigo_barras=None, custo=None,
              venda=None, responsavel=None, obs=None, usuario=None):
    """Registra uma perda. Custo, venda, código interno e departamento vêm do
    catálogo quando o código de barras casa e o campo veio vazio (o que a
    pessoa digitou vence). Devolve (ok, msg, id)."""
    if motivo not in MOTIVOS:
        return False, "Escolha o motivo da perda.", None
    qtd = _num(quantidade, positivo=True)
    if qtd is None:
        return False, "Quantidade inválida.", None
    dia = _norm_dia(data) if data else _hoje()
    if not dia:
        return False, "Data inválida.", None
    if dia > _hoje():
        return False, "A data da perda não pode ser no futuro.", None
    cb = re.sub(r"\D", "", codigo_barras or "")
    cat = produto_do_catalogo(cb) if cb else {"encontrado": False}
    achou = cat.get("encontrado") and not cat.get("ambiguo")
    produto = (produto or "").strip() or (cat.get("descricao") if achou else "")
    if not produto:
        return False, "Informe o produto (ou um código de barras do catálogo).", None
    c = _num(custo)
    vd = _num(venda)
    if achou:
        c = c if c is not None else cat.get("custo")
        vd = vd if vd is not None else cat.get("venda")
    pid = uuid.uuid4().hex[:10]
    agora = _agora()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO perdas (id, data, codigo_barras, codigo_interno, produto, quantidade,"
            " custo, venda, motivo, dep, responsavel, obs, registrado_por, criado_em,"
            " atualizado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, dia, cb or None, (cat.get("codigo_interno") or None) if achou else None,
             produto, qtd, c, vd, motivo, (cat.get("dep") or None) if achou else None,
             (responsavel or "").strip(), (obs or "").strip(), usuario, agora, agora))
        _auditar(conn, pid, "registrar",
                 f"{produto} · {qtd:g} · {MOTIVOS[motivo]} · {_fmt_dia(dia)}", usuario)
        conn.commit()
    finally:
        conn.close()
    aviso = ""
    if cb and not achou:
        aviso = " Código fora do catálogo: sem departamento e, se não digitou, sem custo."
    elif c is None:
        aviso = " Sem custo: a perda não entra no valor do mês até alguém informar."
    return True, "Perda registrada." + aviso, pid


def excluir(pid, usuario=None):
    conn = _conn()
    try:
        r = conn.execute("SELECT lanc_status FROM perdas WHERE id=? AND excluido_em IS NULL",
                         (pid,)).fetchone()
        if not r:
            return False, "Perda não encontrada."
        if r["lanc_status"] == "lancada":
            return False, "Já foi lançada no ERP: desmarque o lançamento antes de excluir."
        conn.execute("UPDATE perdas SET excluido_em=?, excluido_por=? WHERE id=?",
                     (_agora(), usuario, pid))
        _auditar(conn, pid, "excluir", "", usuario)
        conn.commit()
        return True, "Perda excluída."
    finally:
        conn.close()


def marcar_lancadas(ids, referencia="", usuario=None, desfazer=False):
    """Marca (ou desmarca) várias perdas como lançadas no ERP de uma vez — o
    lançamento costuma ser uma nota de perda só para o lote. Devolve
    (ok, msg, n)."""
    ids = [str(i) for i in (ids or []) if i]
    if not ids:
        return False, "Selecione ao menos uma perda.", 0
    if not desfazer and not (referencia or "").strip():
        return False, "Informe a referência do lançamento (nº da nota de perda ou do ajuste).", 0
    agora = _agora()
    conn = _conn()
    try:
        n = 0
        for pid in ids:
            if desfazer:
                r = conn.execute("UPDATE perdas SET lanc_status='pendente', lanc_ref=NULL,"
                                 " lanc_em=NULL, lanc_por=NULL, atualizado_em=? WHERE id=?"
                                 " AND lanc_status='lancada' AND excluido_em IS NULL",
                                 (agora, pid))
            else:
                r = conn.execute("UPDATE perdas SET lanc_status='lancada', lanc_ref=?, lanc_em=?,"
                                 " lanc_por=?, atualizado_em=? WHERE id=?"
                                 " AND lanc_status='pendente' AND excluido_em IS NULL",
                                 (referencia.strip(), agora, usuario, agora, pid))
            if r.rowcount:
                n += 1
                _auditar(conn, pid, "desfazer_lancamento" if desfazer else "lancada",
                         "" if desfazer else referencia.strip(), usuario)
        conn.commit()
    finally:
        conn.close()
    if desfazer:
        return True, f"{n} perda(s) voltaram para pendente.", n
    return True, f"{n} perda(s) marcadas como lançadas ({referencia.strip()}).", n


# ── Leitura ───────────────────────────────────────────────────────────────────
def _enriquecer(p):
    p["motivo_label"] = MOTIVOS.get(p["motivo"], p["motivo"])
    p["valor"] = round((p["quantidade"] or 0) * p["custo"], 2) if p["custo"] is not None else None
    p["data_fmt"] = _fmt_dia(p["data"])
    p["criado_fmt"] = _fmt_dt(p["criado_em"])
    p["lanc_fmt"] = _fmt_dt(p["lanc_em"] or "")
    p["dep_nome"] = nome_dep(p["dep"])
    return p


def listar(mes=None, motivo=None, lanc=None, busca=None, limite=2000):
    sql = "SELECT * FROM perdas WHERE excluido_em IS NULL"
    params = []
    if mes:
        ini, fim = _limites_mes(mes)
        sql += " AND data >= ? AND data < ?"
        params += [ini, fim]
    if motivo in MOTIVOS:
        sql += " AND motivo = ?"
        params.append(motivo)
    if lanc in ("pendente", "lancada"):
        sql += " AND lanc_status = ?"
        params.append(lanc)
    if busca:
        sql += " AND (produto LIKE ? OR codigo_barras LIKE ? OR responsavel LIKE ?)"
        params += [f"%{busca}%"] * 3
    sql += " ORDER BY (lanc_status = 'pendente') DESC, data DESC, criado_em DESC LIMIT ?"
    params.append(int(limite))
    conn = _conn()
    try:
        return [_enriquecer(dict(r)) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def meses_disponiveis():
    conn = _conn()
    try:
        meses = {r[0] for r in conn.execute(
            "SELECT DISTINCT substr(data,1,7) FROM perdas WHERE excluido_em IS NULL")}
    finally:
        conn.close()
    meses.add(_hoje()[:7])
    return [{"mes": m, "rotulo": rotulo_mes(m)} for m in sorted(meses, reverse=True)]


def _perda_vencidos(mes):
    """Linhas de vencido baixado como PERDA no mês (pela data da baixa — é
    quando a perda é reconhecida). Só leitura; tolerante a falha."""
    try:
        from scripts import vencidos as v
        if not os.path.exists(v.BANCO):
            return []
        ini, fim = _limites_mes(mes)
        conn = sqlite3.connect(f"file:{v.BANCO}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in conn.execute(
                "SELECT codigo_barras, produto, quantidade, custo FROM vencidos"
                " WHERE excluido_em IS NULL AND baixa_status='baixado' AND baixa_tipo='perda'"
                " AND COALESCE(baixa_em, criado_em) >= ? AND COALESCE(baixa_em, criado_em) < ?",
                (ini, fim))]
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return []


def _venda_mes(mes):
    """(total do mês, {dep: venda}, periodo) pelo banco de relatórios. O total é
    o do relatório (o que a Curva ABC soma); por departamento, a venda de cada
    linha vai para o dep do produto no catálogo pelo código interno. Sem banco
    ou sem o mês: (None, {}, None)."""
    try:
        from scripts import relatorios_vendas as rv
        if not os.path.exists(rv.BANCO):
            return None, {}, None
        conn = sqlite3.connect(f"file:{rv.BANCO}?mode=ro", uri=True)
        try:
            rel = conn.execute("SELECT total_geral, periodo FROM relatorios WHERE mes=?",
                               (mes,)).fetchone()
            if not rel:
                return None, {}, None
            linhas = conn.execute("SELECT codigo, SUM(valor_total) FROM vendas WHERE mes=?"
                                  " GROUP BY codigo", (mes,)).fetchall()
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return None, {}, None
    deps = _deps_por_codigo_interno([c for c, _ in linhas])
    por_dep = {}
    for codigo, valor in linhas:
        d = deps.get(codigo) or ""
        por_dep[d] = por_dep.get(d, 0.0) + (valor or 0)
    return rel[0], por_dep, rel[1]


def _deps_por_codigo_interno(codigos):
    try:
        from scripts import catalogo
        if not os.path.exists(catalogo.BANCO):
            return {}
        conn = sqlite3.connect(f"file:{catalogo.BANCO}?mode=ro", uri=True)
        try:
            saida = {}
            lista = [c for c in codigos if c]
            for i in range(0, len(lista), 900):
                parte = lista[i:i + 900]
                for cod, dep in conn.execute(
                        "SELECT codigo_interno, dep FROM produtos WHERE codigo_interno IN"
                        f" ({','.join('?' * len(parte))})", parte):
                    saida[cod] = dep
            return saida
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return {}


def _deps_por_barras(codigos):
    try:
        from scripts import catalogo
        return {cb: p.get("dep") for cb, p in catalogo.produtos_por_codigos(codigos).items()}
    except Exception:  # noqa: BLE001
        return {}


def painel(mes):
    """Números do mês: total por motivo (com o vencido), por departamento, a
    venda e a perda em % da venda. Perda sem custo conta na quantidade, não no
    valor — e o painel diz quantas são."""
    if not _MES.match(mes or ""):
        mes = _hoje()[:7]
    perdas = listar(mes=mes, limite=100000)
    vencidos = _perda_vencidos(mes)
    deps_venc = _deps_por_barras(sorted({(x["codigo_barras"] or "").strip()
                                         for x in vencidos if x.get("codigo_barras")}))

    por_motivo = {}
    por_dep = {}
    sem_custo = 0

    def somar(motivo, rotulo, dep, valor):
        m = por_motivo.setdefault(motivo, {"motivo": motivo, "rotulo": rotulo,
                                           "valor": 0.0, "registros": 0})
        m["registros"] += 1
        d = por_dep.setdefault(dep or "", {"dep": dep or "", "nome": nome_dep(dep),
                                           "valor": 0.0, "registros": 0})
        d["registros"] += 1
        if valor is not None:
            m["valor"] += valor
            d["valor"] += valor

    for p in perdas:
        if p["valor"] is None:
            sem_custo += 1
        somar(p["motivo"], p["motivo_label"], p["dep"], p["valor"])
    for x in vencidos:
        valor = round((x["quantidade"] or 0) * x["custo"], 2) if x["custo"] is not None else None
        if valor is None:
            sem_custo += 1
        somar(MOTIVO_VENCIDO, ROTULO_VENCIDO, deps_venc.get((x["codigo_barras"] or "").strip()),
              valor)

    total = round(sum(m["valor"] for m in por_motivo.values()), 2)
    venda_total, venda_dep, periodo = _venda_mes(mes)
    for d in por_dep.values():
        d["valor"] = round(d["valor"], 2)
        vd = venda_dep.get(d["dep"])
        d["venda"] = round(vd, 2) if vd else None
        d["pct_venda"] = round(d["valor"] / vd * 100, 2) if vd else None
    for m in por_motivo.values():
        m["valor"] = round(m["valor"], 2)
        m["pct_total"] = round(m["valor"] / total * 100, 1) if total else 0
    pendentes = [p for p in perdas if p["lanc_status"] == "pendente"]
    return {
        "mes": mes, "rotulo": rotulo_mes(mes),
        "total": total,
        "venda": round(venda_total, 2) if venda_total else None,
        "venda_periodo": periodo,
        "pct_venda": round(total / venda_total * 100, 2) if venda_total else None,
        "por_motivo": sorted(por_motivo.values(), key=lambda m: -m["valor"]),
        "por_dep": sorted(por_dep.values(), key=lambda d: -d["valor"]),
        "sem_custo": sem_custo,
        "pendentes_lancar": len(pendentes),
        "valor_pendente": round(sum(p["valor"] or 0 for p in pendentes), 2),
        "registros": len(perdas), "vencidos": len(vencidos),
    }


# ── Excel dos pendentes (para lançar no ERP) ──────────────────────────────────
def gerar_excel(perdas, titulo):
    """Tabela plana com autofiltro (padrão dos Excel de trabalho, seção 16)."""
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment
    wb = Workbook()
    ws = wb.active
    ws.title = "Perdas"
    ws["A1"] = titulo
    ws["A1"].font = Font(bold=True)
    cab = ["Data", "Motivo", "Código de barras", "Código ERP", "Produto", "Quantidade",
           "Custo unit.", "Valor (custo)", "Departamento", "Responsável", "Situação no ERP",
           "Referência", "Obs."]
    ws.append(cab)
    for c in ws[2]:
        c.font = Font(bold=True)
    for p in perdas:
        ws.append([date.fromisoformat(p["data"]), p["motivo_label"], p["codigo_barras"] or "",
                   p["codigo_interno"] or "", p["produto"], p["quantidade"], p["custo"],
                   p["valor"], p["dep_nome"], p["responsavel"] or "",
                   "lançada" if p["lanc_status"] == "lancada" else "pendente",
                   p["lanc_ref"] or "", p["obs"] or ""])
    n = len(perdas)
    ws.append(["TOTAL", "", "", "", "", None, None,
               f"=SUM(H3:H{n + 2})" if n else 0])
    ws[f"A{n + 3}"].font = Font(bold=True)
    for linha in ws.iter_rows(min_row=3, max_row=n + 3):
        linha[0].number_format = "DD/MM/YYYY"
        for i in (6, 7):
            linha[i].number_format = '"R$" #,##0.00'
        linha[2].alignment = Alignment(horizontal="left")
    larg = [11, 22, 16, 11, 40, 11, 12, 14, 22, 18, 14, 18, 30]
    for i, w in enumerate(larg):
        ws.column_dimensions[chr(65 + i)].width = w
    ws.auto_filter.ref = f"A2:M{n + 2}"
    ws.freeze_panes = "A3"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
