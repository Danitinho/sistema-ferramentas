"""
scripts/catalogo_routes.py
Blueprint do catálogo de produtos (/catalogo): consulta por código de barras e
atualização do arquivo exportado do ERP.

A página (/catalogo/) é a consulta em lista: cola-se ou bipa-se uma lista de
códigos e sai nome, custo e venda de cada um. A ATUALIZAÇÃO do catálogo segue
num modal do /vencidos, que é onde ele nasceu.
"""
import os
import time

from flask import Blueprint, request, jsonify, session, render_template, send_file
from werkzeug.utils import secure_filename

from scripts import catalogo as cat

catalogo_bp = Blueprint("catalogo", __name__, url_prefix="/catalogo")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR_TEMP = os.path.join(BASE_DIR, "uploads")
EXTENSOES = (".txt", ".csv")


def _usuario():
    return session.get("usuario")


@catalogo_bp.route("/")
def index():
    return render_template("catalogo/index.html", status=cat.status(), maximo=cat.MAX_LISTA)


@catalogo_bp.route("/api/consultar", methods=["POST"])
def consultar_lista():
    """{codigos: [...]} -> um resultado por código, na ordem digitada."""
    codigos = (request.get_json(silent=True) or {}).get("codigos") or []
    if not isinstance(codigos, list):
        return jsonify({"ok": False, "erro": "codigos deve ser uma lista"}), 400
    try:
        return jsonify({"ok": True, **cat.consultar_lista(codigos)})
    except ValueError as e:
        return jsonify({"ok": False, "erro": str(e)}), 400


@catalogo_bp.route("/api/consultar/excel", methods=["POST"])
def consultar_lista_excel():
    """Mesmo corpo de /api/consultar; baixa o .xlsx (gerado em memória)."""
    codigos = (request.get_json(silent=True) or {}).get("codigos") or []
    if not isinstance(codigos, list) or not codigos:
        return jsonify({"ok": False, "erro": "informe os códigos"}), 400
    try:
        res = cat.consultar_lista(codigos)
    except ValueError as e:
        return jsonify({"ok": False, "erro": str(e)}), 400
    nome = f"consulta_catalogo_{time.strftime('%Y-%m-%d_%H%M')}.xlsx"
    return send_file(cat.gerar_excel_lista(res), as_attachment=True, download_name=nome,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@catalogo_bp.route("/api/produto")
def produto():
    """Consulta usada pelo formulário enquanto se digita/bipa o código."""
    return jsonify(cat.consultar(request.args.get("cb", "")))


@catalogo_bp.route("/api/status")
def status():
    return jsonify(cat.status())


@catalogo_bp.route("/api/importar", methods=["POST"])
def importar():
    """Recebe o TXT exportado do ERP e substitui o catálogo.

    O arquivo é gravado em uploads/ antes de ser lido (são ~16 MB; ler de um
    caminho permite detectar a codificação sem carregar tudo na memória) e
    apagado depois — o que vale a partir daí é o banco."""
    f = request.files.get("arquivo")
    if not f or not f.filename:
        return jsonify({"ok": False, "msg": "Nenhum arquivo recebido."}), 400
    nome = secure_filename(f.filename)
    if not nome.lower().endswith(EXTENSOES):
        return jsonify({"ok": False,
                        "msg": "Envie o arquivo .txt exportado do ERP."}), 400

    os.makedirs(DIR_TEMP, exist_ok=True)
    destino = os.path.join(DIR_TEMP, f"catalogo_{int(time.time())}_{nome}")
    f.save(destino)
    try:
        res = cat.importar(destino, arquivo=nome, usuario=_usuario())
    except cat.ErroPlanilha as e:
        return jsonify({"ok": False, "msg": str(e)}), 400
    except Exception as e:  # noqa: BLE001 — o motivo real ajuda mais que um 500
        return jsonify({"ok": False, "msg": f"Falha ao ler o arquivo: {e}"}), 400
    finally:
        try:
            os.remove(destino)
        except OSError:
            pass
    res["status"] = cat.status()
    return jsonify(res)
