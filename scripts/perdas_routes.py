"""
scripts/perdas_routes.py
Blueprint das perdas da loja (/perdas): registro, painel do mês e o
acompanhamento do lançamento no ERP.
"""
from flask import Blueprint, render_template, request, jsonify, session, send_file

from scripts import perdas as pd

perdas_bp = Blueprint("perdas", __name__, url_prefix="/perdas")


def _usuario():
    return session.get("usuario")


def _mes():
    mes = request.args.get("mes") or pd._hoje()[:7]
    return mes if pd._MES.match(mes) else pd._hoje()[:7]


@perdas_bp.route("/")
def index():
    mes = _mes()
    lanc = request.args.get("lanc") or ""
    motivo = request.args.get("motivo") or ""
    return render_template(
        "perdas/index.html", mes=mes, meses=pd.meses_disponiveis(), painel=pd.painel(mes),
        perdas=pd.listar(mes=mes, motivo=motivo or None, lanc=lanc or None),
        motivos=pd.MOTIVOS, lanc=lanc, motivo=motivo, hoje=pd._hoje())


@perdas_bp.route("/api/produto")
def api_produto():
    """Bipou o código: o que o catálogo sabe dele (nome, custo, venda, dep)."""
    r = pd.produto_do_catalogo(request.args.get("cb", ""))
    if r.get("encontrado"):
        r["dep_nome"] = pd.nome_dep(r.get("dep"))
    return jsonify(r)


@perdas_bp.route("/api/perda", methods=["POST"])
def api_registrar():
    d = request.get_json(silent=True) or {}
    ok, msg, pid = pd.registrar(
        d.get("produto"), d.get("quantidade"), d.get("motivo"), data=d.get("data"),
        codigo_barras=d.get("codigo_barras"), custo=d.get("custo"), venda=d.get("venda"),
        responsavel=d.get("responsavel"), obs=d.get("obs"), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg, "id": pid})


@perdas_bp.route("/api/perda/<pid>", methods=["DELETE"])
def api_excluir(pid):
    ok, msg = pd.excluir(pid, usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


@perdas_bp.route("/api/lancadas", methods=["POST"])
def api_lancadas():
    d = request.get_json(silent=True) or {}
    ok, msg, n = pd.marcar_lancadas(d.get("ids"), d.get("referencia", ""), usuario=_usuario(),
                                    desfazer=bool(d.get("desfazer")))
    return jsonify({"ok": ok, "msg": msg, "n": n})


@perdas_bp.route("/excel")
def excel():
    """As perdas do mês (com o filtro de situação da tela) para lançar no ERP."""
    mes = _mes()
    lanc = request.args.get("lanc") or None
    lista = pd.listar(mes=mes, lanc=lanc, limite=100000)
    situacao = {"pendente": "pendentes de lançar", "lancada": "já lançadas"}.get(lanc, "todas")
    buf = pd.gerar_excel(lista, f"Perdas da loja · {pd.rotulo_mes(mes)} · {situacao}"
                                f" · gerado em {pd._fmt_dt(pd._agora())}")
    sufixo = f"_{lanc}" if lanc else ""
    return send_file(buf, as_attachment=True, download_name=f"perdas_{mes}{sufixo}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
