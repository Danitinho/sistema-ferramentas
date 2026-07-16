"""
scripts/vencidos_routes.py
Blueprint do módulo de produtos vencidos / perdas (/vencidos).
"""
from flask import Blueprint, render_template, request, jsonify, session
from scripts import vencidos as v

vencidos_bp = Blueprint("vencidos", __name__, url_prefix="/vencidos")


def _usuario():
    return session.get("usuario")


@vencidos_bp.route("/")
def index():
    inicio = request.args.get("inicio", "")
    fim    = request.args.get("fim", "")
    motivo = request.args.get("motivo", "")
    return render_template(
        "vencidos/index.html",
        registros=v.listar(inicio, fim, motivo),
        resumo=v.resumo(inicio, fim, motivo),
        motivos=v.MOTIVOS,
        filtro={"inicio": inicio, "fim": fim, "motivo": motivo},
        hoje=v._hoje(),
    )


@vencidos_bp.route("/api/registrar", methods=["POST"])
def api_registrar():
    d = request.get_json() or {}
    ok, msg = v.registrar(
        produto=d.get("produto", ""), quantidade=d.get("quantidade", 0),
        motivo=d.get("motivo", ""), codigo_barras=d.get("codigo_barras", ""),
        valor_unit=d.get("valor_unit"), data=d.get("data"),
        obs=d.get("obs", ""), usuario=_usuario(),
    )
    return jsonify({"ok": ok, "msg": msg})


@vencidos_bp.route("/api/<id_registro>", methods=["DELETE"])
def api_excluir(id_registro):
    ok, msg = v.excluir(id_registro, usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})
