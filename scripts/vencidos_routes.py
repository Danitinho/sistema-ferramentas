"""
scripts/vencidos_routes.py
Blueprint do módulo de vencidos (/vencidos): avisos, vencidos e baixa.
"""
import io

from flask import Blueprint, render_template, request, jsonify, session, send_file
from scripts import vencidos as v
from scripts import vencidos_relatorio as rel
from scripts import vencidos_pdf as vpdf

vencidos_bp = Blueprint("vencidos", __name__, url_prefix="/vencidos")


def _usuario():
    return session.get("usuario")


LIM_TODOS = 500   # "Todos os meses" mostra só os mais recentes (evita página gigante)


def _mes_pedido():
    """Mês do filtro: sem ?mes abre no MÊS ATUAL; ?mes= vazio = todos os meses."""
    mes = request.args.get("mes")
    return v._hoje()[:7] if mes is None else mes


def _ordem_pedida():
    ordem = request.args.get("ordem", v.ORDEM_PADRAO)
    return ordem if ordem in v.ORDENS_VENCIDOS else v.ORDEM_PADRAO


@vencidos_bp.route("/")
def index():
    meses = v.meses_disponiveis()
    mes_hoje = v._hoje()[:7]
    mes = _mes_pedido()
    todos = (mes == "")
    # garante que o mês atual (e o selecionado) apareçam no seletor mesmo sem
    # dados — senão o mês vazio some da lista e o <select> fica sem opção.
    presentes = {m["mes"] for m in meses}
    for m in (mes_hoje, mes):
        if m and m not in presentes:
            meses.append({"mes": m, "rotulo": v.rotulo_mes(m)})
            presentes.add(m)
    meses.sort(key=lambda x: x["mes"], reverse=True)
    limite = LIM_TODOS if todos else 5000
    ordem = _ordem_pedida()
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


# ── Relatório ─────────────────────────────────────────────────────────────────
# Duas vistas do mesmo `montar_relatorio`: o PDF é o documento de apresentação
# (capa executiva + 5 partes) e o Excel é a planilha de trabalho (tabela plana
# com autofiltro). As duas ignoram o corte de tela do LIM_TODOS.
def _relatorio():
    return rel.montar_relatorio(_mes_pedido(), ordem=_ordem_pedida())


def _entregar(buf, nome, mimetype):
    # em memória: no Windows um temporário aberto pelo send_file não pode ser
    # apagado depois (ficaria lixo em %TEMP%)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=nome, mimetype=mimetype)


@vencidos_bp.route("/pdf")
def pdf():
    """Documento de apresentação, em PDF."""
    if not vpdf.DISPONIVEL:
        return ("<h3>Relatório em PDF indisponível</h3>"
                "<p>A biblioteca <code>reportlab</code> não está instalada neste "
                "servidor. Instale com o python do serviço:<br>"
                "<code>python -m pip install -r requirements.txt</code> "
                "e reinicie o serviço.</p>"
                f"<p><small>{vpdf.ERRO_IMPORT}</small></p>", 503)
    r = _relatorio()
    nome = "vencidos_todos.pdf" if r["todos_meses"] else f"vencidos_{r['mes']}.pdf"
    buf = io.BytesIO()
    vpdf.gerar_pdf_relatorio(r, buf)
    return _entregar(buf, nome, "application/pdf")


@vencidos_bp.route("/excel")
def excel():
    """Planilha de trabalho: uma aba por parte, tabela plana com autofiltro."""
    r = _relatorio()
    nome = "vencidos_todos.xlsx" if r["todos_meses"] else f"vencidos_{r['mes']}.xlsx"
    buf = io.BytesIO()
    rel.gerar_excel_vencidos(r, buf)
    return _entregar(
        buf, nome,
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


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
