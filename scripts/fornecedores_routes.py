"""
scripts/fornecedores_routes.py
Blueprint /fornecedores — APIs do cadastro central de fornecedores (sem página
própria por ora; alimenta o seletor buscar-ou-cadastrar dos outros módulos).

Sincronização com débitos: quando um fornecedor ganha CNPJ (no cadastro ou
depois), garantimos a linha correspondente em `empresas` no debitos.db —
invariante: fornecedor com CNPJ <=> empresa em débitos.
"""
from flask import Blueprint, request, jsonify, session
from scripts import fornecedores as fr

fornecedores_bp = Blueprint("fornecedores", __name__, url_prefix="/fornecedores")


def _usuario():
    return session.get("usuario")


def _garantir_empresa(cnpj, nome):
    """Projeção local no módulo de débitos ('CNPJ já cadastrado' é ok).
    Falha de débitos não impede o cadastro do fornecedor."""
    try:
        from scripts import debitos
        debitos.adicionar_empresa(cnpj, nome, usuario=_usuario())
    except Exception:
        pass


@fornecedores_bp.route("/api/buscar")
def api_buscar():
    q = request.args.get("q", "")
    return jsonify({"ok": True, "fornecedores": fr.listar(q=q, limite=30)})


@fornecedores_bp.route("/api/criar", methods=["POST"])
def api_criar():
    d = request.get_json() or {}
    ok, msg, f = fr.criar(d.get("nome", ""), cnpj=d.get("cnpj"), usuario=_usuario())
    if ok and f and f.get("cnpj"):
        _garantir_empresa(f["cnpj"], f["nome"])
    return jsonify({"ok": ok, "msg": msg, "fornecedor": f})


@fornecedores_bp.route("/api/<id_forn>/cnpj", methods=["POST"])
def api_definir_cnpj(id_forn):
    d = request.get_json() or {}
    ok, msg, f = fr.definir_cnpj(id_forn, d.get("cnpj", ""), usuario=_usuario())
    if ok and f:
        _garantir_empresa(f["cnpj"], f["nome"])
    return jsonify({"ok": ok, "msg": msg, "fornecedor": f})


@fornecedores_bp.route("/api/<id_forn>/nome", methods=["POST"])
def api_editar_nome(id_forn):
    d = request.get_json() or {}
    ok, msg, f = fr.editar_nome(id_forn, d.get("nome", ""), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg, "fornecedor": f})
