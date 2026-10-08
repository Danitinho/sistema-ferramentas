"""
scripts/auth_routes.py
Blueprint de autenticação: login, logout, primeiro acesso (setup) e
gerenciamento de usuários. Também instala a guarda global de sessão.
"""
from functools import wraps
from flask import (Blueprint, render_template, request, redirect, url_for,
                   session, jsonify, g)
from scripts import auth

auth_bp = Blueprint("auth", __name__)

# Endpoints acessíveis sem login.
PUBLICOS = {"auth.login", "auth.logout", "auth.setup", "static"}

# Endpoints que NAO usam sessao de navegador porque tem autenticacao propria.
# Caso concreto: o programa de mesa da reclassificacao roda na maquina do
# operador e se identifica por token no cabecalho X-Token; a guarda de sessao
# devolveria 401 para ele. Nao ponha aqui rota que so tenha o nome do usuario
# como identificacao - o prefixo isenta da sessao, entao a rota precisa checar
# credencial por conta propria.
PREFIXOS_PUBLICOS = ("reclassificacao.api_", "agente.api_")

# ── Papéis ────────────────────────────────────────────────────────────────────
# Só o admin: contas e tudo que decide QUEM (ou que máquina) fala com o agente.
ENDPOINTS_ADMIN = {
    "auth.api_criar_usuario", "auth.api_excluir_usuario", "auth.api_acesso_agente",
    "agente.pareamento_aprovar", "agente.pareamento_recusar", "agente.maquina_revogar",
    "reclassificacao.revogar_operador",
}
# Precisa de ACESSO AO AGENTE: o que manda o robô digitar no RADGe ou mexe no
# controle dele. O painel /agente inteiro (menos a API da máquina, que tem
# token próprio) e, nos outros módulos, só as ações — ver a página não.
ENDPOINTS_AGENTE = {
    "reclassificacao.agente_comando", "reclassificacao.agente_parametros",
    "reclassificacao.liberar", "reclassificacao.devolver", "reclassificacao.erp_mapa",
    "vencidos.rodada_abrir", "vencidos.rodada_incluir",
}


def permissoes_atuais():
    """{papel, admin, agente} de quem está logado (cache por requisição)."""
    if "_perm" not in g:
        u = session.get("usuario")
        g._perm = auth.permissoes(u) if u else {"papel": "", "admin": False, "agente": False}
    return g._perm


def _exigencia(ep):
    if ep in ENDPOINTS_ADMIN:
        return "admin"
    if ep in ENDPOINTS_AGENTE or (ep.startswith("agente.") and ".api_" not in ep):
        return "agente"
    return None


def _negar(exige):
    msg = ("Só o administrador pode fazer isso." if exige == "admin" else
           "Seu usuário não tem acesso ao agente do ERP. Peça ao administrador.")
    if "/api/" in request.path or request.is_json:
        return jsonify({"ok": False, "erro": msg, "msg": msg}), 403
    return render_template("sistema/sem_acesso.html", msg=msg), 403


# ── Guarda global ─────────────────────────────────────────────────────────────
def instalar_guarda(app):
    """Exige login para tudo, menos os endpoints públicos. Enquanto não houver
    nenhum usuário, redireciona para a tela de primeiro acesso."""
    @app.before_request
    def _guarda():
        ep = request.endpoint or ""
        if ep in PUBLICOS:
            return None
        if ep.startswith(PREFIXOS_PUBLICOS):
            return None
        if session.get("usuario"):
            exige = _exigencia(ep)
            if exige:
                try:
                    perm = permissoes_atuais()
                except Exception:          # sem como conferir = não libera
                    perm = {"admin": False, "agente": False}
                if not perm.get(exige):
                    return _negar(exige)
            return None
        if auth.contar_usuarios() == 0:
            return None if ep == "auth.setup" else redirect(url_for("auth.setup"))
        if "/api/" in request.path:
            return jsonify({"ok": False, "erro": "Não autenticado."}), 401
        return redirect(url_for("auth.login", next=request.path))

    @app.context_processor
    def _perm_nos_templates():
        # `perm.admin` / `perm.agente` para esconder o que o usuário não pode
        # usar. Esconder é conforto; quem barra de verdade é a guarda acima.
        try:
            return {"perm": permissoes_atuais()}
        except Exception:
            return {"perm": {"papel": "", "admin": False, "agente": False}}


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
    # Toda conta nova é `usuario`: o único admin é definido pela migração
    # (auth.ADMIN_INICIAL), não pela tela.
    ok, msg = auth.criar_usuario(
        d.get("usuario", ""), d.get("senha", ""),
        nome=d.get("nome", ""), papel="usuario",
        criado_por=session.get("usuario"))
    if ok and d.get("acesso_agente"):
        auth.definir_acesso_agente(d.get("usuario", ""), True, por=session.get("usuario"))
    return jsonify({"ok": ok, "msg": msg})


@auth_bp.route("/sistema/usuarios/api/acesso-agente", methods=["POST"])
def api_acesso_agente():
    d = request.get_json(silent=True) or {}
    ok, msg = auth.definir_acesso_agente(d.get("usuario", ""), bool(d.get("permitir")),
                                         por=session.get("usuario"))
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
    ok, msg = auth.excluir_usuario(alvo, por=session.get("usuario"))
    return jsonify({"ok": ok, "msg": msg})
