"""
scripts/debitos.py
Módulo de débitos, pagamentos e alocações por empresa.
Persiste em dados/debitos.db (SQLite).

Modelo de dados (acompanhamento de pagamento NF-a-NF):
  • debitos      — o que a empresa nos deve (vencimento por NF ou rebaxa de
                   preço). Cada débito acumula `valor_pago`; o status
                   (aberto/parcial/quitado) é derivado de valor_pago × valor_total.
  • pagamentos   — créditos a nosso favor (NF de bonificação etc.). Acumulam
                   `valor_alocado`; o que sobra é o crédito disponível.
  • alocacoes    — ligação N:N: "R$ X do pagamento P quitou o débito D". É ela
                   que permite ver, por débito, quanto ainda falta, e por
                   pagamento, quais débitos ele cobriu.

Confiabilidade:
  • Datas em ISO (ordenação correta) + `data_fmt` para exibição.
  • Exclusão LÓGICA (soft-delete): nada some do banco; excluir um débito ou
    pagamento reverte automaticamente suas alocações.
  • Toda ação vai para a tabela `auditoria`, com autor (`usuario`).
  • NF duplicada na mesma empresa é barrada (evita saldo falso).
"""
import os
import re
import uuid
import sqlite3
from datetime import datetime

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DADOS_DIR = os.path.join(BASE_DIR, "dados")
BANCO     = os.path.join(DADOS_DIR, "debitos.db")

_EPS = 0.005  # tolerância p/ comparação de centavos


def _conn():
    os.makedirs(DADOS_DIR, exist_ok=True)
    conn = sqlite3.connect(BANCO)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    _init_schema(conn)
    return conn


def _init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS empresas (
            cnpj         TEXT PRIMARY KEY,
            razao_social TEXT NOT NULL,
            criado_em    TEXT NOT NULL,
            excluido_em  TEXT,
            excluido_por TEXT
        );
        CREATE TABLE IF NOT EXISTS debitos (
            id          TEXT PRIMARY KEY,
            cnpj        TEXT NOT NULL,
            data        TEXT NOT NULL,
            tipo        TEXT NOT NULL,   -- 'vencimento' | 'rebaxa'
            nf_numero   TEXT,
            produto     TEXT,
            quantidade  REAL,
            valor_unit  REAL,
            valor_total REAL NOT NULL,
            valor_pago  REAL NOT NULL DEFAULT 0,
            obs         TEXT,
            excluido_em  TEXT,
            excluido_por TEXT,
            FOREIGN KEY (cnpj) REFERENCES empresas(cnpj) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS pagamentos (
            id            TEXT PRIMARY KEY,
            cnpj          TEXT NOT NULL,
            data          TEXT NOT NULL,
            nf_numero     TEXT NOT NULL,
            valor_total   REAL NOT NULL,
            valor_alocado REAL NOT NULL DEFAULT 0,
            obs           TEXT,
            excluido_em   TEXT,
            excluido_por  TEXT,
            FOREIGN KEY (cnpj) REFERENCES empresas(cnpj) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS alocacoes (
            id           TEXT PRIMARY KEY,
            pagamento_id TEXT NOT NULL,
            debito_id    TEXT NOT NULL,
            valor        REAL NOT NULL,
            criado_em    TEXT NOT NULL,
            criado_por   TEXT,
            excluido_em  TEXT,
            excluido_por TEXT
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
        CREATE INDEX IF NOT EXISTS idx_deb_cnpj    ON debitos(cnpj);
        CREATE INDEX IF NOT EXISTS idx_pag_cnpj    ON pagamentos(cnpj);
        CREATE INDEX IF NOT EXISTS idx_aloc_deb    ON alocacoes(debito_id);
        CREATE INDEX IF NOT EXISTS idx_aloc_pag    ON alocacoes(pagamento_id);
        CREATE INDEX IF NOT EXISTS idx_audit_ent   ON auditoria(entidade, entidade_id);
    """)
    conn.commit()
    _migrar(conn)


def _tabelas(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _colunas(conn, tabela):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({tabela})")}


def _migrar(conn):
    """Evolui bancos de versões anteriores para o modelo atual, sem destruir dado."""
    # colunas de soft-delete
    for tabela in ("empresas", "debitos", "pagamentos"):
        cols = _colunas(conn, tabela)
        if "excluido_em" not in cols:
            conn.execute(f"ALTER TABLE {tabela} ADD COLUMN excluido_em TEXT")
        if "excluido_por" not in cols:
            conn.execute(f"ALTER TABLE {tabela} ADD COLUMN excluido_por TEXT")
    if "valor_pago" not in _colunas(conn, "debitos"):
        conn.execute("ALTER TABLE debitos ADD COLUMN valor_pago REAL NOT NULL DEFAULT 0")

    # migra a tabela antiga bonificacoes -> pagamentos (idempotente por PK).
    # A bonificacoes pode ser de um schema anterior, sem as colunas de soft-delete.
    if "bonificacoes" in _tabelas(conn):
        bcols   = _colunas(conn, "bonificacoes")
        exc_em  = "excluido_em"  if "excluido_em"  in bcols else "NULL"
        exc_por = "excluido_por" if "excluido_por" in bcols else "NULL"
        conn.execute(f"""
            INSERT OR IGNORE INTO pagamentos
                (id, cnpj, data, nf_numero, valor_total, valor_alocado, obs,
                 excluido_em, excluido_por)
            SELECT id, cnpj, data, nf_numero, valor_total, 0, obs,
                   {exc_em}, {exc_por}
            FROM bonificacoes
        """)

    # datas br -> ISO
    for tabela, col in (("empresas", "criado_em"),
                        ("debitos", "data"),
                        ("pagamentos", "data")):
        tem_br = conn.execute(
            f"SELECT COUNT(*) FROM {tabela} WHERE {col} LIKE '__/__/____%'"
        ).fetchone()[0]
        if not tem_br:
            continue
        for row in conn.execute(f"SELECT rowid AS rid, {col} AS v FROM {tabela}").fetchall():
            novo = _iso_de_br(row["v"])
            if novo != row["v"]:
                conn.execute(f"UPDATE {tabela} SET {col}=? WHERE rowid=?", (novo, row["rid"]))
    conn.commit()


# ── Utilitários ───────────────────────────────────────────────────────────────
def _uid():
    return str(uuid.uuid4())[:8]


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


_BR_RE  = re.compile(r'^(\d{2})/(\d{2})/(\d{4})[ T](\d{2}):(\d{2})(?::(\d{2}))?$')
_ISO_RE = re.compile(r'^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})')


def _iso_de_br(s):
    if not s:
        return s
    m = _BR_RE.match(s.strip())
    if not m:
        return s
    d, mo, y, h, mi, se = m.groups()
    return f"{y}-{mo}-{d} {h}:{mi}:{se or '00'}"


def _fmt_data(s):
    if not s:
        return ""
    m = _ISO_RE.match(s)
    if not m:
        return s
    y, mo, d, h, mi = m.groups()
    return f"{d}/{mo}/{y} {h}:{mi}"


def _parse_valor(v):
    try:
        val = round(float(str(v).replace(",", ".")), 2)
        if val <= 0:
            raise ValueError
        return val
    except (TypeError, ValueError):
        return None


def _status(valor_total, valor_pago):
    if valor_pago <= _EPS:
        return "aberto"
    if valor_pago + _EPS < valor_total:
        return "parcial"
    return "quitado"


def _auditar(conn, entidade, entidade_id, acao, detalhe="", usuario=None):
    conn.execute(
        "INSERT INTO auditoria (quando, usuario, entidade, entidade_id, acao, detalhe) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_agora(), usuario, entidade,
         str(entidade_id) if entidade_id is not None else None, acao, detalhe),
    )


def _nf_duplicada(conn, tabela, cnpj, nf):
    return conn.execute(
        f"SELECT 1 FROM {tabela} WHERE cnpj = ? AND nf_numero = ? AND excluido_em IS NULL",
        (cnpj, nf),
    ).fetchone() is not None


# ── Empresas ──────────────────────────────────────────────────────────────────
def listar_empresas():
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT cnpj, razao_social FROM empresas "
            "WHERE excluido_em IS NULL ORDER BY razao_social"
        ).fetchall()]
    finally:
        conn.close()


def buscar_empresa(cnpj):
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT cnpj, razao_social FROM empresas "
            "WHERE cnpj = ? AND excluido_em IS NULL", (cnpj,)
        ).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def adicionar_empresa(cnpj, razao_social, usuario=None):
    cnpj = cnpj.strip()
    razao_social = razao_social.strip()
    if not cnpj or not razao_social:
        return False, "CNPJ e razão social são obrigatórios."
    conn = _conn()
    try:
        row = conn.execute("SELECT excluido_em FROM empresas WHERE cnpj = ?", (cnpj,)).fetchone()
        if row is not None:
            if row["excluido_em"] is None:
                return False, "CNPJ já cadastrado."
            conn.execute(
                "UPDATE empresas SET razao_social = ?, excluido_em = NULL, "
                "excluido_por = NULL WHERE cnpj = ?", (razao_social, cnpj))
            _auditar(conn, "empresa", cnpj, "reativar", razao_social, usuario)
            conn.commit()
            return True, "Empresa reativada."
        conn.execute(
            "INSERT INTO empresas (cnpj, razao_social, criado_em) VALUES (?, ?, ?)",
            (cnpj, razao_social, _agora()))
        _auditar(conn, "empresa", cnpj, "criar", razao_social, usuario)
        conn.commit()
        return True, "Empresa cadastrada."
    finally:
        conn.close()


def excluir_empresa(cnpj, usuario=None):
    conn = _conn()
    try:
        r = conn.execute(
            "UPDATE empresas SET excluido_em = ?, excluido_por = ? "
            "WHERE cnpj = ? AND excluido_em IS NULL", (_agora(), usuario, cnpj))
        if not r.rowcount:
            return False, "Empresa não encontrada."
        # reverte todas as alocações da empresa e oculta lançamentos
        alocs = conn.execute(
            "SELECT a.* FROM alocacoes a JOIN debitos d ON d.id = a.debito_id "
            "WHERE d.cnpj = ? AND a.excluido_em IS NULL", (cnpj,)).fetchall()
        for a in alocs:
            _reverter_alocacao(conn, a, usuario)
        conn.execute("UPDATE debitos SET excluido_em = ?, excluido_por = ? "
                     "WHERE cnpj = ? AND excluido_em IS NULL", (_agora(), usuario, cnpj))
        conn.execute("UPDATE pagamentos SET excluido_em = ?, excluido_por = ? "
                     "WHERE cnpj = ? AND excluido_em IS NULL", (_agora(), usuario, cnpj))
        _auditar(conn, "empresa", cnpj, "excluir", "", usuario)
        conn.commit()
        return True, "Empresa removida."
    finally:
        conn.close()


# ── Débitos ───────────────────────────────────────────────────────────────────
def _alocacoes_do_debito(conn, debito_id):
    return [dict(a) for a in conn.execute(
        "SELECT a.id, a.valor, a.pagamento_id, p.nf_numero "
        "FROM alocacoes a JOIN pagamentos p ON p.id = a.pagamento_id "
        "WHERE a.debito_id = ? AND a.excluido_em IS NULL ORDER BY a.criado_em",
        (debito_id,)).fetchall()]


def listar_debitos(cnpj=None):
    conn = _conn()
    try:
        sql = "SELECT * FROM debitos WHERE excluido_em IS NULL"
        params = ()
        if cnpj:
            sql += " AND cnpj = ?"
            params = (cnpj,)
        sql += " ORDER BY data DESC"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for d in rows:
            vp = round(d.get("valor_pago") or 0, 2)
            d["valor_pago"] = vp
            d["saldo"]      = round(d["valor_total"] - vp, 2)
            d["status"]     = _status(d["valor_total"], vp)
            d["data_fmt"]   = _fmt_data(d["data"])
            d["alocacoes"]  = _alocacoes_do_debito(conn, d["id"])
        return rows
    finally:
        conn.close()


def adicionar_debito_vencimento(cnpj, nf_numero, valor_total, obs="", usuario=None):
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
        if _nf_duplicada(conn, "debitos", cnpj, nf):
            return False, f"Já existe um débito com a NF {nf} para esta empresa."
        did = _uid()
        conn.execute(
            "INSERT INTO debitos (id, cnpj, data, tipo, nf_numero, valor_total, obs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (did, cnpj, _agora(), "vencimento", nf, valor, obs.strip()))
        _auditar(conn, "debito", did, "criar", f"vencimento NF {nf} · R$ {valor:.2f}", usuario)
        conn.commit()
        return True, f"Vencimento NF {nf} de R$ {valor:.2f} registrado."
    finally:
        conn.close()


def adicionar_debito_rebaxa(cnpj, produto, quantidade, valor_unit, obs="", usuario=None):
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
        did = _uid()
        conn.execute(
            "INSERT INTO debitos (id, cnpj, data, tipo, produto, quantidade, valor_unit, valor_total, obs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (did, cnpj, _agora(), "rebaxa", produto, qtd, v_uni, valor, obs.strip()))
        _auditar(conn, "debito", did, "criar", f"rebaxa {produto} · R$ {valor:.2f}", usuario)
        conn.commit()
        return True, f"Rebaxa de R$ {valor:.2f} registrada."
    finally:
        conn.close()


def excluir_debito(id_debito, usuario=None):
    conn = _conn()
    try:
        d = conn.execute("SELECT * FROM debitos WHERE id = ? AND excluido_em IS NULL",
                         (id_debito,)).fetchone()
        if not d:
            return False, "Débito não encontrado."
        for a in conn.execute("SELECT * FROM alocacoes WHERE debito_id = ? AND excluido_em IS NULL",
                              (id_debito,)).fetchall():
            _reverter_alocacao(conn, a, usuario)
        conn.execute("UPDATE debitos SET excluido_em = ?, excluido_por = ? WHERE id = ?",
                     (_agora(), usuario, id_debito))
        _auditar(conn, "debito", id_debito, "excluir", "", usuario)
        conn.commit()
        return True, "Débito removido."
    finally:
        conn.close()


# ── Pagamentos (créditos: bonificações etc.) ─────────────────────────────────
def _alocacoes_do_pagamento(conn, pagamento_id):
    saida = []
    for a in conn.execute(
        "SELECT a.id, a.valor, a.debito_id, d.tipo, d.nf_numero, d.produto "
        "FROM alocacoes a JOIN debitos d ON d.id = a.debito_id "
        "WHERE a.pagamento_id = ? AND a.excluido_em IS NULL ORDER BY a.criado_em",
        (pagamento_id,)).fetchall():
        rot = f"NF {a['nf_numero']}" if a["tipo"] == "vencimento" else (a["produto"] or "rebaxa")
        saida.append({"id": a["id"], "valor": a["valor"],
                      "debito_id": a["debito_id"], "rotulo": rot})
    return saida


def listar_pagamentos(cnpj=None):
    conn = _conn()
    try:
        sql = "SELECT * FROM pagamentos WHERE excluido_em IS NULL"
        params = ()
        if cnpj:
            sql += " AND cnpj = ?"
            params = (cnpj,)
        sql += " ORDER BY data DESC"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for p in rows:
            va = round(p.get("valor_alocado") or 0, 2)
            p["valor_alocado"] = va
            p["disponivel"]    = round(p["valor_total"] - va, 2)
            p["data_fmt"]      = _fmt_data(p["data"])
            p["alocacoes"]     = _alocacoes_do_pagamento(conn, p["id"])
        return rows
    finally:
        conn.close()


def adicionar_pagamento(cnpj, nf_numero, valor_total, obs="", usuario=None):
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
        if _nf_duplicada(conn, "pagamentos", cnpj, nf):
            return False, f"Já existe um pagamento com a NF {nf} para esta empresa."
        pid = _uid()
        conn.execute(
            "INSERT INTO pagamentos (id, cnpj, data, nf_numero, valor_total, obs) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pid, cnpj, _agora(), nf, valor, obs.strip()))
        _auditar(conn, "pagamento", pid, "criar", f"NF {nf} · R$ {valor:.2f}", usuario)
        conn.commit()
        return True, f"Pagamento NF {nf} de R$ {valor:.2f} registrado."
    finally:
        conn.close()


def excluir_pagamento(id_pagamento, usuario=None):
    conn = _conn()
    try:
        p = conn.execute("SELECT * FROM pagamentos WHERE id = ? AND excluido_em IS NULL",
                         (id_pagamento,)).fetchone()
        if not p:
            return False, "Pagamento não encontrado."
        for a in conn.execute("SELECT * FROM alocacoes WHERE pagamento_id = ? AND excluido_em IS NULL",
                              (id_pagamento,)).fetchall():
            _reverter_alocacao(conn, a, usuario)
        conn.execute("UPDATE pagamentos SET excluido_em = ?, excluido_por = ? WHERE id = ?",
                     (_agora(), usuario, id_pagamento))
        _auditar(conn, "pagamento", id_pagamento, "excluir", "", usuario)
        conn.commit()
        return True, "Pagamento removido."
    finally:
        conn.close()


# ── Alocações ─────────────────────────────────────────────────────────────────
def _reverter_alocacao(conn, aloc_row, usuario=None):
    """Desfaz uma alocação: marca excluída e devolve o valor aos dois lados."""
    conn.execute("UPDATE alocacoes SET excluido_em = ?, excluido_por = ? WHERE id = ?",
                 (_agora(), usuario, aloc_row["id"]))
    conn.execute("UPDATE pagamentos SET valor_alocado = "
                 "ROUND(MAX(0, COALESCE(valor_alocado,0) - ?), 2) WHERE id = ?",
                 (aloc_row["valor"], aloc_row["pagamento_id"]))
    conn.execute("UPDATE debitos SET valor_pago = "
                 "ROUND(MAX(0, COALESCE(valor_pago,0) - ?), 2) WHERE id = ?",
                 (aloc_row["valor"], aloc_row["debito_id"]))


def alocar(pagamento_id, debito_id, valor, usuario=None):
    valor = _parse_valor(valor)
    if valor is None:
        return False, "Valor inválido."
    conn = _conn()
    try:
        p = conn.execute("SELECT * FROM pagamentos WHERE id = ? AND excluido_em IS NULL",
                         (pagamento_id,)).fetchone()
        d = conn.execute("SELECT * FROM debitos WHERE id = ? AND excluido_em IS NULL",
                         (debito_id,)).fetchone()
        if not p:
            return False, "Pagamento não encontrado."
        if not d:
            return False, "Débito não encontrado."
        if p["cnpj"] != d["cnpj"]:
            return False, "Pagamento e débito são de empresas diferentes."
        disp  = round(p["valor_total"] - (p["valor_alocado"] or 0), 2)
        saldo = round(d["valor_total"] - (d["valor_pago"] or 0), 2)
        if valor > disp + _EPS:
            return False, f"O pagamento só tem R$ {disp:.2f} disponível."
        if valor > saldo + _EPS:
            return False, f"O débito só deve R$ {saldo:.2f}."
        aid = _uid()
        conn.execute(
            "INSERT INTO alocacoes (id, pagamento_id, debito_id, valor, criado_em, criado_por) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (aid, pagamento_id, debito_id, valor, _agora(), usuario))
        conn.execute("UPDATE pagamentos SET valor_alocado = ROUND(COALESCE(valor_alocado,0)+?,2) WHERE id = ?",
                     (valor, pagamento_id))
        conn.execute("UPDATE debitos SET valor_pago = ROUND(COALESCE(valor_pago,0)+?,2) WHERE id = ?",
                     (valor, debito_id))
        _auditar(conn, "alocacao", aid, "criar",
                 f"R$ {valor:.2f} · pag {pagamento_id} → déb {debito_id}", usuario)
        conn.commit()
        return True, f"Alocado R$ {valor:.2f}."
    finally:
        conn.close()


def alocar_automatico(pagamento_id, usuario=None):
    """Distribui o crédito disponível do pagamento entre os débitos em aberto,
    do mais antigo para o mais novo (FIFO)."""
    conn = _conn()
    try:
        p = conn.execute("SELECT * FROM pagamentos WHERE id = ? AND excluido_em IS NULL",
                         (pagamento_id,)).fetchone()
        if not p:
            return False, "Pagamento não encontrado."
        restante = round(p["valor_total"] - (p["valor_alocado"] or 0), 2)
        if restante <= _EPS:
            return False, "Este pagamento já está todo alocado."
        debs = conn.execute(
            "SELECT * FROM debitos WHERE cnpj = ? AND excluido_em IS NULL ORDER BY data ASC",
            (p["cnpj"],)).fetchall()
        alocado = 0.0
        for d in debs:
            if restante <= _EPS:
                break
            saldo = round(d["valor_total"] - (d["valor_pago"] or 0), 2)
            if saldo <= _EPS:
                continue
            v = round(min(saldo, restante), 2)
            aid = _uid()
            conn.execute(
                "INSERT INTO alocacoes (id, pagamento_id, debito_id, valor, criado_em, criado_por) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (aid, pagamento_id, d["id"], v, _agora(), usuario))
            conn.execute("UPDATE pagamentos SET valor_alocado = ROUND(COALESCE(valor_alocado,0)+?,2) WHERE id = ?",
                         (v, pagamento_id))
            conn.execute("UPDATE debitos SET valor_pago = ROUND(COALESCE(valor_pago,0)+?,2) WHERE id = ?",
                         (v, d["id"]))
            _auditar(conn, "alocacao", aid, "criar-auto", f"R$ {v:.2f} · déb {d['id']}", usuario)
            alocado = round(alocado + v, 2)
            restante = round(restante - v, 2)
        conn.commit()
        if alocado <= _EPS:
            return False, "Não há débito em aberto para alocar."
        return True, f"Alocado automaticamente R$ {alocado:.2f}."
    finally:
        conn.close()


def desalocar(alocacao_id, usuario=None):
    conn = _conn()
    try:
        a = conn.execute("SELECT * FROM alocacoes WHERE id = ? AND excluido_em IS NULL",
                         (alocacao_id,)).fetchone()
        if not a:
            return False, "Alocação não encontrada."
        _reverter_alocacao(conn, a, usuario)
        _auditar(conn, "alocacao", alocacao_id, "excluir", f"R$ {a['valor']:.2f}", usuario)
        conn.commit()
        return True, "Alocação desfeita."
    finally:
        conn.close()


# ── Saldo ─────────────────────────────────────────────────────────────────────
def calcular_saldo(cnpj):
    conn = _conn()
    try:
        tot_deb = conn.execute(
            "SELECT COALESCE(SUM(valor_total),0), COALESCE(SUM(valor_pago),0) "
            "FROM debitos WHERE cnpj = ? AND excluido_em IS NULL", (cnpj,)).fetchone()
        total_debito, total_pago = round(tot_deb[0], 2), round(tot_deb[1], 2)
        tot_pag = conn.execute(
            "SELECT COALESCE(SUM(valor_total),0), COALESCE(SUM(valor_alocado),0) "
            "FROM pagamentos WHERE cnpj = ? AND excluido_em IS NULL", (cnpj,)).fetchone()
        total_pagamentos, total_alocado = round(tot_pag[0], 2), round(tot_pag[1], 2)

        saldo_devedor    = round(total_debito - total_pago, 2)
        credito_disponivel = round(total_pagamentos - total_alocado, 2)
        return {
            "total_debito":       total_debito,
            "total_pago":         total_pago,
            "saldo_devedor":      saldo_devedor,
            "total_pagamentos":   total_pagamentos,
            "credito_disponivel": credito_disponivel,
            # alias de compatibilidade com telas antigas
            "total_bonificacao":  total_pagamentos,
            "quitado":            saldo_devedor <= _EPS,
        }
    finally:
        conn.close()


def resumo_empresas():
    resultado = []
    for emp in listar_empresas():
        saldo = calcular_saldo(emp["cnpj"])
        resultado.append({**emp, **saldo})
    return resultado


# ── Auditoria ─────────────────────────────────────────────────────────────────
def listar_auditoria(limite=200, entidade=None, entidade_id=None):
    conn = _conn()
    try:
        sql = "SELECT * FROM auditoria"
        cond, params = [], []
        if entidade:
            cond.append("entidade = ?"); params.append(entidade)
        if entidade_id:
            cond.append("entidade_id = ?"); params.append(str(entidade_id))
        if cond:
            sql += " WHERE " + " AND ".join(cond)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limite)
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for a in rows:
            a["quando_fmt"] = _fmt_data(a["quando"])
        return rows
    finally:
        conn.close()
