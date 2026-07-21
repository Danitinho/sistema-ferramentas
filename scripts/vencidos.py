"""
scripts/vencidos.py
===================
Controle de vencidos em dois estágios + baixa.

Fluxo do negócio:
  1. AVISO  — antes de vencer, o responsável pela seção avisa (≥30 dias) e o
     cadastro registra: produto, código de barras, quantidade, fornecedor,
     responsável (quem avisou), data de vencimento, custo, venda e valor
     promocional (se for pra promoção).
  2. VENCIDO — a mercadoria vencida chega ao escritório e o cadastro registra o
     básico: produto, código, quantidade, fornecedor, custo, responsável pela
     entrega e SE foi avisado (o sistema detecta sozinho pelo código de barras).
  3. BAIXA  — botão que confirma a baixa no sistema, como perda (nota de perda)
     ou devolução (fornecedor + nº da NF).

O sistema registra data/hora e autor de cada ação, cruza aviso × vencido
automaticamente e vigia a regra dos 30 dias (sinaliza, não bloqueia).

Persiste em dados/vencidos.db (SQLite). Camada de lógica: NÃO importa Flask.
"""
import os
import re
import uuid
import sqlite3
from datetime import datetime, date, timedelta

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DADOS_DIR = os.path.join(BASE_DIR, "dados")
BANCO     = os.path.join(DADOS_DIR, "vencidos.db")

_EPS = 0.005
DIAS_MINIMO = 30  # antecedência mínima do aviso

TIPOS_BAIXA = {"perda": "Perda", "devolucao": "Devolução ao fornecedor"}

MESES_PT = ["", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
            "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


def rotulo_mes(mes):
    """'2026-07' → 'julho/2026'."""
    try:
        y, mo = mes.split("-")
        return f"{MESES_PT[int(mo)]}/{y}"
    except (ValueError, IndexError):
        return mes


def _conn():
    os.makedirs(DADOS_DIR, exist_ok=True)
    conn = sqlite3.connect(BANCO)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    _init_schema(conn)
    return conn


def _init_schema(conn):
    # Legado: a versão anterior do módulo usava uma tabela `vencidos` com coluna
    # `motivo`. Se existir e estiver vazia, recria no esquema novo.
    cols = {r[1] for r in conn.execute("PRAGMA table_info(vencidos)")}
    if cols and "motivo" in cols and "baixa_status" not in cols:
        if conn.execute("SELECT COUNT(*) FROM vencidos").fetchone()[0] == 0:
            conn.execute("DROP TABLE vencidos")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS avisos (
            id            TEXT PRIMARY KEY,
            produto       TEXT NOT NULL,
            codigo_barras TEXT,
            quantidade    REAL NOT NULL,
            fornecedor    TEXT,
            responsavel   TEXT,               -- quem avisou (responsável da seção)
            data_vencimento TEXT NOT NULL,     -- AAAA-MM-DD
            custo         REAL,
            venda         REAL,
            valor_promocional REAL,
            obs           TEXT,
            registrado_por TEXT,
            criado_em     TEXT NOT NULL,
            resolvido_em  TEXT,
            resolvido_vencido_id TEXT,
            excluido_em   TEXT,
            excluido_por  TEXT
        );
        CREATE TABLE IF NOT EXISTS vencidos (
            id            TEXT PRIMARY KEY,
            produto       TEXT NOT NULL,
            codigo_barras TEXT,
            quantidade    REAL NOT NULL,
            fornecedor    TEXT,
            custo         REAL,
            responsavel_entrega TEXT,          -- funcionário que trouxe
            foi_avisado   INTEGER NOT NULL DEFAULT 0,
            aviso_id      TEXT,
            obs           TEXT,
            registrado_por TEXT,
            criado_em     TEXT NOT NULL,
            baixa_status  TEXT NOT NULL DEFAULT 'pendente',   -- pendente | baixado
            baixa_tipo    TEXT,                -- perda | devolucao
            baixa_ref     TEXT,                -- nº nota de perda / nº NF devolução
            baixa_em      TEXT,
            baixa_por     TEXT,
            excluido_em   TEXT,
            excluido_por  TEXT
        );
        CREATE TABLE IF NOT EXISTS auditoria (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            quando      TEXT NOT NULL,
            usuario     TEXT,
            entidade    TEXT NOT NULL,
            entidade_id TEXT,
            acao        TEXT NOT NULL,
            detalhe     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_aviso_barras ON avisos(codigo_barras);
        CREATE INDEX IF NOT EXISTS idx_aviso_venc   ON avisos(data_vencimento);
        CREATE INDEX IF NOT EXISTS idx_venc_barras  ON vencidos(codigo_barras);
        CREATE INDEX IF NOT EXISTS idx_venc_criado  ON vencidos(criado_em);
    """)
    conn.commit()


# ── Utilitários ───────────────────────────────────────────────────────────────
def _uid():
    return str(uuid.uuid4())[:8]


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hoje():
    return date.today().strftime("%Y-%m-%d")


_DATA_ISO = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
_DATA_BR  = re.compile(r'^(\d{2})/(\d{2})/(\d{4})$')


def _norm_dia(s):
    if not s:
        return None
    s = str(s).strip()
    if _DATA_ISO.match(s):
        return s
    m = _DATA_BR.match(s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo}-{d}"
    return None


def _fmt_dia(s):
    m = _DATA_ISO.match(s or "")
    if not m:
        return s or ""
    y, mo, d = m.groups()
    return f"{d}/{mo}/{y}"


def _fmt_dt(s):
    """'2026-07-20 14:30:00' → '20/07/2026 14:30'."""
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})', s or "")
    if not m:
        return s or ""
    y, mo, d, h, mi = m.groups()
    return f"{d}/{mo}/{y} {h}:{mi}"


def _limites_mes(mes):
    """'2026-07' → ('2026-07-01', '2026-08-01') para range indexado (>= ini AND < fim)."""
    y, mo = int(mes[:4]), int(mes[5:7])
    ini = f"{y:04d}-{mo:02d}-01"
    fy, fm = (y + 1, 1) if mo == 12 else (y, mo + 1)
    return ini, f"{fy:04d}-{fm:02d}-01"


def _dias_ate(data_iso):
    try:
        return (date.fromisoformat(data_iso) - date.today()).days
    except (TypeError, ValueError):
        return None


def _dias_entre(inicio_iso, fim_iso):
    try:
        return (date.fromisoformat(fim_iso) - date.fromisoformat((inicio_iso or "")[:10])).days
    except (TypeError, ValueError):
        return None


def _parse_qtd(v):
    try:
        q = float(str(v).replace(",", "."))
        return q if q > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_valor_opcional(v):
    if v in (None, "", "N", "n"):
        return None
    try:
        val = round(float(str(v).replace(",", ".")), 2)
        return val if val >= 0 else None
    except (TypeError, ValueError):
        return None


def _auditar(conn, entidade, entidade_id, acao, detalhe="", usuario=None):
    conn.execute(
        "INSERT INTO auditoria (quando, usuario, entidade, entidade_id, acao, detalhe) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_agora(), usuario, entidade, str(entidade_id) if entidade_id else None, acao, detalhe),
    )


# ── Enriquecimento (campos derivados) ─────────────────────────────────────────
def _enriquecer_aviso(a):
    dias_venc = _dias_ate(a["data_vencimento"])
    dias_antec = _dias_entre(a["criado_em"], a["data_vencimento"])
    if a["resolvido_em"]:
        status = "resolvido"
    elif dias_venc is None:
        status = "no_prazo"
    elif dias_venc < 0:
        status = "vencido"
    elif dias_venc <= 30:
        status = "vence_breve"
    else:
        status = "no_prazo"
    a["dias_para_vencer"] = dias_venc
    a["dias_antecedencia"] = dias_antec
    a["no_prazo"] = (dias_antec is not None and dias_antec >= DIAS_MINIMO)
    a["status"] = status
    a["promocao"] = a["valor_promocional"] is not None
    a["data_venc_fmt"] = _fmt_dia(a["data_vencimento"])
    a["criado_fmt"] = _fmt_dt(a["criado_em"])
    return a


def _enriquecer_vencido(v):
    v["foi_avisado"] = bool(v["foi_avisado"])
    v["valor_perdido"] = round((v["quantidade"] or 0) * (v["custo"] or 0), 2)
    v["criado_fmt"] = _fmt_dt(v["criado_em"])
    v["baixa_fmt"] = _fmt_dt(v["baixa_em"]) if v["baixa_em"] else ""
    v["baixa_tipo_label"] = TIPOS_BAIXA.get(v["baixa_tipo"], v["baixa_tipo"] or "")
    return v


# ── Avisos ────────────────────────────────────────────────────────────────────
def registrar_aviso(produto, codigo_barras, quantidade, fornecedor, responsavel,
                    data_vencimento, custo=None, venda=None, valor_promocional=None,
                    obs="", usuario=None):
    produto = (produto or "").strip()
    if not produto:
        return False, "Informe o produto."
    qtd = _parse_qtd(quantidade)
    if qtd is None:
        return False, "Quantidade inválida."
    dv = _norm_dia(data_vencimento)
    if not dv:
        return False, "Informe a data de vencimento."
    conn = _conn()
    try:
        aid = _uid()
        conn.execute(
            "INSERT INTO avisos (id, produto, codigo_barras, quantidade, fornecedor, "
            "responsavel, data_vencimento, custo, venda, valor_promocional, obs, "
            "registrado_por, criado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, produto, (codigo_barras or "").strip(), qtd, (fornecedor or "").strip(),
             (responsavel or "").strip(), dv, _parse_valor_opcional(custo),
             _parse_valor_opcional(venda), _parse_valor_opcional(valor_promocional),
             (obs or "").strip(), usuario, _agora()),
        )
        _auditar(conn, "aviso", aid, "criar", f"{produto} · vence {_fmt_dia(dv)}", usuario)
        conn.commit()
        dias = _dias_entre(_hoje(), dv)
        aviso_atraso = dias is not None and dias < DIAS_MINIMO
        msg = f"Aviso de '{produto}' registrado."
        if aviso_atraso:
            msg += f" Atenção: avisado com {dias} dia(s) — abaixo dos {DIAS_MINIMO} do prazo."
        return True, msg
    finally:
        conn.close()


def meses_disponiveis():
    """União dos meses presentes: registro dos vencidos (criado_em) e vencimento
    dos avisos (data_vencimento), do mais recente ao mais antigo."""
    conn = _conn()
    try:
        vm = [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(criado_em,1,7) FROM vencidos WHERE excluido_em IS NULL")]
        am = [r[0] for r in conn.execute(
            "SELECT DISTINCT substr(data_vencimento,1,7) FROM avisos WHERE excluido_em IS NULL")]
    finally:
        conn.close()
    meses = sorted({m for m in vm + am if m}, reverse=True)
    return [{"mes": m, "rotulo": rotulo_mes(m)} for m in meses]


def listar_avisos(mes=None, status=None, busca=None, incluir_resolvidos=True, limite=5000):
    # avisos paginam pelo mês de VENCIMENTO (filtro por range, no SQL)
    conn = _conn()
    try:
        sql = "SELECT * FROM avisos WHERE excluido_em IS NULL"
        params = []
        if mes:
            ini, fim = _limites_mes(mes)
            sql += " AND data_vencimento >= ? AND data_vencimento < ?"
            params += [ini, fim]
        sql += " ORDER BY data_vencimento ASC, criado_em DESC"
        if limite:
            sql += f" LIMIT {int(limite)}"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    saida = []
    for a in rows:
        _enriquecer_aviso(a)
        if not incluir_resolvidos and a["status"] == "resolvido":
            continue
        if status and a["status"] != status:
            continue
        if busca:
            alvo = f"{a['produto']} {a['codigo_barras']} {a['fornecedor']} {a['responsavel']}".lower()
            if busca.lower() not in alvo:
                continue
        saida.append(a)
    return saida


def excluir_aviso(id_aviso, usuario=None):
    conn = _conn()
    try:
        r = conn.execute("UPDATE avisos SET excluido_em=?, excluido_por=? "
                         "WHERE id=? AND excluido_em IS NULL", (_agora(), usuario, id_aviso))
        if r.rowcount:
            _auditar(conn, "aviso", id_aviso, "excluir", "", usuario)
            conn.commit()
            return True, "Aviso removido."
        return False, "Aviso não encontrado."
    finally:
        conn.close()


def buscar_aviso_ativo_por_barras(codigo_barras):
    """Aviso ativo (não resolvido, não excluído) do mesmo código de barras, mais
    próximo de vencer. Retorna dict enriquecido ou None."""
    cb = (codigo_barras or "").strip()
    if not cb:
        return None
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT * FROM avisos WHERE codigo_barras = ? AND excluido_em IS NULL "
            "AND resolvido_em IS NULL ORDER BY data_vencimento ASC LIMIT 1", (cb,)).fetchone()
    finally:
        conn.close()
    return _enriquecer_aviso(dict(r)) if r else None


def checar_aviso(codigo_barras):
    """Para a checagem ao vivo no formulário do vencido."""
    a = buscar_aviso_ativo_por_barras(codigo_barras)
    if not a:
        return {"avisado": False}
    return {
        "avisado": True, "aviso_id": a["id"], "produto": a["produto"],
        "data_venc_fmt": a["data_venc_fmt"], "dias_antecedencia": a["dias_antecedencia"],
        "no_prazo": a["no_prazo"], "responsavel": a["responsavel"],
        "quantidade": a["quantidade"], "fornecedor": a["fornecedor"], "custo": a["custo"],
    }


# ── Vencidos ──────────────────────────────────────────────────────────────────
def registrar_vencido(produto, codigo_barras, quantidade, fornecedor, custo,
                      responsavel_entrega, obs="", usuario=None):
    produto = (produto or "").strip()
    if not produto:
        return False, "Informe o produto."
    qtd = _parse_qtd(quantidade)
    if qtd is None:
        return False, "Quantidade inválida."
    cb = (codigo_barras or "").strip()
    aviso = buscar_aviso_ativo_por_barras(cb)
    foi_avisado = 1 if aviso else 0
    conn = _conn()
    try:
        vid = _uid()
        conn.execute(
            "INSERT INTO vencidos (id, produto, codigo_barras, quantidade, fornecedor, "
            "custo, responsavel_entrega, foi_avisado, aviso_id, obs, registrado_por, "
            "criado_em, baixa_status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pendente')",
            (vid, produto, cb, qtd, (fornecedor or "").strip(),
             _parse_valor_opcional(custo), (responsavel_entrega or "").strip(),
             foi_avisado, aviso["id"] if aviso else None, (obs or "").strip(),
             usuario, _agora()),
        )
        # resolve o aviso correspondente (vincula e tira da vigília)
        if aviso:
            conn.execute("UPDATE avisos SET resolvido_em=?, resolvido_vencido_id=? WHERE id=?",
                         (_agora(), vid, aviso["id"]))
            _auditar(conn, "aviso", aviso["id"], "resolver", f"vencido {vid}", usuario)
        _auditar(conn, "vencido", vid, "criar",
                 f"{produto} · {qtd:g} un · {'avisado' if foi_avisado else 'SEM aviso'}", usuario)
        conn.commit()
        if foi_avisado:
            return True, f"Vencido '{produto}' registrado — avisado em {aviso['data_venc_fmt']}."
        return True, f"Vencido '{produto}' registrado — SEM aviso prévio."
    finally:
        conn.close()


def editar_vencido(id_vencido, produto, quantidade, codigo_barras=None, fornecedor=None,
                   custo=None, responsavel_entrega=None, foi_avisado=None, obs=None, usuario=None):
    """Corrige um vencido — inclusive o 'foi avisado?' manualmente. Só permitido
    se NÃO estiver baixado (o usuário deve reabrir a baixa antes de editar)."""
    produto = (produto or "").strip()
    if not produto:
        return False, "Informe o produto."
    qtd = _parse_qtd(quantidade)
    if qtd is None:
        return False, "Quantidade inválida."
    fa = 1 if str(foi_avisado).strip().lower() in ("1", "true", "sim", "s") else 0
    conn = _conn()
    try:
        v = conn.execute("SELECT * FROM vencidos WHERE id = ? AND excluido_em IS NULL",
                         (id_vencido,)).fetchone()
        if not v:
            return False, "Vencido não encontrado."
        if v["baixa_status"] == "baixado":
            return False, "Este vencido está baixado. Reabra a baixa para poder editar."
        novo_aviso_id = v["aviso_id"]
        # se passou a 'não avisado' e havia aviso vinculado, devolve o aviso à vigília
        if fa == 0 and v["aviso_id"]:
            conn.execute("UPDATE avisos SET resolvido_em = NULL, resolvido_vencido_id = NULL WHERE id = ?",
                         (v["aviso_id"],))
            _auditar(conn, "aviso", v["aviso_id"], "reabrir", "vencido desvinculado na edição", usuario)
            novo_aviso_id = None
        conn.execute(
            "UPDATE vencidos SET produto=?, codigo_barras=?, quantidade=?, fornecedor=?, "
            "custo=?, responsavel_entrega=?, foi_avisado=?, aviso_id=?, obs=? WHERE id=?",
            (produto, (codigo_barras or "").strip(), qtd, (fornecedor or "").strip(),
             _parse_valor_opcional(custo), (responsavel_entrega or "").strip(),
             fa, novo_aviso_id, (obs or "").strip(), id_vencido))
        _auditar(conn, "vencido", id_vencido, "editar",
                 f"{produto} · {qtd:g} un · {'avisado' if fa else 'sem aviso'}", usuario)
        conn.commit()
        return True, "Vencido atualizado."
    finally:
        conn.close()


def listar_vencidos(mes=None, baixa=None, avisado=None, busca=None, limite=5000):
    # vencidos paginam pelo mês de REGISTRO (criado_em) — filtro por range, no SQL
    conn = _conn()
    try:
        sql = "SELECT * FROM vencidos WHERE excluido_em IS NULL"
        params = []
        if mes:
            ini, fim = _limites_mes(mes)
            sql += " AND criado_em >= ? AND criado_em < ?"
            params += [ini, fim]
        sql += " ORDER BY (baixa_status='baixado'), criado_em DESC"
        if limite:
            sql += f" LIMIT {int(limite)}"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    saida = []
    for v in rows:
        _enriquecer_vencido(v)
        if baixa and v["baixa_status"] != baixa:
            continue
        if avisado == "sim" and not v["foi_avisado"]:
            continue
        if avisado == "nao" and v["foi_avisado"]:
            continue
        if busca:
            alvo = f"{v['produto']} {v['codigo_barras']} {v['fornecedor']} {v['responsavel_entrega']}".lower()
            if busca.lower() not in alvo:
                continue
        saida.append(v)
    return saida


def dar_baixa(id_vencido, tipo, referencia="", usuario=None):
    if tipo not in TIPOS_BAIXA:
        return False, "Tipo de baixa inválido."
    conn = _conn()
    try:
        v = conn.execute("SELECT * FROM vencidos WHERE id=? AND excluido_em IS NULL",
                         (id_vencido,)).fetchone()
        if not v:
            return False, "Vencido não encontrado."
        if v["baixa_status"] == "baixado":
            return False, "Este vencido já teve baixa."
        conn.execute(
            "UPDATE vencidos SET baixa_status='baixado', baixa_tipo=?, baixa_ref=?, "
            "baixa_em=?, baixa_por=? WHERE id=?",
            (tipo, (referencia or "").strip(), _agora(), usuario, id_vencido))
        _auditar(conn, "vencido", id_vencido, "baixa",
                 f"{TIPOS_BAIXA[tipo]}" + (f" · {referencia}" if referencia else ""), usuario)
        conn.commit()
        return True, "Baixa confirmada."
    finally:
        conn.close()


def reabrir_baixa(id_vencido, usuario=None):
    conn = _conn()
    try:
        r = conn.execute(
            "UPDATE vencidos SET baixa_status='pendente', baixa_tipo=NULL, baixa_ref=NULL, "
            "baixa_em=NULL, baixa_por=NULL WHERE id=? AND excluido_em IS NULL AND baixa_status='baixado'",
            (id_vencido,))
        if r.rowcount:
            _auditar(conn, "vencido", id_vencido, "reabrir", "", usuario)
            conn.commit()
            return True, "Baixa reaberta."
        return False, "Vencido não encontrado ou sem baixa."
    finally:
        conn.close()


def excluir_vencido(id_vencido, usuario=None):
    conn = _conn()
    try:
        r = conn.execute("UPDATE vencidos SET excluido_em=?, excluido_por=? "
                         "WHERE id=? AND excluido_em IS NULL", (_agora(), usuario, id_vencido))
        if r.rowcount:
            _auditar(conn, "vencido", id_vencido, "excluir", "", usuario)
            conn.commit()
            return True, "Vencido removido."
        return False, "Vencido não encontrado."
    finally:
        conn.close()


# ── Painel ────────────────────────────────────────────────────────────────────
def resumo(mes=None):
    """Painel. Se `mes` (AAAA-MM): vencidos por mês de registro; o card de avisos
    passa a contar avisos que VENCEM naquele mês. Sem mês: visão geral, com o
    card de avisos no sentido 'vencendo em ≤30 dias' a partir de hoje."""
    agg = ("SELECT COUNT(*), "
           "COALESCE(SUM(quantidade * COALESCE(custo,0)),0), "
           "COALESCE(SUM(foi_avisado),0), "
           "COALESCE(SUM(CASE WHEN baixa_status <> 'baixado' THEN 1 ELSE 0 END),0) "
           "FROM vencidos WHERE excluido_em IS NULL")
    conn = _conn()
    try:
        if mes:
            ini, fim = _limites_mes(mes)
            row = conn.execute(agg + " AND criado_em >= ? AND criado_em < ?", (ini, fim)).fetchone()
            avisos_vencendo = conn.execute(
                "SELECT COUNT(*) FROM avisos WHERE excluido_em IS NULL AND resolvido_em IS NULL "
                "AND data_vencimento >= ? AND data_vencimento < ?", (ini, fim)).fetchone()[0]
        else:
            row = conn.execute(agg).fetchone()
            # 'vencendo em ≤30 dias' (inclui já vencidos ainda pendentes)
            limite30 = (date.today() + timedelta(days=30)).isoformat()
            avisos_vencendo = conn.execute(
                "SELECT COUNT(*) FROM avisos WHERE excluido_em IS NULL AND resolvido_em IS NULL "
                "AND data_vencimento <= ?", (limite30,)).fetchone()[0]
    finally:
        conn.close()

    total_v, valor, avisados, pendentes_baixa = row[0], round(row[1], 2), row[2], row[3]
    pct_avisado = round(avisados * 100 / total_v) if total_v else 0
    return {
        "total_vencidos": total_v,
        "valor_perdido": valor,
        "avisados": avisados,
        "pct_avisado": pct_avisado,
        "pendentes_baixa": pendentes_baixa,
        "avisos_vencendo": avisos_vencendo,
        "mes": mes,
    }
