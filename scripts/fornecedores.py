"""
scripts/fornecedores.py
Cadastro central de fornecedores/empresas — fonte da verdade compartilhada
pelos módulos de débitos ("empresas") e de vencidos ("fornecedor").

Regras:
  • Fornecedor pode existir SEM CNPJ (criado pelo vencidos só com nome).
  • CNPJ é obrigatório apenas para participar de débitos; quem ganha CNPJ
    também ganha uma linha em `empresas` no debitos.db (sincronizada pela
    camada de rotas — invariante: fornecedor com CNPJ <=> empresa em débitos).
  • CNPJ é armazenado como digitado (com pontos/barra, compatível com as
    chaves atuais de débitos); duplicidade é comparada só pelos dígitos.

Semeadura (uma vez por processo, tolerante a falha):
  • importa as empresas não excluídas do debitos.db (leitura read-only);
  • importa os nomes distintos de fornecedor já digitados no vencidos.db.
"""
import os
import re
import uuid
import sqlite3
from datetime import datetime

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DADOS_DIR = os.path.join(BASE_DIR, "dados")
BANCO     = os.path.join(DADOS_DIR, "fornecedores.db")

_SEMEADO = False   # semeadura roda uma vez por processo


def _conn():
    os.makedirs(DADOS_DIR, exist_ok=True)
    conn = sqlite3.connect(BANCO)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    _init_schema(conn)
    return conn


def _init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fornecedores (
            id           TEXT PRIMARY KEY,
            cnpj         TEXT UNIQUE,          -- pode ser NULL (fornecedor só com nome)
            nome         TEXT NOT NULL,
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
        CREATE INDEX IF NOT EXISTS idx_forn_nome ON fornecedores(nome);
    """)
    _migrar(conn)
    conn.commit()
    global _SEMEADO
    if not _SEMEADO:
        _SEMEADO = True
        _semear(conn)


def _migrar(conn):
    """`numero` = código do fornecedor no ERP (RADGe). Único entre os ativos:
    é por ele que o agente filtra as notas de entrada, e dois cadastros com o
    mesmo número abririam a nota do fornecedor errado."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(fornecedores)")}
    if "numero" not in cols:
        conn.execute("ALTER TABLE fornecedores ADD COLUMN numero TEXT")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_forn_numero ON fornecedores(numero) "
                 "WHERE numero IS NOT NULL AND excluido_em IS NULL")
    # `mesclado_para` = id do cadastro que absorveu este. Sem ele, cadastrar de
    # novo o nome de um absorvido o reativaria, desfazendo a junção calado.
    if "mesclado_para" not in cols:
        conn.execute("ALTER TABLE fornecedores ADD COLUMN mesclado_para TEXT")
        # junções anteriores só deixaram rastro na auditoria: "X -> Y (id)"
        for r in conn.execute("SELECT entidade_id, detalhe FROM auditoria "
                              "WHERE acao='mesclado_em'").fetchall():
            m = re.search(r"\(([^()]+)\)\s*$", r["detalhe"] or "")
            if m and r["entidade_id"]:
                conn.execute("UPDATE fornecedores SET mesclado_para=? WHERE id=?",
                             (m.group(1), r["entidade_id"]))


def _destino_mescla(conn, r):
    """Segue a cadeia de junções a partir de um cadastro absorvido até o ativo
    que ficou com ele (ou None, se a cadeia terminar num excluído)."""
    vistos = set()
    while r and r["excluido_em"] and r["mesclado_para"] and r["id"] not in vistos:
        vistos.add(r["id"])
        r = conn.execute("SELECT * FROM fornecedores WHERE id=?",
                         (r["mesclado_para"],)).fetchone()
    return r if r and not r["excluido_em"] else None


# ── Utilitários ───────────────────────────────────────────────────────────────
def _uid():
    return str(uuid.uuid4())[:8]


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _norm_nome(s):
    return re.sub(r"\s+", " ", (s or "").strip()).lower()


def _cnpj_digitos(s):
    return re.sub(r"\D", "", s or "")


def _auditar(conn, entidade_id, acao, detalhe="", usuario=None):
    conn.execute(
        "INSERT INTO auditoria (quando, usuario, entidade, entidade_id, acao, detalhe) "
        "VALUES (?, ?, 'fornecedor', ?, ?, ?)",
        (_agora(), usuario, str(entidade_id) if entidade_id else None, acao, detalhe),
    )


def _numero(s):
    """Número do fornecedor no ERP: só dígitos, sem zero à esquerda ('015' e
    '15' são o mesmo cadastro no RADGe). Vazio -> None."""
    return re.sub(r"\D", "", str(s or "")).lstrip("0") or None


def _dict(r):
    return {"id": r["id"], "cnpj": r["cnpj"], "nome": r["nome"], "numero": r["numero"]}


# ── Semeadura a partir dos bancos existentes ─────────────────────────────────
def _semear(conn):
    """Importa cadastros já existentes nos outros módulos. Idempotente e
    tolerante a falha: bancos ausentes/estruturas antigas não quebram nada.
    Compara com TODAS as linhas (inclusive excluídas) para nunca ressuscitar
    um fornecedor removido de propósito."""
    existentes_cnpj = {_cnpj_digitos(r["cnpj"]) for r in
                       conn.execute("SELECT cnpj FROM fornecedores WHERE cnpj IS NOT NULL")}
    existentes_nome = {_norm_nome(r["nome"]) for r in
                       conn.execute("SELECT nome FROM fornecedores")}
    novos = 0

    # empresas de débitos (com CNPJ)
    try:
        db_deb = os.path.join(DADOS_DIR, "debitos.db")
        if os.path.exists(db_deb):
            ro = sqlite3.connect(f"file:{db_deb}?mode=ro", uri=True)
            try:
                ro.row_factory = sqlite3.Row
                for r in ro.execute("SELECT cnpj, razao_social FROM empresas "
                                    "WHERE excluido_em IS NULL"):
                    dig = _cnpj_digitos(r["cnpj"])
                    nome = (r["razao_social"] or "").strip()
                    if not nome or not dig or dig in existentes_cnpj:
                        continue
                    conn.execute(
                        "INSERT INTO fornecedores (id, cnpj, nome, criado_em) VALUES (?,?,?,?)",
                        (_uid(), r["cnpj"].strip(), nome, _agora()))
                    existentes_cnpj.add(dig)
                    existentes_nome.add(_norm_nome(nome))
                    novos += 1
            finally:
                ro.close()
    except Exception:
        pass  # debitos.db indisponível/antigo — segue sem ele

    # nomes de fornecedor já digitados nos vencidos (sem CNPJ)
    try:
        db_ven = os.path.join(DADOS_DIR, "vencidos.db")
        if os.path.exists(db_ven):
            ro = sqlite3.connect(f"file:{db_ven}?mode=ro", uri=True)
            try:
                nomes = set()
                for tabela in ("avisos", "vencidos"):
                    for r in ro.execute(f"SELECT DISTINCT fornecedor FROM {tabela} "
                                        "WHERE fornecedor IS NOT NULL AND TRIM(fornecedor) <> '' "
                                        "AND excluido_em IS NULL"):
                        nomes.add(r[0].strip())
            finally:
                ro.close()
            for nome in sorted(nomes):
                if _norm_nome(nome) in existentes_nome:
                    continue
                conn.execute(
                    "INSERT INTO fornecedores (id, cnpj, nome, criado_em) VALUES (?, NULL, ?, ?)",
                    (_uid(), nome, _agora()))
                existentes_nome.add(_norm_nome(nome))
                novos += 1
    except Exception:
        pass  # vencidos.db indisponível/antigo — segue sem ele

    if novos:
        _auditar(conn, None, "semear", f"{novos} fornecedor(es) importado(s)")
    conn.commit()


# ── Consulta ──────────────────────────────────────────────────────────────────
def listar(q=None, limite=50):
    """Fornecedores ativos, filtrados por nome ou CNPJ (subtexto, sem caixa)."""
    conn = _conn()
    try:
        sql = "SELECT * FROM fornecedores WHERE excluido_em IS NULL"
        params = []
        q = (q or "").strip()
        if q:
            dig = _cnpj_digitos(q)
            if dig:
                # também casa pelo CNPJ, comparando só os dígitos
                sql += " AND (nome LIKE ? COLLATE NOCASE OR "
                sql += "REPLACE(REPLACE(REPLACE(COALESCE(cnpj,''),'.',''),'/',''),'-','') LIKE ?)"
                params += [f"%{q}%", f"%{dig}%"]
            else:
                sql += " AND nome LIKE ? COLLATE NOCASE"
                params.append(f"%{q}%")
        sql += " ORDER BY nome COLLATE NOCASE LIMIT ?"
        params.append(int(limite))
        return [_dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def buscar(id_forn):
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM fornecedores WHERE id = ? AND excluido_em IS NULL",
                         (id_forn,)).fetchone()
        return _dict(r) if r else None
    finally:
        conn.close()


def buscar_por_cnpj(cnpj):
    dig = _cnpj_digitos(cnpj)
    if not dig:
        return None
    conn = _conn()
    try:
        r = _por_cnpj(conn, dig)
        return _dict(r) if r else None
    finally:
        conn.close()


def _por_cnpj(conn, dig, ativos=True):
    sql = ("SELECT * FROM fornecedores WHERE "
           "REPLACE(REPLACE(REPLACE(COALESCE(cnpj,''),'.',''),'/',''),'-','') = ?")
    if ativos:
        sql += " AND excluido_em IS NULL"
    return conn.execute(sql, (dig,)).fetchone()


def _por_nome(conn, nome, ativos=True):
    alvo = _norm_nome(nome)
    sql = "SELECT * FROM fornecedores"
    if ativos:
        sql += " WHERE excluido_em IS NULL"
    for r in conn.execute(sql):
        if _norm_nome(r["nome"]) == alvo:
            return r
    return None


def mapa_nomes():
    """{nome normalizado: id} dos fornecedores ativos — usado pelo backfill do
    módulo de vencidos."""
    conn = _conn()
    try:
        return {_norm_nome(r["nome"]): r["id"] for r in
                conn.execute("SELECT id, nome FROM fornecedores WHERE excluido_em IS NULL")}
    finally:
        conn.close()


# ── Mutações ──────────────────────────────────────────────────────────────────
def criar(nome, cnpj=None, usuario=None):
    """Cria um fornecedor (CNPJ opcional). Retorna (ok, msg, fornecedor|None).
    Reativa um excluído de mesmo CNPJ/nome em vez de duplicar."""
    nome = re.sub(r"\s+", " ", (nome or "").strip())
    if not nome:
        return False, "Informe o nome do fornecedor.", None
    cnpj = (cnpj or "").strip() or None
    if cnpj and not _cnpj_digitos(cnpj):
        return False, "CNPJ inválido.", None
    conn = _conn()
    try:
        if cnpj:
            r = _por_cnpj(conn, _cnpj_digitos(cnpj), ativos=False)
            if r and not r["excluido_em"]:
                return False, "Já existe fornecedor com este CNPJ.", _dict(r)
            if r:  # excluído: reativa com o nome novo
                conn.execute("UPDATE fornecedores SET nome=?, excluido_em=NULL, "
                             "excluido_por=NULL WHERE id=?", (nome, r["id"]))
                _auditar(conn, r["id"], "reativar", nome, usuario)
                conn.commit()
                return True, "Fornecedor reativado.", buscar(r["id"])
        # o ativo tem precedência: depois de uma junção que manteve o nome do
        # absorvido, há um ativo e um excluído com o mesmo nome
        r = _por_nome(conn, nome) or _por_nome(conn, nome, ativos=False)
        if r and not r["excluido_em"]:
            return False, "Já existe fornecedor com este nome.", _dict(r)
        if r and r["mesclado_para"]:
            destino = _destino_mescla(conn, r)
            if destino:
                return False, (f"'{nome}' foi juntado a '{destino['nome']}'."), _dict(destino)
        if r:  # excluído: reativa (e completa o CNPJ se veio)
            conn.execute("UPDATE fornecedores SET cnpj=COALESCE(?, cnpj), excluido_em=NULL, "
                         "excluido_por=NULL WHERE id=?", (cnpj, r["id"]))
            _auditar(conn, r["id"], "reativar", nome, usuario)
            conn.commit()
            return True, "Fornecedor reativado.", buscar(r["id"])
        fid = _uid()
        conn.execute("INSERT INTO fornecedores (id, cnpj, nome, criado_em, criado_por) "
                     "VALUES (?,?,?,?,?)", (fid, cnpj, nome, _agora(), usuario))
        _auditar(conn, fid, "criar", f"{nome}{' · ' + cnpj if cnpj else ''}", usuario)
        conn.commit()
        return True, f"Fornecedor '{nome}' cadastrado.", {"id": fid, "cnpj": cnpj, "nome": nome, "numero": None}
    finally:
        conn.close()


def definir_cnpj(id_forn, cnpj, usuario=None):
    """Define (ou corrige) o CNPJ de um fornecedor. Retorna (ok, msg, fornecedor|None)."""
    cnpj = (cnpj or "").strip()
    if not _cnpj_digitos(cnpj):
        return False, "Informe um CNPJ válido.", None
    conn = _conn()
    try:
        f = conn.execute("SELECT * FROM fornecedores WHERE id=? AND excluido_em IS NULL",
                         (id_forn,)).fetchone()
        if not f:
            return False, "Fornecedor não encontrado.", None
        outro = _por_cnpj(conn, _cnpj_digitos(cnpj))
        if outro and outro["id"] != id_forn:
            return False, f"Este CNPJ já pertence a '{outro['nome']}'.", None
        conn.execute("UPDATE fornecedores SET cnpj=? WHERE id=?", (cnpj, id_forn))
        _auditar(conn, id_forn, "definir_cnpj", cnpj, usuario)
        conn.commit()
        return True, "CNPJ definido.", {**_dict(f), "cnpj": cnpj}
    finally:
        conn.close()


def editar_nome(id_forn, nome, usuario=None):
    nome = re.sub(r"\s+", " ", (nome or "").strip())
    if not nome:
        return False, "Informe o nome do fornecedor.", None
    conn = _conn()
    try:
        f = conn.execute("SELECT * FROM fornecedores WHERE id=? AND excluido_em IS NULL",
                         (id_forn,)).fetchone()
        if not f:
            return False, "Fornecedor não encontrado.", None
        outro = _por_nome(conn, nome)
        if outro and outro["id"] != id_forn:
            return False, "Já existe fornecedor com este nome.", None
        conn.execute("UPDATE fornecedores SET nome=? WHERE id=?", (nome, id_forn))
        _auditar(conn, id_forn, "editar", f"{f['nome']} -> {nome}", usuario)
        conn.commit()
        return True, "Nome atualizado.", {**_dict(f), "nome": nome}
    finally:
        conn.close()


def listar_todos():
    """Todos os fornecedores ativos, para a página de cadastro."""
    conn = _conn()
    try:
        return [_dict(r) for r in conn.execute(
            "SELECT * FROM fornecedores WHERE excluido_em IS NULL "
            "ORDER BY nome COLLATE NOCASE")]
    finally:
        conn.close()


def definir_numero(id_forn, numero, usuario=None):
    """Define, corrige ou limpa (vazio) o número do fornecedor no ERP.
    Retorna (ok, msg, fornecedor|None)."""
    bruto = str(numero or "").strip()
    if bruto and not re.fullmatch(r"\d+", bruto):
        return False, "O número do fornecedor no ERP tem só dígitos.", None
    numero = _numero(bruto)
    if bruto and not numero:
        return False, "Número do fornecedor inválido.", None
    conn = _conn()
    try:
        f = conn.execute("SELECT * FROM fornecedores WHERE id=? AND excluido_em IS NULL",
                         (id_forn,)).fetchone()
        if not f:
            return False, "Fornecedor não encontrado.", None
        if numero == f["numero"]:
            return True, "Nada a alterar.", _dict(f)
        if numero:
            outro = conn.execute("SELECT nome FROM fornecedores WHERE numero=? AND id<>? "
                                 "AND excluido_em IS NULL", (numero, id_forn)).fetchone()
            if outro:
                return False, f"O número {numero} já pertence a '{outro['nome']}'.", None
        conn.execute("UPDATE fornecedores SET numero=? WHERE id=?", (numero, id_forn))
        _auditar(conn, id_forn, "definir_numero",
                 f"{f['numero'] or '—'} -> {numero or '—'}", usuario)
        conn.commit()
        return True, "Número atualizado.", {**_dict(f), "numero": numero}
    finally:
        conn.close()


# ── Junção de cadastros duplicados ───────────────────────────────────────────
def plano_mescla(id_a, id_b, nome):
    """Valida a junção de dois cadastros e diz como ela vai ficar, SEM gravar.
    Retorna (ok, msg, plano|None); plano = {fica, sai, nome, cnpj, numero}.

    Quem sobrevive é o registro que tem CNPJ (a chave dos débitos continua a
    mesma); sem CNPJ, o que tem número; sem nenhum, o `id_a`. O nome final é
    escolha de quem junta e precisa ser um dos dois. CNPJs diferentes são
    empresas diferentes em débitos, e números diferentes são fornecedores
    diferentes no ERP: nos dois casos a junção é recusada."""
    if not id_a or not id_b or id_a == id_b:
        return False, "Escolha dois cadastros diferentes.", None
    conn = _conn()
    try:
        a = conn.execute("SELECT * FROM fornecedores WHERE id=? AND excluido_em IS NULL",
                         (id_a,)).fetchone()
        b = conn.execute("SELECT * FROM fornecedores WHERE id=? AND excluido_em IS NULL",
                         (id_b,)).fetchone()
        if not a or not b:
            return False, "Fornecedor não encontrado.", None
        if a["cnpj"] and b["cnpj"] and _cnpj_digitos(a["cnpj"]) != _cnpj_digitos(b["cnpj"]):
            return False, (f"Os dois têm CNPJ diferente ({a['cnpj']} e {b['cnpj']}): "
                           "são empresas distintas e não podem ser juntadas."), None
        if a["numero"] and b["numero"] and a["numero"] != b["numero"]:
            return False, (f"Os dois têm número diferente no ERP ({a['numero']} e "
                           f"{b['numero']}): são fornecedores distintos no RADGe."), None
        nome = re.sub(r"\s+", " ", (nome or "").strip())
        if nome not in (a["nome"], b["nome"]):
            return False, "Escolha qual dos dois nomes fica.", None
        if b["cnpj"] and not a["cnpj"]:
            fica, sai = b, a
        elif b["numero"] and not a["numero"] and not a["cnpj"]:
            fica, sai = b, a
        else:
            fica, sai = a, b
        outro = _por_nome(conn, nome)
        if outro and outro["id"] not in (a["id"], b["id"]):
            return False, f"Já existe outro fornecedor chamado '{nome}'.", None
        return True, "", {
            "fica": _dict(fica), "sai": _dict(sai), "nome": nome,
            "cnpj": fica["cnpj"] or sai["cnpj"],
            "numero": fica["numero"] or sai["numero"],
        }
    finally:
        conn.close()


def mesclar(plano, usuario=None):
    """Executa um plano de `plano_mescla`: o que sai vira soft-delete (com CNPJ
    e número liberados, que passam para o que fica) e o que fica recebe nome,
    CNPJ e número finais. Os lançamentos dos outros módulos são movidos pela
    camada de rotas. Retorna (ok, msg, fornecedor_final|None)."""
    fica, sai = plano["fica"], plano["sai"]
    conn = _conn()
    try:
        # revalida dentro da conexão: entre a prévia e o clique alguém pode ter
        # excluído ou juntado um dos dois
        vivos = conn.execute("SELECT COUNT(*) FROM fornecedores WHERE id IN (?,?) "
                             "AND excluido_em IS NULL", (fica["id"], sai["id"])).fetchone()[0]
        if vivos != 2:
            return False, "Um dos cadastros mudou enquanto isso; abra a junção de novo.", None
        agora = _agora()
        conn.execute("UPDATE fornecedores SET excluido_em=?, excluido_por=?, cnpj=NULL, "
                     "numero=NULL, mesclado_para=? WHERE id=?",
                     (agora, usuario, fica["id"], sai["id"]))
        conn.execute("UPDATE fornecedores SET nome=?, cnpj=?, numero=? WHERE id=?",
                     (plano["nome"], plano["cnpj"], plano["numero"], fica["id"]))
        herdou = []
        if sai["cnpj"] and not fica["cnpj"]:
            herdou.append(f"CNPJ {sai['cnpj']}")
        if sai["numero"] and not fica["numero"]:
            herdou.append(f"número {sai['numero']}")
        _auditar(conn, sai["id"], "mesclado_em",
                 f"{sai['nome']} -> {plano['nome']} ({fica['id']})", usuario)
        _auditar(conn, fica["id"], "mesclar",
                 f"absorveu '{sai['nome']}' ({sai['id']})"
                 + (f"; herdou {', '.join(herdou)}" if herdou else ""), usuario)
        if plano["nome"] != fica["nome"]:
            _auditar(conn, fica["id"], "editar", f"{fica['nome']} -> {plano['nome']}", usuario)
        conn.commit()
        return True, (f"'{sai['nome']}' e '{fica['nome']}' agora são um cadastro só: "
                      f"'{plano['nome']}'."), \
            {"id": fica["id"], "nome": plano["nome"], "cnpj": plano["cnpj"],
             "numero": plano["numero"]}
    finally:
        conn.close()
