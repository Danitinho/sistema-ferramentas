"""
scripts/vencidos.py
===================
Registro interno de produtos vencidos / perdas.
Persiste em dados/vencidos.db (SQLite).

Nasce já no padrão de confiabilidade do sistema:
  • Datas em ISO (ordenação correta) + `data_fmt` para exibição.
  • Exclusão LÓGICA (soft-delete): nada some do banco.
  • Toda ação vai para a tabela `auditoria`, com autor (`usuario`).

Camada de lógica: NÃO importa Flask.
"""
import os
import re
import uuid
import sqlite3
from datetime import datetime, date

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DADOS_DIR = os.path.join(BASE_DIR, "dados")
BANCO     = os.path.join(DADOS_DIR, "vencidos.db")

MOTIVOS = ["vencido", "avaria", "quebra", "consumo interno", "outro"]


def _conn():
    os.makedirs(DADOS_DIR, exist_ok=True)
    conn = sqlite3.connect(BANCO)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS vencidos (
            id            TEXT PRIMARY KEY,
            data          TEXT NOT NULL,      -- data da ocorrência (AAAA-MM-DD)
            produto       TEXT NOT NULL,
            codigo_barras TEXT,
            quantidade    REAL NOT NULL,
            motivo        TEXT NOT NULL,
            valor_unit    REAL,
            valor_total   REAL NOT NULL DEFAULT 0,
            obs           TEXT,
            criado_em     TEXT NOT NULL,
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
        CREATE INDEX IF NOT EXISTS idx_venc_data   ON vencidos(data);
        CREATE INDEX IF NOT EXISTS idx_venc_barras ON vencidos(codigo_barras);
    """)
    conn.commit()
    return conn


# ── Utilitários ───────────────────────────────────────────────────────────────
def _uid():
    return str(uuid.uuid4())[:8]


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _hoje():
    return date.today().strftime("%Y-%m-%d")


_DATA_ISO = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
_DATA_BR  = re.compile(r'^(\d{2})/(\d{2})/(\d{4})$')


def _normaliza_data(s):
    """Aceita 'AAAA-MM-DD' ou 'DD/MM/AAAA' e devolve ISO; None se vazio/ inválido."""
    if not s:
        return None
    s = s.strip()
    if _DATA_ISO.match(s):
        return s
    m = _DATA_BR.match(s)
    if m:
        d, mo, y = m.groups()
        return f"{y}-{mo}-{d}"
    return None


def _fmt_data(s):
    if not s:
        return ""
    m = _DATA_ISO.match(s)
    if m:
        y, mo, d = m.groups()
        return f"{d}/{mo}/{y}"
    return s


def _parse_qtd(v):
    try:
        q = float(str(v).replace(",", "."))
        return q if q > 0 else None
    except (TypeError, ValueError):
        return None


def _parse_valor_opcional(v):
    if v in (None, "", "0", 0):
        return None
    try:
        val = round(float(str(v).replace(",", ".")), 2)
        return val if val > 0 else None
    except (TypeError, ValueError):
        return None


def _auditar(conn, entidade_id, acao, detalhe="", usuario=None):
    conn.execute(
        "INSERT INTO auditoria (quando, usuario, entidade, entidade_id, acao, detalhe) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_agora(), usuario, "vencido", entidade_id, acao, detalhe),
    )


# ── Operações ─────────────────────────────────────────────────────────────────
def registrar(produto, quantidade, motivo, codigo_barras="", valor_unit=None,
              data=None, obs="", usuario=None):
    produto = (produto or "").strip()
    if not produto:
        return False, "Informe o produto."
    qtd = _parse_qtd(quantidade)
    if qtd is None:
        return False, "Quantidade inválida."
    motivo = (motivo or "").strip().lower()
    if motivo not in MOTIVOS:
        return False, "Motivo inválido."
    data_iso = _normaliza_data(data) or _hoje()
    vu = _parse_valor_opcional(valor_unit)
    valor_total = round(qtd * vu, 2) if vu else 0.0

    conn = _conn()
    try:
        vid = _uid()
        conn.execute(
            "INSERT INTO vencidos (id, data, produto, codigo_barras, quantidade, motivo, "
            "valor_unit, valor_total, obs, criado_em) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (vid, data_iso, produto, (codigo_barras or "").strip(), qtd, motivo,
             vu, valor_total, (obs or "").strip(), _agora()),
        )
        _auditar(conn, vid, "criar",
                 f"{produto} · {qtd:g} · {motivo}"
                 + (f" · R$ {valor_total:.2f}" if valor_total else ""), usuario)
        conn.commit()
        return True, f"Registro de '{produto}' salvo."
    finally:
        conn.close()


def listar(inicio=None, fim=None, motivo=None, limite=500):
    ini = _normaliza_data(inicio)
    fim_ = _normaliza_data(fim)
    conn = _conn()
    try:
        sql = "SELECT * FROM vencidos WHERE excluido_em IS NULL"
        params = []
        if ini:
            sql += " AND data >= ?"; params.append(ini)
        if fim_:
            sql += " AND data <= ?"; params.append(fim_)
        if motivo and motivo in MOTIVOS:
            sql += " AND motivo = ?"; params.append(motivo)
        sql += " ORDER BY data DESC, criado_em DESC LIMIT ?"
        params.append(limite)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for v in rows:
            v["data_fmt"] = _fmt_data(v["data"])
        return rows
    finally:
        conn.close()


def excluir(id_registro, usuario=None):
    conn = _conn()
    try:
        r = conn.execute(
            "UPDATE vencidos SET excluido_em = ?, excluido_por = ? "
            "WHERE id = ? AND excluido_em IS NULL", (_agora(), usuario, id_registro))
        if r.rowcount:
            _auditar(conn, id_registro, "excluir", "", usuario)
            conn.commit()
            return True, "Registro removido."
        return False, "Registro não encontrado."
    finally:
        conn.close()


def resumo(inicio=None, fim=None, motivo=None):
    itens = listar(inicio, fim, motivo, limite=100000)
    total_valor = round(sum(v["valor_total"] or 0 for v in itens), 2)
    total_qtd   = round(sum(v["quantidade"] or 0 for v in itens), 3)
    por_motivo = {}
    for v in itens:
        m = por_motivo.setdefault(v["motivo"], {"itens": 0, "quantidade": 0.0, "valor": 0.0})
        m["itens"] += 1
        m["quantidade"] = round(m["quantidade"] + (v["quantidade"] or 0), 3)
        m["valor"] = round(m["valor"] + (v["valor_total"] or 0), 2)
    return {
        "total_registros": len(itens),
        "total_quantidade": total_qtd,
        "total_valor": total_valor,
        "por_motivo": por_motivo,
    }
