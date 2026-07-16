"""
scripts/auth.py
===============
Login simples do sistema (usuários + senhas) — sem dependências externas.

  • Senhas guardadas como hash PBKDF2-HMAC-SHA256 com salt por usuário
    (biblioteca padrão hashlib/secrets). Nunca em texto puro.
  • Persiste em dados/sistema.db.
  • Também guarda/gera a SECRET_KEY do Flask (dados/secret.key), para que as
    sessões sobrevivam a reinícios do serviço.

Camada de lógica: NÃO importa Flask.
"""
import os
import hmac
import sqlite3
import hashlib
import secrets
from datetime import datetime

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
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS usuarios (
            usuario   TEXT PRIMARY KEY,
            nome      TEXT,
            senha     TEXT NOT NULL,   -- pbkdf2$iter$salt$hash
            papel     TEXT NOT NULL DEFAULT 'admin',
            criado_em TEXT NOT NULL,
            criado_por TEXT
        );
    """)
    conn.commit()
    return conn


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
def contar_usuarios():
    conn = _conn()
    try:
        return conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0]
    finally:
        conn.close()


def listar_usuarios():
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT usuario, nome, papel, criado_em FROM usuarios ORDER BY usuario"
        ).fetchall()]
    finally:
        conn.close()


def criar_usuario(usuario, senha, nome="", papel="admin", criado_por=None):
    usuario = (usuario or "").strip().lower()
    if not usuario or not senha:
        return False, "Usuário e senha são obrigatórios."
    if len(senha) < 4:
        return False, "A senha deve ter ao menos 4 caracteres."
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO usuarios (usuario, nome, senha, papel, criado_em, criado_por) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (usuario, (nome or "").strip(), _hash(senha), papel, _agora(), criado_por),
        )
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
        r = conn.execute("SELECT * FROM usuarios WHERE usuario = ?", (usuario,)).fetchone()
    finally:
        conn.close()
    if r and _conferir(senha or "", r["senha"]):
        return {"usuario": r["usuario"], "nome": r["nome"], "papel": r["papel"]}
    return None


def alterar_senha(usuario, nova_senha):
    if not nova_senha or len(nova_senha) < 4:
        return False, "A senha deve ter ao menos 4 caracteres."
    conn = _conn()
    try:
        r = conn.execute("UPDATE usuarios SET senha = ? WHERE usuario = ?",
                         (_hash(nova_senha), (usuario or "").strip().lower()))
        conn.commit()
        return (True, "Senha alterada.") if r.rowcount else (False, "Usuário não encontrado.")
    finally:
        conn.close()


def excluir_usuario(usuario):
    usuario = (usuario or "").strip().lower()
    conn = _conn()
    try:
        if conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] <= 1:
            return False, "Não é possível excluir o único usuário do sistema."
        r = conn.execute("DELETE FROM usuarios WHERE usuario = ?", (usuario,))
        conn.commit()
        return (True, "Usuário excluído.") if r.rowcount else (False, "Usuário não encontrado.")
    finally:
        conn.close()


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
