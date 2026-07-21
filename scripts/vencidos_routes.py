"""
scripts/vencidos_routes.py
Blueprint do módulo de vencidos (/vencidos): avisos, vencidos e baixa.
"""
from flask import Blueprint, render_template, request, jsonify, session
from scripts import vencidos as v

vencidos_bp = Blueprint("vencidos", __name__, url_prefix="/vencidos")


def _usuario():
    return session.get("usuario")


LIM_TODOS = 500   # "Todos os meses" mostra só os mais recentes (evita página gigante)


@vencidos_bp.route("/")
def index():
    meses = v.meses_disponiveis()
    # Sem ?mes: abre no mês mais recente com dados (ou no mês atual). ?mes= vazio = Todos.
    mes = request.args.get("mes")
    if mes is None:
        mes = meses[0]["mes"] if meses else v._hoje()[:7]
    todos = (mes == "")
    limite = LIM_TODOS if todos else 5000
    vencidos = v.listar_vencidos(mes=(None if todos else mes), limite=limite)
    avisos   = v.listar_avisos(mes=(None if todos else mes), limite=limite)
    return render_template(
        "vencidos/index.html",
        resumo=v.resumo(None if todos else mes),
        vencidos=vencidos, avisos=avisos,
        tipos_baixa=v.TIPOS_BAIXA,
        meses=meses,
        mes_atual=mes,
        truncado=todos and (len(vencidos) >= LIM_TODOS or len(avisos) >= LIM_TODOS),
        lim_todos=LIM_TODOS,
        hoje=v._hoje(),
    )


# ── API — Avisos ──────────────────────────────────────────────────────────────
@vencidos_bp.route("/api/aviso", methods=["POST"])
def api_aviso():
    d = request.get_json() or {}
    ok, msg = v.registrar_aviso(
        produto=d.get("produto", ""), codigo_barras=d.get("codigo_barras", ""),
        quantidade=d.get("quantidade", 0), fornecedor=d.get("fornecedor", ""),
        responsavel=d.get("responsavel", ""), data_vencimento=d.get("data_vencimento", ""),
        custo=d.get("custo"), venda=d.get("venda"),
        valor_promocional=d.get("valor_promocional"), obs=d.get("obs", ""),
        usuario=_usuario(),
    )
    return jsonify({"ok": ok, "msg": msg})


@vencidos_bp.route("/api/aviso/<id_aviso>", methods=["DELETE"])
def api_del_aviso(id_aviso):
    ok, msg = v.excluir_aviso(id_aviso, usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


# ── API — Vencidos ────────────────────────────────────────────────────────────
@vencidos_bp.route("/api/checar-aviso", methods=["POST"])
def api_checar_aviso():
    d = request.get_json() or {}
    return jsonify(v.checar_aviso(d.get("codigo_barras", "")))


@vencidos_bp.route("/api/vencido", methods=["POST"])
def api_vencido():
    d = request.get_json() or {}
    ok, msg = v.registrar_vencido(
        produto=d.get("produto", ""), codigo_barras=d.get("codigo_barras", ""),
        quantidade=d.get("quantidade", 0), fornecedor=d.get("fornecedor", ""),
        custo=d.get("custo"), responsavel_entrega=d.get("responsavel_entrega", ""),
        obs=d.get("obs", ""), usuario=_usuario(),
    )
    return jsonify({"ok": ok, "msg": msg})


@vencidos_bp.route("/api/vencido/<id_vencido>/baixa", methods=["POST"])
def api_baixa(id_vencido):
    d = request.get_json() or {}
    ok, msg = v.dar_baixa(id_vencido, d.get("tipo", ""), d.get("referencia", ""),
                          usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


@vencidos_bp.route("/api/vencido/<id_vencido>/reabrir", methods=["POST"])
def api_reabrir(id_vencido):
    ok, msg = v.reabrir_baixa(id_vencido, usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


@vencidos_bp.route("/api/vencido/<id_vencido>", methods=["DELETE"])
def api_del_vencido(id_vencido):
    ok, msg = v.excluir_vencido(id_vencido, usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})
