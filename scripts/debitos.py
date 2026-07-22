"""
scripts/debitos.py
Módulo de débitos, pagamentos e alocações por empresa.
Persiste em dados/debitos.db (SQLite).

Modelo de dados (acompanhamento de pagamento NF-a-NF):
  • debitos      — o que a empresa nos deve (vencimento por NF ou rebaxa de
                   preço). Cada débito acumula `valor_pago`; o status
                   (aberto/parcial/quitado) é derivado de valor_pago × valor_total.
  • pagamentos   — abatimentos a nosso favor, de três `tipo`s: bonificação (NF),
                   troca direta de produtos ou desconto no boleto. Cada um tem
                   uma `referencia` (nº NF / nº boleto / descrição). Acumulam
                   `valor_alocado`; o que sobra é o CRÉDITO disponível da empresa.
  • alocacoes    — ligação N:N: "R$ X do pagamento P quitou o débito D". É ela
                   que permite ver, por débito, quanto ainda falta, e por
                   pagamento, quais débitos ele cobriu.

Fluxo: o pagamento é lançado DENTRO de um débito (abate aquele débito); se o
valor exceder o saldo, o excedente vira crédito; o crédito pode depois ser
aplicado em outro débito (alocar / alocar_automatico). Também é possível lançar
um crédito avulso (sem débito de origem).

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
import calendar
from datetime import datetime

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DADOS_DIR = os.path.join(BASE_DIR, "dados")
BANCO     = os.path.join(DADOS_DIR, "debitos.db")

_EPS = 0.005  # tolerância p/ comparação de centavos

# Tipos de pagamento (abatimento) e seus rótulos amigáveis.
TIPOS_PAGAMENTO = {
    "bonificacao":     "Bonificação",
    "troca":           "Troca direta",
    "desconto_boleto": "Desconto no boleto",
}
# Rótulo do campo de referência conforme o tipo (usado pela UI).
REF_LABEL = {
    "bonificacao":     "Nº da NF",
    "troca":           "Descrição da troca",
    "desconto_boleto": "Nº / venc. do boleto",
}
MESES_PT = ["", "janeiro", "fevereiro", "março", "abril", "maio", "junho",
            "julho", "agosto", "setembro", "outubro", "novembro", "dezembro"]


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
            periodo_tipo   TEXT,   -- 'mes' | 'intervalo' | NULL (sem período)
            periodo_inicio TEXT,   -- AAAA-MM-DD
            periodo_fim    TEXT,   -- AAAA-MM-DD
            excluido_em  TEXT,
            excluido_por TEXT,
            FOREIGN KEY (cnpj) REFERENCES empresas(cnpj) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS pagamentos (
            id            TEXT PRIMARY KEY,
            cnpj          TEXT NOT NULL,
            data          TEXT NOT NULL,
            tipo          TEXT NOT NULL DEFAULT 'bonificacao',  -- bonificacao | troca | desconto_boleto
            referencia    TEXT,                                 -- nº NF / nº boleto / descrição
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
    for col in ("periodo_tipo", "periodo_inicio", "periodo_fim"):
        if col not in _colunas(conn, "debitos"):
            conn.execute(f"ALTER TABLE debitos ADD COLUMN {col} TEXT")

    # pagamentos: nf_numero -> referencia + tipo. Como o SQLite não afrouxa o
    # NOT NULL de nf_numero via ALTER, reconstrói a tabela (não destrutivo).
    if "pagamentos" in _tabelas(conn) and "referencia" not in _colunas(conn, "pagamentos"):
        conn.executescript("""
            ALTER TABLE pagamentos RENAME TO _pag_old;
            CREATE TABLE pagamentos (
                id            TEXT PRIMARY KEY,
                cnpj          TEXT NOT NULL,
                data          TEXT NOT NULL,
                tipo          TEXT NOT NULL DEFAULT 'bonificacao',
                referencia    TEXT,
                valor_total   REAL NOT NULL,
                valor_alocado REAL NOT NULL DEFAULT 0,
                obs           TEXT,
                excluido_em   TEXT,
                excluido_por  TEXT,
                FOREIGN KEY (cnpj) REFERENCES empresas(cnpj) ON DELETE CASCADE
            );
            INSERT INTO pagamentos
                (id, cnpj, data, tipo, referencia, valor_total, valor_alocado,
                 obs, excluido_em, excluido_por)
            SELECT id, cnpj, data, 'bonificacao', nf_numero, valor_total,
                   COALESCE(valor_alocado, 0), obs, excluido_em, excluido_por
            FROM _pag_old;
            DROP TABLE _pag_old;
            CREATE INDEX IF NOT EXISTS idx_pag_cnpj ON pagamentos(cnpj);
        """)

    # migra a tabela antiga bonificacoes -> pagamentos (idempotente por PK).
    if "bonificacoes" in _tabelas(conn):
        conn.execute("""
            INSERT OR IGNORE INTO pagamentos
                (id, cnpj, data, tipo, referencia, valor_total, valor_alocado, obs)
            SELECT id, cnpj, data, 'bonificacao', nf_numero, valor_total, 0, obs
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


# ── Período do débito ─────────────────────────────────────────────────────────
_DATA_ISO = re.compile(r'^(\d{4})-(\d{2})-(\d{2})$')
_DATA_BR  = re.compile(r'^(\d{2})/(\d{2})/(\d{4})$')


def _norm_dia(s):
    """Aceita 'AAAA-MM-DD' ou 'DD/MM/AAAA' → ISO; None se inválido."""
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


def _fmt_dia(s):
    """'2026-04-05' → '05/04/2026'."""
    m = _DATA_ISO.match(s or "")
    if not m:
        return s or ""
    y, mo, d = m.groups()
    return f"{d}/{mo}/{y}"


def _parse_periodo(tipo, inicio, fim):
    """Valida o período informado.
    Retorna (tipo, inicio_iso, fim_iso) se ok; None se ausente; False se inválido.
      • tipo 'mes':       `inicio` = 'AAAA-MM' → 1º ao último dia do mês.
      • tipo 'intervalo': `inicio`/`fim` = datas (ISO ou BR), início <= fim.
    """
    tipo = (tipo or "").strip()
    if tipo not in ("mes", "intervalo"):
        return None
    if tipo == "mes":
        m = re.match(r'^(\d{4})-(\d{2})$', (inicio or "").strip())
        if not m:
            return False
        y, mo = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12):
            return False
        ult = calendar.monthrange(y, mo)[1]
        return ("mes", f"{y:04d}-{mo:02d}-01", f"{y:04d}-{mo:02d}-{ult:02d}")
    ini, f = _norm_dia(inicio), _norm_dia(fim)
    if not ini or not f or ini > f:
        return False
    return ("intervalo", ini, f)


def _periodo_label(tipo, inicio, fim):
    if tipo == "mes" and inicio:
        y, mo = inicio[:4], int(inicio[5:7])
        return f"{MESES_PT[mo]}/{y}"
    if tipo == "intervalo" and inicio and fim:
        return f"{_fmt_dia(inicio)} a {_fmt_dia(fim)}"
    return ""


def _meses_entre(inicio_iso, fim_iso):
    """Lista de 'AAAA-MM' cobertos por [inicio, fim] (inclusive)."""
    if not inicio_iso or not fim_iso:
        return []
    y, m = int(inicio_iso[:4]), int(inicio_iso[5:7])
    y2, m2 = int(fim_iso[:4]), int(fim_iso[5:7])
    out = []
    while (y, m) <= (y2, m2):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            m = 1; y += 1
    return out


def rotulo_mes(mes):
    """'2026-06' → 'junho/2026'."""
    try:
        y, mo = mes.split("-")
        return f"{MESES_PT[int(mo)]}/{y}"
    except (ValueError, IndexError):
        return mes


def meses_debitos(cnpj):
    """Meses presentes nos débitos da empresa (para o seletor), do mais recente
    ao mais antigo, + se há débitos sem período."""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT periodo_tipo, periodo_inicio, periodo_fim FROM debitos "
            "WHERE cnpj = ? AND excluido_em IS NULL", (cnpj,)).fetchall()
    finally:
        conn.close()
    meses, tem_sem = set(), False
    for r in rows:
        if not r["periodo_inicio"]:
            tem_sem = True
            continue
        meses.update(_meses_entre(r["periodo_inicio"], r["periodo_fim"]))
    ordenados = sorted(meses, reverse=True)
    return {
        "meses": [{"mes": m, "rotulo": rotulo_mes(m)} for m in ordenados],
        "tem_sem_periodo": tem_sem,
    }


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


def _nf_debito_duplicada_outro(conn, cnpj, nf, id_atual):
    """Como _nf_duplicada, mas ignora o próprio débito (para edição)."""
    return conn.execute(
        "SELECT 1 FROM debitos WHERE cnpj = ? AND nf_numero = ? "
        "AND excluido_em IS NULL AND id <> ?", (cnpj, nf, id_atual),
    ).fetchone() is not None


def _ref_bonificacao_duplicada(conn, cnpj, referencia):
    """Barra NF de bonificação repetida (só bonificação tem nº único de NF)."""
    return conn.execute(
        "SELECT 1 FROM pagamentos WHERE cnpj = ? AND tipo = 'bonificacao' "
        "AND referencia = ? AND excluido_em IS NULL", (cnpj, referencia),
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


def editar_empresa(cnpj, novo_cnpj=None, nova_razao=None, usuario=None):
    """Altera razão social e/ou CNPJ de uma empresa. Mudança de CNPJ migra os
    débitos e pagamentos para a chave nova (o CNPJ é a PK e a FK de tudo aqui),
    preservando lançamentos, alocações e histórico."""
    conn = _conn()
    try:
        emp = conn.execute("SELECT * FROM empresas WHERE cnpj = ? AND excluido_em IS NULL",
                           (cnpj,)).fetchone()
        if not emp:
            return False, "Empresa não encontrada."
        novo_cnpj = (novo_cnpj or "").strip() or cnpj
        nova_razao = (nova_razao or "").strip() or emp["razao_social"]
        if novo_cnpj != cnpj:
            if conn.execute("SELECT 1 FROM empresas WHERE cnpj = ?", (novo_cnpj,)).fetchone():
                return False, "Já existe empresa com este CNPJ."
            # a FK debitos/pagamentos -> empresas(cnpj) não tem ON UPDATE CASCADE;
            # desliga a checagem só nesta conexão para trocar a chave em bloco
            # (precisa ser antes de qualquer DML — fora de transação aberta).
            conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("UPDATE empresas   SET cnpj = ? WHERE cnpj = ?", (novo_cnpj, cnpj))
            conn.execute("UPDATE debitos    SET cnpj = ? WHERE cnpj = ?", (novo_cnpj, cnpj))
            conn.execute("UPDATE pagamentos SET cnpj = ? WHERE cnpj = ?", (novo_cnpj, cnpj))
        if nova_razao != emp["razao_social"]:
            conn.execute("UPDATE empresas SET razao_social = ? WHERE cnpj = ?",
                         (nova_razao, novo_cnpj))
        _auditar(conn, "empresa", novo_cnpj, "editar",
                 f"{emp['razao_social']} ({cnpj}) -> {nova_razao} ({novo_cnpj})", usuario)
        conn.commit()
        return True, "Empresa atualizada."
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
    """Pagamentos aplicados a um débito, com o tipo e a referência de cada um."""
    saida = []
    for a in conn.execute(
        "SELECT a.id, a.valor, a.pagamento_id, p.tipo, p.referencia "
        "FROM alocacoes a JOIN pagamentos p ON p.id = a.pagamento_id "
        "WHERE a.debito_id = ? AND a.excluido_em IS NULL ORDER BY a.criado_em",
        (debito_id,)).fetchall():
        saida.append({
            "id": a["id"], "valor": a["valor"], "pagamento_id": a["pagamento_id"],
            "tipo": a["tipo"], "tipo_label": TIPOS_PAGAMENTO.get(a["tipo"], a["tipo"]),
            "referencia": a["referencia"] or "",
        })
    return saida


def listar_debitos(cnpj=None, mes=None):
    """Lista débitos. `mes` filtra pelo período:
      • 'AAAA-MM' → débitos cujo período cobre aquele mês (intervalos aparecem
        em todos os meses que tocam);
      • 'sem'     → débitos sem período;
      • None/''   → todos.
    """
    conn = _conn()
    try:
        sql = "SELECT * FROM debitos WHERE excluido_em IS NULL"
        params = []
        if cnpj:
            sql += " AND cnpj = ?"
            params.append(cnpj)
        if mes == "sem":
            sql += " AND periodo_inicio IS NULL"
        elif mes:
            m = re.match(r'^(\d{4})-(\d{2})$', mes)
            if m:
                y, mo = int(m.group(1)), int(m.group(2))
                primeiro = f"{y:04d}-{mo:02d}-01"
                ultimo   = f"{y:04d}-{mo:02d}-{calendar.monthrange(y, mo)[1]:02d}"
                # sobreposição: periodo_inicio <= último E periodo_fim >= primeiro
                sql += (" AND periodo_inicio IS NOT NULL "
                        "AND periodo_inicio <= ? AND periodo_fim >= ?")
                params.extend([ultimo, primeiro])
        sql += " ORDER BY data DESC"
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
        for d in rows:
            vp = round(d.get("valor_pago") or 0, 2)
            d["valor_pago"]     = vp
            d["saldo"]          = round(d["valor_total"] - vp, 2)
            d["status"]         = _status(d["valor_total"], vp)
            d["data_fmt"]       = _fmt_data(d["data"])
            d["periodo_label"]  = _periodo_label(d.get("periodo_tipo"),
                                                 d.get("periodo_inicio"), d.get("periodo_fim"))
            d["alocacoes"]      = _alocacoes_do_debito(conn, d["id"])
        return rows
    finally:
        conn.close()


def adicionar_debito_vencimento(cnpj, nf_numero, valor_total, obs="", usuario=None,
                                periodo_tipo=None, periodo_inicio=None, periodo_fim=None):
    if not buscar_empresa(cnpj):
        return False, "Empresa não encontrada."
    nf = nf_numero.strip()
    if not nf:
        return False, "Número da NF é obrigatório."
    valor = _parse_valor(valor_total)
    if valor is None:
        return False, "Valor inválido."
    per = _parse_periodo(periodo_tipo, periodo_inicio, periodo_fim)
    if per is None:
        return False, "Informe o período a que o débito se refere."
    if per is False:
        return False, "Período inválido."
    p_tipo, p_ini, p_fim = per
    conn = _conn()
    try:
        if _nf_duplicada(conn, "debitos", cnpj, nf):
            return False, f"Já existe um débito com a NF {nf} para esta empresa."
        did = _uid()
        conn.execute(
            "INSERT INTO debitos (id, cnpj, data, tipo, nf_numero, valor_total, obs, "
            "periodo_tipo, periodo_inicio, periodo_fim) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (did, cnpj, _agora(), "vencimento", nf, valor, obs.strip(), p_tipo, p_ini, p_fim))
        _auditar(conn, "debito", did, "criar",
                 f"vencimento NF {nf} · R$ {valor:.2f} · {_periodo_label(p_tipo, p_ini, p_fim)}", usuario)
        conn.commit()
        return True, f"Vencimento NF {nf} de R$ {valor:.2f} registrado."
    finally:
        conn.close()


def adicionar_debito_rebaxa(cnpj, produto, quantidade, valor_unit, obs="", usuario=None,
                            periodo_tipo=None, periodo_inicio=None, periodo_fim=None):
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
    per = _parse_periodo(periodo_tipo, periodo_inicio, periodo_fim)
    if per is None:
        return False, "Informe o período a que o débito se refere."
    if per is False:
        return False, "Período inválido."
    p_tipo, p_ini, p_fim = per
    conn = _conn()
    try:
        did = _uid()
        conn.execute(
            "INSERT INTO debitos (id, cnpj, data, tipo, produto, quantidade, valor_unit, valor_total, obs, "
            "periodo_tipo, periodo_inicio, periodo_fim) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (did, cnpj, _agora(), "rebaxa", produto, qtd, v_uni, valor, obs.strip(),
             p_tipo, p_ini, p_fim))
        _auditar(conn, "debito", did, "criar",
                 f"rebaxa {produto} · R$ {valor:.2f} · {_periodo_label(p_tipo, p_ini, p_fim)}", usuario)
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


def editar_debito(id_debito, valor_total=None, nf_numero=None, produto=None,
                  quantidade=None, valor_unit=None, obs=None,
                  periodo_tipo=None, periodo_inicio=None, periodo_fim=None, usuario=None):
    """Corrige as informações de um débito (dentro do mesmo tipo). Preserva o
    registro, as alocações e o histórico. O status é derivado — reajusta sozinho."""
    per = _parse_periodo(periodo_tipo, periodo_inicio, periodo_fim)
    if per is None:
        return False, "Informe o período a que o débito se refere."
    if per is False:
        return False, "Período inválido."
    p_tipo, p_ini, p_fim = per
    conn = _conn()
    try:
        d = conn.execute("SELECT * FROM debitos WHERE id = ? AND excluido_em IS NULL",
                         (id_debito,)).fetchone()
        if not d:
            return False, "Débito não encontrado."
        cnpj = d["cnpj"]
        valor_pago = round(d["valor_pago"] or 0, 2)

        if d["tipo"] == "vencimento":
            nf = (nf_numero or "").strip()
            if not nf:
                return False, "Número da NF é obrigatório."
            if _nf_debito_duplicada_outro(conn, cnpj, nf, id_debito):
                return False, f"Já existe outro débito com a NF {nf} para esta empresa."
            novo_valor = _parse_valor(valor_total)
            if novo_valor is None:
                return False, "Valor inválido."
            campos = {"nf_numero": nf, "valor_total": novo_valor}
            resumo = f"vencimento NF {nf} · R$ {novo_valor:.2f}"
        else:  # rebaxa
            prod = (produto or "").strip()
            if not prod:
                return False, "Nome do produto é obrigatório."
            try:
                qtd   = float(str(quantidade).replace(",", "."))
                v_uni = float(str(valor_unit).replace(",", "."))
                if qtd <= 0 or v_uni <= 0:
                    raise ValueError
                novo_valor = round(qtd * v_uni, 2)
            except (TypeError, ValueError):
                return False, "Quantidade ou valor unitário inválido."
            campos = {"produto": prod, "quantidade": qtd, "valor_unit": v_uni,
                      "valor_total": novo_valor}
            resumo = f"rebaxa {prod} · R$ {novo_valor:.2f}"

        if novo_valor + _EPS < valor_pago:
            return False, (f"O novo valor R$ {novo_valor:.2f} é menor que o já pago "
                           f"R$ {valor_pago:.2f}. Desfaça pagamentos antes de reduzir.")

        campos.update({"obs": (obs or "").strip(), "periodo_tipo": p_tipo,
                       "periodo_inicio": p_ini, "periodo_fim": p_fim})
        sets = ", ".join(f"{k} = ?" for k in campos)
        conn.execute(f"UPDATE debitos SET {sets} WHERE id = ?",
                     (*campos.values(), id_debito))
        _auditar(conn, "debito", id_debito, "editar",
                 f"{resumo} · {_periodo_label(p_tipo, p_ini, p_fim)}", usuario)
        conn.commit()
        return True, "Débito atualizado."
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
            p["tipo_label"]    = TIPOS_PAGAMENTO.get(p.get("tipo"), p.get("tipo") or "")
            p["referencia"]    = p.get("referencia") or ""
            p["alocacoes"]     = _alocacoes_do_pagamento(conn, p["id"])
        return rows
    finally:
        conn.close()


def listar_creditos(cnpj):
    """Pagamentos com saldo não alocado — o 'pool' de crédito da empresa."""
    return [p for p in listar_pagamentos(cnpj) if p["disponivel"] > _EPS]


def adicionar_pagamento(cnpj, valor_total, tipo="bonificacao", referencia="",
                        obs="", usuario=None, debito_id=None):
    """Registra um pagamento (abatimento). Se `debito_id` for informado, o valor
    abate aquele débito e o excedente vira crédito; sem `debito_id`, entra como
    crédito avulso."""
    if not buscar_empresa(cnpj):
        return False, "Empresa não encontrada."
    if tipo not in TIPOS_PAGAMENTO:
        return False, "Tipo de pagamento inválido."
    referencia = (referencia or "").strip()
    if not referencia:
        return False, f"Informe {REF_LABEL[tipo].lower()}."
    valor = _parse_valor(valor_total)
    if valor is None:
        return False, "Valor inválido."
    conn = _conn()
    try:
        if tipo == "bonificacao" and _ref_bonificacao_duplicada(conn, cnpj, referencia):
            return False, f"Já existe uma bonificação com a NF {referencia} para esta empresa."

        # se veio de um débito, valida antes de gravar qualquer coisa
        d = None
        if debito_id:
            d = conn.execute(
                "SELECT * FROM debitos WHERE id = ? AND cnpj = ? AND excluido_em IS NULL",
                (debito_id, cnpj)).fetchone()
            if not d:
                return False, "Débito de destino não encontrado."

        pid = _uid()
        conn.execute(
            "INSERT INTO pagamentos (id, cnpj, data, tipo, referencia, valor_total, obs) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pid, cnpj, _agora(), tipo, referencia, valor, obs.strip()))
        _auditar(conn, "pagamento", pid, "criar",
                 f"{TIPOS_PAGAMENTO[tipo]} {referencia} · R$ {valor:.2f}"
                 + (f" (no débito {debito_id})" if debito_id else " (crédito avulso)"),
                 usuario)

        alocado = 0.0
        if d:
            saldo = round(d["valor_total"] - (d["valor_pago"] or 0), 2)
            aloc = round(min(valor, saldo), 2)
            if aloc > _EPS:
                aid = _uid()
                conn.execute(
                    "INSERT INTO alocacoes (id, pagamento_id, debito_id, valor, criado_em, criado_por) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (aid, pid, debito_id, aloc, _agora(), usuario))
                conn.execute("UPDATE pagamentos SET valor_alocado = ROUND(COALESCE(valor_alocado,0)+?,2) WHERE id = ?",
                             (aloc, pid))
                conn.execute("UPDATE debitos SET valor_pago = ROUND(COALESCE(valor_pago,0)+?,2) WHERE id = ?",
                             (aloc, debito_id))
                _auditar(conn, "alocacao", aid, "criar",
                         f"R$ {aloc:.2f} · pag {pid} → déb {debito_id}", usuario)
                alocado = aloc
        conn.commit()

        credito = round(valor - alocado, 2)
        if not debito_id:
            return True, f"Crédito de R$ {valor:.2f} registrado."
        if credito > _EPS:
            return True, (f"R$ {alocado:.2f} abateram o débito e "
                          f"R$ {credito:.2f} viraram crédito.")
        return True, f"R$ {alocado:.2f} aplicados ao débito."
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
            "SELECT COALESCE(SUM(valor_total),0), COALESCE(SUM(valor_pago),0), "
            "COUNT(*), COALESCE(SUM(CASE WHEN valor_pago + ? < valor_total THEN 1 ELSE 0 END),0) "
            "FROM debitos WHERE cnpj = ? AND excluido_em IS NULL", (_EPS, cnpj)).fetchone()
        total_debito, total_pago = round(tot_deb[0], 2), round(tot_deb[1], 2)
        debitos_total, debitos_abertos = tot_deb[2], tot_deb[3]
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
            "debitos_total":      debitos_total,
            "debitos_abertos":    debitos_abertos,
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
