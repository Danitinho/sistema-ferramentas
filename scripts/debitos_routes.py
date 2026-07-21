"""
scripts/debitos_routes.py
Blueprint Flask do módulo de débitos e bonificações.
"""
from flask import Blueprint, render_template, request, jsonify, redirect, url_for, session
from scripts import debitos as db

debitos_bp = Blueprint("debitos", __name__, url_prefix="/debitos")


def _usuario():
    """Autor da ação para a auditoria (None enquanto não houver login)."""
    return session.get("usuario")


# ── Páginas ───────────────────────────────────────────────────────────────────

@debitos_bp.route("/")
def index():
    return render_template("debitos/debitos_index.html", empresas=db.resumo_empresas())


@debitos_bp.route("/empresa/<cnpj>")
def empresa(cnpj):
    emp = db.buscar_empresa(cnpj)
    if not emp:
        return redirect(url_for("debitos.index"))
    mes = request.args.get("mes", "")
    return render_template(
        "debitos/debitos_empresa.html",
        emp=emp,
        saldo=db.calcular_saldo(cnpj),
        debitos=db.listar_debitos(cnpj, mes=mes),
        creditos=db.listar_creditos(cnpj),
        tipos=db.TIPOS_PAGAMENTO,
        ref_label=db.REF_LABEL,
        meses=db.meses_debitos(cnpj),
        mes_atual=mes,
    )


# ── API — Empresas ────────────────────────────────────────────────────────────

@debitos_bp.route("/api/empresa", methods=["POST"])
def api_add_empresa():
    d = request.get_json()
    ok, msg = db.adicionar_empresa(d.get("cnpj", ""), d.get("razao_social", ""), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/empresa/<cnpj>", methods=["DELETE"])
def api_del_empresa(cnpj):
    ok, msg = db.excluir_empresa(cnpj, usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


# ── API — Débitos ─────────────────────────────────────────────────────────────

@debitos_bp.route("/api/debito/vencimento", methods=["POST"])
def api_add_vencimento():
    d = request.get_json()
    ok, msg = db.adicionar_debito_vencimento(
        cnpj=d.get("cnpj", ""), nf_numero=d.get("nf_numero", ""),
        valor_total=d.get("valor_total", 0), obs=d.get("obs", ""), usuario=_usuario(),
        periodo_tipo=d.get("periodo_tipo"), periodo_inicio=d.get("periodo_inicio"),
        periodo_fim=d.get("periodo_fim"),
    )
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/debito/rebaxa", methods=["POST"])  # typo legado — não renomear
def api_add_rebaxa():
    d = request.get_json()
    ok, msg = db.adicionar_debito_rebaxa(
        cnpj=d.get("cnpj", ""), produto=d.get("produto", ""),
        quantidade=d.get("quantidade", 0), valor_unit=d.get("valor_unit", 0),
        obs=d.get("obs", ""), usuario=_usuario(),
        periodo_tipo=d.get("periodo_tipo"), periodo_inicio=d.get("periodo_inicio"),
        periodo_fim=d.get("periodo_fim"),
    )
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/debito/<id_debito>/editar", methods=["POST"])
def api_edit_debito(id_debito):
    d = request.get_json() or {}
    ok, msg = db.editar_debito(
        id_debito,
        valor_total=d.get("valor_total"), nf_numero=d.get("nf_numero"),
        produto=d.get("produto"), quantidade=d.get("quantidade"),
        valor_unit=d.get("valor_unit"), obs=d.get("obs", ""),
        periodo_tipo=d.get("periodo_tipo"), periodo_inicio=d.get("periodo_inicio"),
        periodo_fim=d.get("periodo_fim"), usuario=_usuario(),
    )
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/debito/<id_debito>", methods=["DELETE"])
def api_del_debito(id_debito):
    ok, msg = db.excluir_debito(id_debito, usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


# ── API — Pagamentos (créditos: bonificações etc.) ───────────────────────────

@debitos_bp.route("/api/pagamento", methods=["POST"])
@debitos_bp.route("/api/bonificacao", methods=["POST"])  # alias legado
def api_add_pagamento():
    d = request.get_json() or {}
    # `referencia` é o campo novo; aceita `nf_numero` do frontend legado.
    referencia = d.get("referencia") or d.get("nf_numero") or ""
    ok, msg = db.adicionar_pagamento(
        cnpj=d.get("cnpj", ""), valor_total=d.get("valor_total", 0),
        tipo=d.get("tipo", "bonificacao"), referencia=referencia,
        obs=d.get("obs", ""), debito_id=d.get("debito_id"), usuario=_usuario(),
    )
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/pagamento/<id_pag>", methods=["DELETE"])
@debitos_bp.route("/api/bonificacao/<id_pag>", methods=["DELETE"])  # alias legado
def api_del_pagamento(id_pag):
    ok, msg = db.excluir_pagamento(id_pag, usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


# ── API — Alocações (quitação de débito por pagamento) ───────────────────────

@debitos_bp.route("/api/alocar", methods=["POST"])
def api_alocar():
    d = request.get_json() or {}
    ok, msg = db.alocar(d.get("pagamento_id", ""), d.get("debito_id", ""),
                        d.get("valor", 0), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/alocar/auto", methods=["POST"])
def api_alocar_auto():
    d = request.get_json() or {}
    ok, msg = db.alocar_automatico(d.get("pagamento_id", ""), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/desalocar", methods=["POST"])
def api_desalocar():
    d = request.get_json() or {}
    ok, msg = db.desalocar(d.get("alocacao_id", ""), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})
