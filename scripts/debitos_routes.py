"""
scripts/debitos_routes.py
Blueprint Flask do módulo de débitos.

Registre no app.py:
    from scripts.debitos_routes import debitos_bp
    app.register_blueprint(debitos_bp)
"""
from flask import Blueprint, render_template, request, jsonify, redirect, url_for
from scripts import debitos as db

debitos_bp = Blueprint("debitos", __name__, url_prefix="/debitos")


# ── Páginas ───────────────────────────────────────────────────────────────────

@debitos_bp.route("/")
def index():
    empresas = db.resumo_empresas()
    return render_template("debitos/debitos_index.html", empresas=empresas)


@debitos_bp.route("/empresa/<cnpj>")
def empresa(cnpj):
    emp = db.buscar_empresa(cnpj)
    if not emp:
        return redirect(url_for("debitos.index"))
    saldo        = db.calcular_saldo(cnpj)
    debitos      = sorted(db.listar_debitos(cnpj),      key=lambda x: x["data"], reverse=True)
    bonificacoes = sorted(db.listar_bonificacoes(cnpj), key=lambda x: x["data"], reverse=True)
    return render_template("debitos/debitos_empresa.html",
                           emp=emp, saldo=saldo,
                           debitos=debitos, bonificacoes=bonificacoes)


# ── API — Empresas ────────────────────────────────────────────────────────────

@debitos_bp.route("/api/empresa", methods=["POST"])
def api_add_empresa():
    data = request.get_json()
    ok, msg = db.adicionar_empresa(data.get("cnpj", ""), data.get("razao_social", ""))
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/empresa/<cnpj>", methods=["DELETE"])
def api_del_empresa(cnpj):
    ok, msg = db.excluir_empresa(cnpj)
    return jsonify({"ok": ok, "msg": msg})


# ── API — Débitos ─────────────────────────────────────────────────────────────

@debitos_bp.route("/api/debito/vencimento", methods=["POST"])
def api_add_vencimento():
    d = request.get_json()
    ok, msg = db.adicionar_debito_vencimento(
        cnpj        = d.get("cnpj", ""),
        nf_numero   = d.get("nf_numero", ""),
        valor_total = d.get("valor_total", 0),
        obs         = d.get("obs", ""),
    )
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/debito/rebaxa", methods=["POST"])
def api_add_rebaxa():
    d = request.get_json()
    ok, msg = db.adicionar_debito_rebaxa(
        cnpj       = d.get("cnpj", ""),
        produto    = d.get("produto", ""),
        quantidade = d.get("quantidade", 0),
        valor_unit = d.get("valor_unit", 0),
        obs        = d.get("obs", ""),
    )
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/debito/<id_debito>", methods=["DELETE"])
def api_del_debito(id_debito):
    ok, msg = db.excluir_debito(id_debito)
    return jsonify({"ok": ok, "msg": msg})


# ── API — Bonificações ────────────────────────────────────────────────────────

@debitos_bp.route("/api/bonificacao", methods=["POST"])
def api_add_bonificacao():
    d = request.get_json()
    ok, msg = db.adicionar_bonificacao(
        cnpj        = d.get("cnpj", ""),
        nf_numero   = d.get("nf_numero", ""),
        valor_total = d.get("valor_total", 0),
        obs         = d.get("obs", ""),
    )
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/bonificacao/<id_bonif>", methods=["DELETE"])
def api_del_bonificacao(id_bonif):
    ok, msg = db.excluir_bonificacao(id_bonif)
    return jsonify({"ok": ok, "msg": msg})
