"""
scripts/auth.py
===============
Login simples do sistema (usuários + senhas) — sem dependências externas.

  • Senhas guardadas como hash PBKDF2-HMAC-SHA256 com salt por usuário
    (biblioteca padrão hashlib/secrets). Nunca em texto puro.
  • Persiste em dados/sistema.db.
  • Também guarda/gera a SECRET_KEY do Flask (dados/secret.key), para que as
    sessões sobrevivam a reinícios do serviço.
  • Dois papéis: `admin` e `usuario`. Só o admin cria/exclui contas e decide
    quem tem ACESSO AO AGENTE do ERP (`acesso_agente`): mandar pedido ao robô
    que digita no RADGe, ligar/pausar as máquinas, aprovar máquina nova. O
    admin tem esse acesso sempre.

Camada de lógica: NÃO importa Flask.
"""
import os
import hmac
import sqlite3
import hashlib
import secrets
import threading
import time
from datetime import datetime

from scripts import db_util

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DADOS_DIR  = os.path.join(BASE_DIR, "dados")
BANCO      = os.path.join(DADOS_DIR, "sistema.db")
SECRET_ARQ = os.path.join(DADOS_DIR, "secret.key")

_ITERACOES = 240_000


def _conn():
    os.makedirs(DADOS_DIR, exist_ok=True)
    conn = sqlite3.connect(BANCO)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    # A guarda de login confere as permissões em TODA requisição: o esquema é
    # preparado uma vez por processo, não a cada pedido.
    db_util.preparar(conn, BANCO, _init_schema)
    return conn


def _init_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usuarios (
            usuario   TEXT PRIMARY KEY,
            nome      TEXT,
            senha     TEXT NOT NULL,   -- pbkdf2$iter$salt$hash
            papel     TEXT NOT NULL DEFAULT 'usuario',
            criado_em TEXT NOT NULL,
            criado_por TEXT
        );
        CREATE TABLE IF NOT EXISTS config (
            chave TEXT PRIMARY KEY,
            valor TEXT
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
    """)
    _migrar(conn)
    conn.commit()


# Único admin depois da migração de papéis (pedido do usuário, 29/09/2026).
ADMIN_INICIAL = "danielcadastro"
PAPEIS = ("admin", "usuario")


def _migrar(conn):
    """Idempotente. Acrescenta `acesso_agente` e, UMA vez, rebaixa todo mundo a
    `usuario` menos o ADMIN_INICIAL — até aqui toda conta nascia admin, então o
    papel não dizia nada. Se o ADMIN_INICIAL não existir (instalação nova, cujo
    admin veio do /setup), ninguém é rebaixado: rebaixar sem ter um admin
    trancaria o sistema do lado de fora."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(usuarios)")}
    if "acesso_agente" not in cols:
        conn.execute("ALTER TABLE usuarios ADD COLUMN acesso_agente INTEGER NOT NULL DEFAULT 0")
    # Excluir usuário virou soft-delete (03/11/2026): a auditoria aponta para o
    # nome, e apagar a linha deixava os registros sem dono identificável.
    for col in ("excluido_em", "excluido_por"):
        if col not in cols:
            conn.execute(f"ALTER TABLE usuarios ADD COLUMN {col} TEXT")
    feito = conn.execute("SELECT valor FROM config WHERE chave='migracao_papeis'").fetchone()
    if feito:
        return
    if conn.execute("SELECT 1 FROM usuarios WHERE usuario=?", (ADMIN_INICIAL,)).fetchone():
        conn.execute("UPDATE usuarios SET papel = CASE WHEN usuario=? THEN 'admin'"
                     " ELSE 'usuario' END", (ADMIN_INICIAL,))
        _auditar(conn, "sistema", "usuarios", "", "migrar_papeis",
                 f"so {ADMIN_INICIAL} continua admin")
    conn.execute("UPDATE usuarios SET papel='usuario' WHERE papel NOT IN ('admin','usuario')")
    conn.execute("INSERT INTO config (chave, valor) VALUES ('migracao_papeis', ?)", (_agora(),))


def _auditar(conn, usuario, entidade, entidade_id, acao, detalhe=""):
    conn.execute(
        "INSERT INTO auditoria (quando, usuario, entidade, entidade_id, acao, detalhe)"
        " VALUES (?,?,?,?,?,?)", (_agora(), usuario or "", entidade, entidade_id or "",
                                  acao, detalhe))


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Hash de senha ─────────────────────────────────────────────────────────────
def _hash(senha, salt=None, iteracoes=_ITERACOES):
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"),
                             bytes.fromhex(salt), iteracoes)
    return f"pbkdf2${iteracoes}${salt}${dk.hex()}"


def _conferir(senha, armazenado):
    try:
        algo, iteracoes, salt, _h = armazenado.split("$")
        if algo != "pbkdf2":
            return False
        calc = _hash(senha, salt, int(iteracoes))
        return hmac.compare_digest(calc, armazenado)
    except (ValueError, AttributeError):
        return False


# ── Usuários ──────────────────────────────────────────────────────────────────
# Senha nova precisa de 6 caracteres. Senhas antigas mais curtas continuam
# entrando: a regra vale ao criar/trocar, não tranca ninguém do lado de fora.
SENHA_MINIMA = 6
_ATIVO = "excluido_em IS NULL"


def _senha_valida(senha):
    if not senha or len(senha) < SENHA_MINIMA:
        return f"A senha deve ter ao menos {SENHA_MINIMA} caracteres."
    return None


def contar_usuarios():
    conn = _conn()
    try:
        return conn.execute(f"SELECT COUNT(*) FROM usuarios WHERE {_ATIVO}").fetchone()[0]
    finally:
        conn.close()


def listar_usuarios():
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT usuario, nome, papel, acesso_agente, criado_em FROM usuarios"
            f" WHERE {_ATIVO} ORDER BY papel <> 'admin', usuario"
        ).fetchall()]
    finally:
        conn.close()


def criar_usuario(usuario, senha, nome="", papel="usuario", criado_por=None):
    usuario = (usuario or "").strip().lower()
    if papel not in PAPEIS:
        return False, "Papel inválido."
    if not usuario or not senha:
        return False, "Usuário e senha são obrigatórios."
    erro = _senha_valida(senha)
    if erro:
        return False, erro
    conn = _conn()
    try:
        antigo = conn.execute("SELECT excluido_em FROM usuarios WHERE usuario=?",
                              (usuario,)).fetchone()
        if antigo and not antigo["excluido_em"]:
            return False, "Já existe um usuário com esse nome."
        if antigo:
            # Nome de conta excluída: reaproveita a linha (a chave é o nome),
            # com senha e papel novos e sem o acesso que a conta tinha antes.
            conn.execute(
                "UPDATE usuarios SET nome=?, senha=?, papel=?, acesso_agente=0,"
                " criado_em=?, criado_por=?, excluido_em=NULL, excluido_por=NULL"
                " WHERE usuario=?",
                ((nome or "").strip(), _hash(senha), papel, _agora(), criado_por, usuario))
            _auditar(conn, criado_por, "usuario", usuario, "recriar", f"papel {papel}")
        else:
            conn.execute(
                "INSERT INTO usuarios (usuario, nome, senha, papel, criado_em, criado_por) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (usuario, (nome or "").strip(), _hash(senha), papel, _agora(), criado_por),
            )
            _auditar(conn, criado_por, "usuario", usuario, "criar", f"papel {papel}")
        conn.commit()
        return True, "Usuário criado."
    except sqlite3.IntegrityError:
        return False, "Já existe um usuário com esse nome."
    finally:
        conn.close()


def verificar(usuario, senha):
    """Retorna o dict do usuário se as credenciais baterem, senão None."""
    usuario = (usuario or "").strip().lower()
    conn = _conn()
    try:
        r = conn.execute(f"SELECT * FROM usuarios WHERE usuario = ? AND {_ATIVO}",
                         (usuario,)).fetchone()
    finally:
        conn.close()
    if r and _conferir(senha or "", r["senha"]):
        return {"usuario": r["usuario"], "nome": r["nome"], "papel": r["papel"]}
    if not r:
        _hash(senha or "")   # mesmo custo de tempo: não revela se o usuário existe
    return None


def alterar_senha(usuario, nova_senha, por=None):
    """Troca a senha. `por` = quem trocou (o admin, na redefinição); sem ele, é
    a própria pessoa."""
    erro = _senha_valida(nova_senha)
    if erro:
        return False, erro
    usuario = (usuario or "").strip().lower()
    conn = _conn()
    try:
        r = conn.execute(f"UPDATE usuarios SET senha = ? WHERE usuario = ? AND {_ATIVO}",
                         (_hash(nova_senha), usuario))
        if r.rowcount:
            _auditar(conn, por or usuario, "usuario", usuario,
                     "redefinir_senha" if por and por != usuario else "alterar_senha")
        conn.commit()
        return (True, "Senha alterada.") if r.rowcount else (False, "Usuário não encontrado.")
    finally:
        conn.close()


def excluir_usuario(usuario, por=None):
    """Soft-delete: a conta deixa de entrar, a linha fica (a auditoria aponta
    para ela)."""
    usuario = (usuario or "").strip().lower()
    conn = _conn()
    try:
        if conn.execute(f"SELECT COUNT(*) FROM usuarios WHERE {_ATIVO}").fetchone()[0] <= 1:
            return False, "Não é possível excluir o único usuário do sistema."
        alvo = conn.execute(f"SELECT papel FROM usuarios WHERE usuario=? AND {_ATIVO}",
                            (usuario,)).fetchone()
        if not alvo:
            return False, "Usuário não encontrado."
        if alvo["papel"] == "admin" and conn.execute(
                f"SELECT COUNT(*) FROM usuarios WHERE papel='admin' AND {_ATIVO}").fetchone()[0] <= 1:
            return False, "Não é possível excluir o único administrador."
        conn.execute("UPDATE usuarios SET excluido_em=?, excluido_por=? WHERE usuario=?",
                     (_agora(), por, usuario))
        _auditar(conn, por, "usuario", usuario, "excluir")
        conn.commit()
        return True, "Usuário excluído."
    finally:
        conn.close()


def permissoes(usuario):
    """{papel, admin, agente, existe} de quem está logado, lido do banco a cada
    pedido: tirar o acesso vale na hora, sem esperar a sessão expirar.
    Usuário que não existe mais (ou foi excluído) não pode nada — e a guarda
    derruba a sessão dele."""
    conn = _conn()
    try:
        r = conn.execute(f"SELECT papel, acesso_agente FROM usuarios WHERE usuario=? AND {_ATIVO}",
                         ((usuario or "").strip().lower(),)).fetchone()
    finally:
        conn.close()
    if not r:
        return {"papel": "", "admin": False, "agente": False, "existe": False}
    admin = r["papel"] == "admin"
    return {"papel": r["papel"], "admin": admin,
            "agente": admin or bool(r["acesso_agente"]), "existe": True}


def definir_acesso_agente(usuario, permitir, por=None):
    usuario = (usuario or "").strip().lower()
    conn = _conn()
    try:
        r = conn.execute(f"SELECT papel FROM usuarios WHERE usuario=? AND {_ATIVO}",
                         (usuario,)).fetchone()
        if not r:
            return False, "Usuário não encontrado."
        if r["papel"] == "admin":
            return False, "O administrador sempre tem acesso ao agente."
        conn.execute("UPDATE usuarios SET acesso_agente=? WHERE usuario=?",
                     (1 if permitir else 0, usuario))
        _auditar(conn, por, "usuario", usuario, "acesso_agente",
                 "liberado" if permitir else "retirado")
        conn.commit()
        return True, ("Acesso ao agente liberado." if permitir
                      else "Acesso ao agente retirado.")
    finally:
        conn.close()


# ── Limite de tentativas de login ─────────────────────────────────────────────
# Em memória, de propósito: reiniciar o serviço zera, e tudo bem — o objetivo é
# tornar adivinhar senha pela rede lento, não guardar histórico. Duas contagens:
# por USUÁRIO (alguém tentando a conta de outro, de qualquer aparelho) e por IP
# (um aparelho varrendo várias contas).
FALHAS_USUARIO = 5        # erros na mesma conta...
FALHAS_IP = 20            # ...ou no mesmo aparelho, dentro da janela
JANELA_FALHAS_S = 15 * 60
BLOQUEIO_S = 5 * 60

_falhas = {}              # (tipo, valor) -> [instantes das falhas]
_bloqueio = {}            # (tipo, valor) -> instante em que libera
_lock_falhas = threading.Lock()


def _chaves(usuario, ip):
    return (("u", (usuario or "").strip().lower(), FALHAS_USUARIO),
            ("ip", ip or "", FALHAS_IP))


def bloqueio_restante(usuario, ip, agora=None):
    """Segundos até poder tentar de novo (0 = liberado)."""
    agora = time.monotonic() if agora is None else agora
    with _lock_falhas:
        resta = 0
        for tipo, valor, _lim in _chaves(usuario, ip):
            resta = max(resta, _bloqueio.get((tipo, valor), 0) - agora)
    return int(resta + 0.999) if resta > 0 else 0


def registrar_falha(usuario, ip, agora=None):
    agora = time.monotonic() if agora is None else agora
    with _lock_falhas:
        for tipo, valor, limite in _chaves(usuario, ip):
            if not valor:
                continue
            k = (tipo, valor)
            lista = [t for t in _falhas.get(k, []) if agora - t < JANELA_FALHAS_S]
            lista.append(agora)
            _falhas[k] = lista
            if len(lista) >= limite:
                _bloqueio[k] = agora + BLOQUEIO_S
                _falhas[k] = []


def limpar_falhas(usuario):
    """Login certo zera a conta. O IP não: um aparelho que acertou uma senha
    não ganha crédito para varrer as outras."""
    k = ("u", (usuario or "").strip().lower())
    with _lock_falhas:
        _falhas.pop(k, None)
        _bloqueio.pop(k, None)


# ── SECRET_KEY persistente ────────────────────────────────────────────────────
def obter_ou_criar_secret():
    os.makedirs(DADOS_DIR, exist_ok=True)
    if os.path.isfile(SECRET_ARQ):
        with open(SECRET_ARQ, "r", encoding="utf-8") as f:
            chave = f.read().strip()
            if chave:
                return chave
    chave = secrets.token_hex(32)
    with open(SECRET_ARQ, "w", encoding="utf-8") as f:
        f.write(chave)
    return chave
