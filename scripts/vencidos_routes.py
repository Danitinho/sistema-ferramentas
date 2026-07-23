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
    ordem = request.args.get("ordem", v.ORDEM_PADRAO)
    if ordem not in v.ORDENS_VENCIDOS:
        ordem = v.ORDEM_PADRAO
    ordem_avisos = request.args.get("ordem_avisos", v.ORDEM_AVISOS_PADRAO)
    if ordem_avisos not in v.ORDENS_AVISOS:
        ordem_avisos = v.ORDEM_AVISOS_PADRAO
    vencidos = v.listar_vencidos(mes=(None if todos else mes), limite=limite, ordem=ordem)
    avisos   = v.listar_avisos(mes=(None if todos else mes), limite=limite, ordem=ordem_avisos)
    return render_template(
        "vencidos/index.html",
        resumo=v.resumo(None if todos else mes),
        vencidos=vencidos, avisos=avisos,
        tipos_baixa=v.TIPOS_BAIXA,
        meses=meses,
        mes_atual=mes,
        ordem_atual=ordem,
        ordem_avisos_atual=ordem_avisos,
        truncado=todos and (len(vencidos) >= LIM_TODOS or len(avisos) >= LIM_TODOS),
        lim_todos=LIM_TODOS,
        hoje=v._hoje(),
        rk_reincidencia=v.ranking_reincidencia(),
        rk_fornecedores=v.ranking_fornecedores(),
        rk_responsaveis=v.ranking_responsaveis(),
    )


# ── API — Avisos ──────────────────────────────────────────────────────────────
@vencidos_bp.route("/api/aviso", methods=["POST"])
def api_aviso():
    d = request.get_json() or {}
    ok, msg = v.registrar_aviso(
        produto=d.get("produto", ""), codigo_barras=d.get("codigo_barras", ""),
        quantidade=d.get("quantidade", 0), fornecedor=d.get("fornecedor", ""),
        fornecedor_id=d.get("fornecedor_id"),
        responsavel=d.get("responsavel", ""), data_vencimento=d.get("data_vencimento", ""),
        custo=d.get("custo"), venda=d.get("venda"),
        valor_promocional=d.get("valor_promocional"), obs=d.get("obs", ""),
        usuario=_usuario(),
    )
    return jsonify({"ok": ok, "msg": msg})


@vencidos_bp.route("/api/aviso/<id_aviso>/editar", methods=["POST"])
def api_edit_aviso(id_aviso):
    d = request.get_json() or {}
    ok, msg = v.editar_aviso(
        id_aviso, produto=d.get("produto", ""), quantidade=d.get("quantidade", 0),
        data_vencimento=d.get("data_vencimento", ""), codigo_barras=d.get("codigo_barras", ""),
        fornecedor=d.get("fornecedor", ""), fornecedor_id=d.get("fornecedor_id"),
        responsavel=d.get("responsavel", ""),
        custo=d.get("custo"), venda=d.get("venda"),
        valor_promocional=d.get("valor_promocional"), obs=d.get("obs", ""),
        usuario=_usuario())
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
        fornecedor_id=d.get("fornecedor_id"),
        custo=d.get("custo"), responsavel_entrega=d.get("responsavel_entrega", ""),
        obs=d.get("obs", ""), usuario=_usuario(),
    )
    return jsonify({"ok": ok, "msg": msg})


@vencidos_bp.route("/api/vencido/<id_vencido>/editar", methods=["POST"])
def api_edit_vencido(id_vencido):
    d = request.get_json() or {}
    ok, msg = v.editar_vencido(
        id_vencido, produto=d.get("produto", ""), quantidade=d.get("quantidade", 0),
        codigo_barras=d.get("codigo_barras", ""), fornecedor=d.get("fornecedor", ""),
        fornecedor_id=d.get("fornecedor_id"),
        custo=d.get("custo"), responsavel_entrega=d.get("responsavel_entrega", ""),
        foi_avisado=d.get("foi_avisado"), obs=d.get("obs", ""), usuario=_usuario())
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
