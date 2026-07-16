"""
scripts/auth_routes.py
Blueprint de autenticação: login, logout, primeiro acesso (setup) e
gerenciamento de usuários. Também instala a guarda global de sessão.
"""
from functools import wraps
from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, jsonify)
from scripts import auth

auth_bp = Blueprint("auth", __name__)

# Endpoints acessíveis sem login.
PUBLICOS = {"auth.login", "auth.logout", "auth.setup", "static"}


# ── Guarda global ─────────────────────────────────────────────────────────────
def instalar_guarda(app):
    """Exige login para tudo, menos os endpoints públicos. Enquanto não houver
    nenhum usuário, redireciona para a tela de primeiro acesso."""
    @app.before_request
    def _guarda():
        ep = request.endpoint or ""
        if ep in PUBLICOS:
            return None
        if session.get("usuario"):
            return None
        if auth.contar_usuarios() == 0:
            return None if ep == "auth.setup" else redirect(url_for("auth.setup"))
        if "/api/" in request.path:
            return jsonify({"ok": False, "erro": "Não autenticado."}), 401
        return redirect(url_for("auth.login", next=request.path))


def login_obrigatorio(f):
    """Decorator opcional para proteger rotas específicas (a guarda global já
    cobre o sistema todo; útil se a guarda for desativada)."""
    @wraps(f)
    def wrap(*args, **kwargs):
        if not session.get("usuario"):
            if "/api/" in request.path:
                return jsonify({"ok": False, "erro": "Não autenticado."}), 401
            return redirect(url_for("auth.login", next=request.path))
        return f(*args, **kwargs)
    return wrap


def _destino_seguro(alvo):
    """Só aceita redirecionamento interno (evita open-redirect)."""
    if alvo and alvo.startswith("/") and not alvo.startswith("//"):
        return alvo
    return "/"


# ── Primeiro acesso ───────────────────────────────────────────────────────────
@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    if auth.contar_usuarios() > 0:
        return redirect(url_for("auth.login"))
    if request.method == "POST":
        usuario = request.form.get("usuario", "")
        senha   = request.form.get("senha", "")
        nome    = request.form.get("nome", "")
        ok, msg = auth.criar_usuario(usuario, senha, nome=nome, papel="admin",
                                     criado_por="setup")
        if ok:
            u = auth.verificar(usuario, senha)
            session.permanent = True
            session["usuario"] = u["usuario"]
            session["nome"] = u["nome"]
            return redirect("/")
        return render_template("sistema/setup.html", erro=msg)
    return render_template("sistema/setup.html", erro=None)


# ── Login / logout ────────────────────────────────────────────────────────────
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if auth.contar_usuarios() == 0:
        return redirect(url_for("auth.setup"))
    if session.get("usuario"):
        return redirect("/")
    if request.method == "POST":
        usuario = request.form.get("usuario", "")
        senha   = request.form.get("senha", "")
        u = auth.verificar(usuario, senha)
        if u:
            session.permanent = True
            session["usuario"] = u["usuario"]
            session["nome"] = u["nome"]
            return redirect(_destino_seguro(request.form.get("next", "/")))
        return render_template("sistema/login.html", erro="Usuário ou senha inválidos.",
                               proximo=request.form.get("next", "/"))
    return render_template("sistema/login.html", erro=None,
                           proximo=request.args.get("next", "/"))


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("auth.login"))


# ── Gerenciamento de usuários ─────────────────────────────────────────────────
@auth_bp.route("/sistema/usuarios")
def usuarios():
    return render_template("sistema/usuarios.html",
                           usuarios=auth.listar_usuarios(),
                           atual=session.get("usuario"))


@auth_bp.route("/sistema/usuarios/api/criar", methods=["POST"])
def api_criar_usuario():
    d = request.get_json(silent=True) or {}
    ok, msg = auth.criar_usuario(
        d.get("usuario", ""), d.get("senha", ""),
        nome=d.get("nome", ""), papel=d.get("papel", "admin"),
        criado_por=session.get("usuario"))
    return jsonify({"ok": ok, "msg": msg})


@auth_bp.route("/sistema/usuarios/api/senha", methods=["POST"])
def api_alterar_senha():
    d = request.get_json(silent=True) or {}
    # cada um só altera a própria senha
    ok, msg = auth.alterar_senha(session.get("usuario"), d.get("senha", ""))
    return jsonify({"ok": ok, "msg": msg})


@auth_bp.route("/sistema/usuarios/api/excluir", methods=["POST"])
def api_excluir_usuario():
    d = request.get_json(silent=True) or {}
    alvo = (d.get("usuario") or "").strip().lower()
    if alvo == session.get("usuario"):
        return jsonify({"ok": False, "msg": "Você não pode excluir a si mesmo."})
    ok, msg = auth.excluir_usuario(alvo)
    return jsonify({"ok": ok, "msg": msg})
