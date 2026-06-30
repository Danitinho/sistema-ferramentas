"""
scripts/relatorios_routes.py
Blueprint Flask do módulo de Relatórios de Venda (Curva ABC).

Registre no app.py:
    from scripts.relatorios_routes import relatorios_bp
    app.register_blueprint(relatorios_bp)

No index.html, aponte o card "Relatórios de venda" para /relatorios.
"""
import os
import tempfile
from werkzeug.utils import secure_filename
from flask import (Blueprint, render_template, request, jsonify,
                   send_file, current_app)

from scripts import relatorios_vendas as rv

relatorios_bp = Blueprint("relatorios", __name__, url_prefix="/relatorios")


# ── Página ────────────────────────────────────────────────────────────────────
@relatorios_bp.route("/")
def index():
    return render_template("relatorios/index.html", status=rv.status())


# ── API — Status (para atualizar a tela sem recarregar) ───────────────────────
@relatorios_bp.route("/api/status")
def api_status():
    return jsonify(rv.status())


# ── API — Enviar PDFs para a pasta de entrada ─────────────────────────────────
@relatorios_bp.route("/api/upload", methods=["POST"])
def api_upload():
    arquivos = request.files.getlist("pdfs")
    if not arquivos:
        return jsonify({"ok": False, "msg": "Nenhum arquivo recebido."}), 400

    rv._garantir_pastas()
    salvos = 0
    for f in arquivos:
        if not f.filename:
            continue
        nome = secure_filename(f.filename)
        if not nome.lower().endswith(".pdf"):
            continue
        f.save(os.path.join(rv.DIR_ENTRADA, nome))
        salvos += 1

    if salvos == 0:
        return jsonify({"ok": False, "msg": "Nenhum PDF válido enviado."}), 400
    return jsonify({"ok": True, "salvos": salvos,
                    "msg": f"{salvos} PDF(s) enviados para a fila."})


# ── API — Processar a pasta de entrada ────────────────────────────────────────
@relatorios_bp.route("/api/processar", methods=["POST"])
def api_processar():
    relatorio = rv.processar_entrada()
    relatorio["ok"] = True
    relatorio["status"] = rv.status()
    return jsonify(relatorio)


# ── API — Consultar códigos de barras ─────────────────────────────────────────
@relatorios_bp.route("/api/consultar", methods=["POST"])
def api_consultar():
    d = request.get_json() or {}
    codigos = d.get("codigos", "")
    resultado = rv.consultar_codigos(codigos)
    if not resultado["meses"]:
        return jsonify({"ok": False,
                        "msg": "Nenhum relatório no banco ainda. Processe PDFs primeiro."})
    if not resultado["linhas"]:
        return jsonify({"ok": False, "msg": "Informe ao menos um código de barras."})
    resultado["ok"] = True
    return jsonify(resultado)


# ── API — Baixar Excel da consulta ────────────────────────────────────────────
@relatorios_bp.route("/api/consultar/excel", methods=["POST"])
def api_consultar_excel():
    d = request.get_json() or {}
    resultado = rv.consultar_codigos(d.get("codigos", ""))
    if not resultado["linhas"]:
        return jsonify({"ok": False, "msg": "Nada para exportar."}), 400

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    tmp.close()
    rv.gerar_excel_consulta(resultado, tmp.name)
    return send_file(tmp.name, as_attachment=True,
                     download_name="consulta_vendas.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
