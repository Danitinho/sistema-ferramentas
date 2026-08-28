"""
scripts/debitos_routes.py
Blueprint Flask do módulo de débitos e bonificações.
"""
import io

from flask import (Blueprint, render_template, request, jsonify, redirect,
                   url_for, session, send_file)
from scripts import debitos as db
from scripts import debitos_relatorio as rel
from scripts import debitos_pdf as dpdf

debitos_bp = Blueprint("debitos", __name__, url_prefix="/debitos")


def _usuario():
    """Autor da ação para a auditoria (None enquanto não houver login)."""
    return session.get("usuario")


# ── Páginas ───────────────────────────────────────────────────────────────────

@debitos_bp.route("/")
def index():
    return render_template("debitos/debitos_index.html", empresas=db.resumo_empresas())


@debitos_bp.route("/empresa/<path:cnpj>")   # path: o CNPJ contém '/'
def empresa(cnpj):
    emp = db.buscar_empresa(cnpj)
    if not emp:
        return redirect(url_for("debitos.index"))
    mes = request.args.get("mes", "")
    vend = request.args.get("vendedor", "")
    return render_template(
        "debitos/debitos_empresa.html",
        emp=emp,
        saldo=db.calcular_saldo(cnpj, vendedor=vend),
        debitos=db.listar_debitos(cnpj, mes=mes, vendedor=vend),
        creditos=db.listar_creditos(cnpj, vendedor=vend),
        tipos=db.TIPOS_PAGAMENTO,
        ref_label=db.REF_LABEL,
        meses=db.meses_debitos(cnpj),
        mes_atual=mes,
        vendedores=db.vendedores_empresa(cnpj),
        vendedor_atual=vend,
    )


@debitos_bp.route("/relatorio")
def relatorio():
    """Fechamento do mês: débitos do mês + do mês anterior (que são pagos ao
    longo dele) + os pagamentos que abateram uns e outros."""
    r = rel.montar_relatorio(request.args.get("mes"), request.args.get("cnpj"))
    return render_template(
        "debitos/debitos_relatorio.html",
        rel=r,
        empresas=db.listar_empresas(),
        meses=rel.meses_disponiveis(),
    )


# Duas vistas do mesmo `montar_relatorio`: o PDF é o documento de apresentação
# (capa executiva + as 3 partes) e o Excel é a planilha de análise (4 abas
# planas com autofiltro).
def _nome_arquivo(r, ext):
    sufixo = ("" if r["todas_empresas"]
              else "_" + "".join(c for c in r["escopo"][:20] if c.isalnum()))
    return f"debitos_{r['mes']}{sufixo}.{ext}"


def _entregar(buf, nome, mimetype):
    # Em memória: no Windows um temporário aberto pelo send_file não pode ser
    # apagado depois (ficaria lixo em %TEMP%).
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=nome, mimetype=mimetype)


@debitos_bp.route("/relatorio/pdf")
def relatorio_pdf():
    """Documento de apresentação, em PDF."""
    if not dpdf.DISPONIVEL:
        return ("<h3>Relatório em PDF indisponível</h3>"
                "<p>A biblioteca <code>reportlab</code> não está instalada neste "
                "servidor. Instale com o python do serviço:<br>"
                "<code>python -m pip install -r requirements.txt</code> "
                "e reinicie o serviço.</p>"
                f"<p><small>{dpdf.ERRO_IMPORT}</small></p>", 503)
    r = rel.montar_relatorio(request.args.get("mes"), request.args.get("cnpj"))
    buf = io.BytesIO()
    dpdf.gerar_pdf_relatorio(r, buf)
    return _entregar(buf, _nome_arquivo(r, "pdf"), "application/pdf")


@debitos_bp.route("/relatorio/excel")
def relatorio_excel():
    """Planilha de análise: débitos, pagamentos aplicados, resumo e crédito."""
    r = rel.montar_relatorio(request.args.get("mes"), request.args.get("cnpj"))
    buf = io.BytesIO()
    rel.gerar_excel_relatorio(r, buf)
    return _entregar(
        buf, _nome_arquivo(r, "xlsx"),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ── API — Empresas ────────────────────────────────────────────────────────────

@debitos_bp.route("/api/empresa", methods=["POST"])
def api_add_empresa():
    d = request.get_json()
    ok, msg = db.adicionar_empresa(d.get("cnpj", ""), d.get("razao_social", ""), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


@debitos_bp.route("/api/empresa/<path:cnpj>", methods=["DELETE"])   # path: o CNPJ contém '/'
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
        periodo_fim=d.get("periodo_fim"), vendedor=d.get("vendedor", ""),
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
        periodo_fim=d.get("periodo_fim"), vendedor=d.get("vendedor", ""),
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
        vendedor=d.get("vendedor", ""),
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
        vendedor=d.get("vendedor", ""),
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
