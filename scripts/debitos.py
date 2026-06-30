"""
scripts/debitos.py
Módulo de débitos e bonificações por empresa.
Persiste em dados/debitos.db (SQLite).
"""
import os
import uuid
import sqlite3
from datetime import datetime

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DADOS_DIR = os.path.join(BASE_DIR, "dados")
BANCO     = os.path.join(DADOS_DIR, "debitos.db")


def _conn():
    os.makedirs(DADOS_DIR, exist_ok=True)
    conn = sqlite3.connect(BANCO)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _init_schema(conn)
    return conn


def _init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS empresas (
            cnpj         TEXT PRIMARY KEY,
            razao_social TEXT NOT NULL,
            criado_em    TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS debitos (
            id          TEXT PRIMARY KEY,
            cnpj        TEXT NOT NULL,
            data        TEXT NOT NULL,
            tipo        TEXT NOT NULL,   -- 'vencimento' | 'rebaxa'
            nf_numero   TEXT,            -- só vencimento
            produto     TEXT,            -- só rebaxa
            quantidade  REAL,            -- só rebaxa
            valor_unit  REAL,            -- só rebaxa
            valor_total REAL NOT NULL,
            obs         TEXT,
            FOREIGN KEY (cnpj) REFERENCES empresas(cnpj) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS bonificacoes (
            id          TEXT PRIMARY KEY,
            cnpj        TEXT NOT NULL,
            data        TEXT NOT NULL,
            nf_numero   TEXT NOT NULL,
            valor_total REAL NOT NULL,
            obs         TEXT,
            FOREIGN KEY (cnpj) REFERENCES empresas(cnpj) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_deb_cnpj  ON debitos(cnpj);
        CREATE INDEX IF NOT EXISTS idx_bon_cnpj  ON bonificacoes(cnpj);
    """)
    conn.commit()


def _uid():
    return str(uuid.uuid4())[:8]


def _agora():
    return datetime.now().strftime("%d/%m/%Y %H:%M")


def _parse_valor(v):
    try:
        val = round(float(str(v).replace(",", ".")), 2)
        if val <= 0:
            raise ValueError
        return val
    except (TypeError, ValueError):
        return None


# ── Empresas ──────────────────────────────────────────────────────────────────

def listar_empresas():
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT cnpj, razao_social FROM empresas ORDER BY razao_social"
        ).fetchall()]
    finally:
        conn.close()


def buscar_empresa(cnpj):
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT cnpj, razao_social FROM empresas WHERE cnpj = ?", (cnpj,)
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def adicionar_empresa(cnpj, razao_social):
    cnpj = cnpj.strip()
    razao_social = razao_social.strip()
    if not cnpj or not razao_social:
        return False, "CNPJ e razão social são obrigatórios."
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO empresas (cnpj, razao_social, criado_em) VALUES (?, ?, ?)",
            (cnpj, razao_social, _agora()),
        )
        conn.commit()
        return True, "Empresa cadastrada."
    except sqlite3.IntegrityError:
        return False, "CNPJ já cadastrado."
    finally:
        conn.close()


def excluir_empresa(cnpj):
    conn = _conn()
    try:
        r = conn.execute("DELETE FROM empresas WHERE cnpj = ?", (cnpj,))
        conn.commit()
        return (True, "Empresa removida.") if r.rowcount else (False, "Empresa não encontrada.")
    finally:
        conn.close()


# ── Débitos ───────────────────────────────────────────────────────────────────

def listar_debitos(cnpj=None):
    conn = _conn()
    try:
        sql = "SELECT * FROM debitos"
        sql += " WHERE cnpj = ?" if cnpj else ""
        sql += " ORDER BY data DESC"
        rows = conn.execute(sql, (cnpj,) if cnpj else ()).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def adicionar_debito_vencimento(cnpj, nf_numero, valor_total, obs=""):
    if not buscar_empresa(cnpj):
        return False, "Empresa não encontrada."
    nf = nf_numero.strip()
    if not nf:
        return False, "Número da NF é obrigatório."
    valor = _parse_valor(valor_total)
    if valor is None:
        return False, "Valor inválido."
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO debitos (id, cnpj, data, tipo, nf_numero, valor_total, obs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_uid(), cnpj, _agora(), "vencimento", nf, valor, obs.strip()),
        )
        conn.commit()
        return True, f"Vencimento NF {nf} de R$ {valor:.2f} registrado."
    finally:
        conn.close()


def adicionar_debito_rebaxa(cnpj, produto, quantidade, valor_unit, obs=""):
    if not buscar_empresa(cnpj):
        return False, "Empresa não encontrada."
    produto = produto.strip()
    if not produto:
        return False, "Nome do produto é obrigatório."
    try:
        qtd   = float(str(quantidade).replace(",", "."))
        v_uni = float(str(valor_unit).replace(",", "."))
        if qtd <= 0 or v_uni <= 0:
            raise ValueError
        valor = round(qtd * v_uni, 2)
    except (TypeError, ValueError):
        return False, "Quantidade ou valor unitário inválido."
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO debitos (id, cnpj, data, tipo, produto, quantidade, valor_unit, valor_total, obs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_uid(), cnpj, _agora(), "rebaxa", produto, qtd, v_uni, valor, obs.strip()),
        )
        conn.commit()
        return True, f"Rebaxa de R$ {valor:.2f} registrada."
    finally:
        conn.close()


def excluir_debito(id_debito):
    conn = _conn()
    try:
        r = conn.execute("DELETE FROM debitos WHERE id = ?", (id_debito,))
        conn.commit()
        return (True, "Débito removido.") if r.rowcount else (False, "Débito não encontrado.")
    finally:
        conn.close()


# ── Bonificações ──────────────────────────────────────────────────────────────

def listar_bonificacoes(cnpj=None):
    conn = _conn()
    try:
        sql = "SELECT * FROM bonificacoes"
        sql += " WHERE cnpj = ?" if cnpj else ""
        sql += " ORDER BY data DESC"
        rows = conn.execute(sql, (cnpj,) if cnpj else ()).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def adicionar_bonificacao(cnpj, nf_numero, valor_total, obs=""):
    if not buscar_empresa(cnpj):
        return False, "Empresa não encontrada."
    nf = nf_numero.strip()
    if not nf:
        return False, "Número da NF é obrigatório."
    valor = _parse_valor(valor_total)
    if valor is None:
        return False, "Valor inválido."
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO bonificacoes (id, cnpj, data, nf_numero, valor_total, obs) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_uid(), cnpj, _agora(), nf, valor, obs.strip()),
        )
        conn.commit()
        return True, f"Bonificação NF {nf} de R$ {valor:.2f} registrada."
    finally:
        conn.close()


def excluir_bonificacao(id_bonif):
    conn = _conn()
    try:
        r = conn.execute("DELETE FROM bonificacoes WHERE id = ?", (id_bonif,))
        conn.commit()
        return (True, "Bonificação removida.") if r.rowcount else (False, "Bonificação não encontrada.")
    finally:
        conn.close()


# ── Saldo ─────────────────────────────────────────────────────────────────────

def calcular_saldo(cnpj):
    conn = _conn()
    try:
        total_deb = conn.execute(
            "SELECT COALESCE(SUM(valor_total), 0) FROM debitos WHERE cnpj = ?", (cnpj,)
        ).fetchone()[0]
        total_bon = conn.execute(
            "SELECT COALESCE(SUM(valor_total), 0) FROM bonificacoes WHERE cnpj = ?", (cnpj,)
        ).fetchone()[0]
        saldo = round(total_deb - total_bon, 2)
        return {
            "total_debito":      round(total_deb, 2),
            "total_bonificacao": round(total_bon, 2),
            "saldo_devedor":     saldo,
            "quitado":           saldo <= 0,
        }
    finally:
        conn.close()


def resumo_empresas():
    resultado = []
    for emp in listar_empresas():
        saldo = calcular_saldo(emp["cnpj"])
        resultado.append({**emp, **saldo})
    return resultado
