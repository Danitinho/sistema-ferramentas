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


def _sincronizar_empresa(antes, depois):
    """Reflete a mudança do fornecedor na projeção `empresas` de débitos:
    cria a empresa quando o fornecedor ganha CNPJ, renomeia e/ou migra a chave
    quando CNPJ/razão social mudam. `antes` pode ser None (fornecedor novo).
    Falha de débitos não impede a mudança no cadastro central."""
    if not depois or not depois.get("cnpj"):
        return
    try:
        from scripts import debitos
        cnpj_antigo = (antes or {}).get("cnpj")
        if cnpj_antigo and debitos.buscar_empresa(cnpj_antigo):
            debitos.editar_empresa(cnpj_antigo, novo_cnpj=depois["cnpj"],
                                   nova_razao=depois["nome"], usuario=_usuario())
        else:
            debitos.adicionar_empresa(depois["cnpj"], depois["nome"], usuario=_usuario())
    except Exception:
        pass


def _sincronizar_vencidos(antes, depois):
    """Propaga o nome novo para as linhas de avisos/vencidos vinculadas."""
    if not antes or not depois or antes.get("nome") == depois.get("nome"):
        return
    try:
        from scripts import vencidos
        vencidos.renomear_fornecedor(depois["id"], depois["nome"], usuario=_usuario())
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
    if ok:
        _sincronizar_empresa(None, f)
    return jsonify({"ok": ok, "msg": msg, "fornecedor": f})


@fornecedores_bp.route("/api/<id_forn>/cnpj", methods=["POST"])
def api_definir_cnpj(id_forn):
    d = request.get_json() or {}
    antes = fr.buscar(id_forn)
    ok, msg, f = fr.definir_cnpj(id_forn, d.get("cnpj", ""), usuario=_usuario())
    if ok:
        _sincronizar_empresa(antes, f)
    return jsonify({"ok": ok, "msg": msg, "fornecedor": f})


@fornecedores_bp.route("/api/<id_forn>/nome", methods=["POST"])
def api_editar_nome(id_forn):
    d = request.get_json() or {}
    antes = fr.buscar(id_forn)
    ok, msg, f = fr.editar_nome(id_forn, d.get("nome", ""), usuario=_usuario())
    if ok:
        _sincronizar_empresa(antes, f)
        _sincronizar_vencidos(antes, f)
    return jsonify({"ok": ok, "msg": msg, "fornecedor": f})


@fornecedores_bp.route("/api/<id_forn>/editar", methods=["POST"])
def api_editar(id_forn):
    """Edita nome e/ou CNPJ num passo só (troca de razão social / CNPJ da
    empresa). Sincroniza débitos (migra a chave se o CNPJ mudou) e vencidos."""
    d = request.get_json() or {}
    antes = fr.buscar(id_forn)
    if not antes:
        return jsonify({"ok": False, "msg": "Fornecedor não encontrado."})
    nome = (d.get("nome") or "").strip()
    cnpj = (d.get("cnpj") or "").strip()
    if (not nome or nome == antes["nome"]) and (not cnpj or cnpj == (antes["cnpj"] or "")):
        return jsonify({"ok": True, "msg": "Nada a alterar.", "fornecedor": antes})
    if nome and nome != antes["nome"]:
        ok, msg, _f = fr.editar_nome(id_forn, nome, usuario=_usuario())
        if not ok:
            return jsonify({"ok": False, "msg": msg})
    if cnpj and cnpj != (antes["cnpj"] or ""):
        ok, msg, _f = fr.definir_cnpj(id_forn, cnpj, usuario=_usuario())
        if not ok:
            return jsonify({"ok": False, "msg": msg})
    depois = fr.buscar(id_forn)
    _sincronizar_empresa(antes, depois)
    _sincronizar_vencidos(antes, depois)
    return jsonify({"ok": True, "msg": "Fornecedor atualizado.", "fornecedor": depois})
