"""
scripts/layouts_routes.py
Blueprint Flask para gerenciamento e geração de layouts.

Registre no app.py:
    from scripts.layouts_routes import layouts_bp
    app.register_blueprint(layouts_bp)

Adicione ao index.html um card apontando para /layouts
"""
import os
import shutil
import json
from flask import (Blueprint, render_template, request, jsonify,
                   send_file, redirect, url_for)
from scripts import gerador_layouts as gl
from scripts import gira_imagem, gera_pdf

layouts_bp = Blueprint("layouts", __name__, url_prefix="/layouts")

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _pasta_saida():
    return os.path.join(OUTPUT_FOLDER, "ofertas_geradas")


def _limpar_pasta(pasta):
    if os.path.exists(pasta):
        shutil.rmtree(pasta)
    os.makedirs(pasta, exist_ok=True)


# ── Listagem ──────────────────────────────────────────────────────────────────

@layouts_bp.route("/")
def index():
    layouts = gl.listar_layouts()
    return render_template("layouts/index.html", layouts=layouts)


# ── Cadastrar novo layout ─────────────────────────────────────────────────────

@layouts_bp.route("/cadastrar")
def cadastrar():
    return render_template("layouts/cadastrar.html",
                            layout=None, layout_id=None)


@layouts_bp.route("/cadastrar", methods=["POST"])
def cadastrar_post():
    dados = request.get_json()
    if not dados:
        return jsonify({"ok": False, "msg": "Dados inválidos."}), 400

    # Salva imagem base enviada (se houver)
    imagem_base_nome = dados.get("imagem_base", "")

    layout_id = gl.salvar_layout(dados)
    return jsonify({"ok": True, "id": layout_id,
                    "redirect": url_for("layouts.index")})


# ── Listar fontes disponíveis ─────────────────────────────────────────────────

@layouts_bp.route("/fontes")
def listar_fontes():
    exts = {".ttf", ".otf", ".TTF", ".OTF"}
    fontes = []
    for f in sorted(os.listdir(gl.ASSETS_DIR)):
        if os.path.splitext(f)[1] in exts:
            fontes.append({"nome": f, "path": f"assets/{f}"})
    return jsonify(fontes)


# ── Upload da imagem base ─────────────────────────────────────────────────────

@layouts_bp.route("/upload-imagem", methods=["POST"])
def upload_imagem():
    """Recebe a imagem base e salva em assets/. Retorna o nome do arquivo."""
    arquivo = request.files.get("imagem")
    if not arquivo or arquivo.filename == "":
        return jsonify({"ok": False, "msg": "Nenhum arquivo enviado."}), 400

    ext      = os.path.splitext(arquivo.filename)[1].lower()
    nome     = f"layout_{os.urandom(4).hex()}{ext}"
    destino  = os.path.join(gl.ASSETS_DIR, nome)
    arquivo.save(destino)
    return jsonify({"ok": True, "nome": nome})


# ── Editar layout existente ───────────────────────────────────────────────────

@layouts_bp.route("/<layout_id>/editar")
def editar(layout_id):
    layout = gl.carregar_layout(layout_id)
    if not layout:
        return redirect(url_for("layouts.index"))
    return render_template("layouts/cadastrar.html",
                            layout=layout, layout_id=layout_id)


@layouts_bp.route("/<layout_id>/editar", methods=["POST"])
def editar_post(layout_id):
    dados = request.get_json()
    if not dados:
        return jsonify({"ok": False, "msg": "Dados inválidos."}), 400
    gl.salvar_layout(dados, layout_id)
    return jsonify({"ok": True, "redirect": url_for("layouts.index")})


# ── Excluir layout ────────────────────────────────────────────────────────────

@layouts_bp.route("/<layout_id>/excluir", methods=["POST"])
def excluir(layout_id):
    ok = gl.excluir_layout(layout_id)
    return jsonify({"ok": ok})


# ── Página de geração ─────────────────────────────────────────────────────────

@layouts_bp.route("/<layout_id>/gerar")
def gerar_pagina(layout_id):
    layout = gl.carregar_layout(layout_id)
    if not layout:
        return redirect(url_for("layouts.index"))
    formato = gl.formato_txt(layout)
    return render_template("layouts/gerar.html",
                            layout=layout, layout_id=layout_id,
                            formato=formato)


@layouts_bp.route("/<layout_id>/gerar", methods=["POST"])
def gerar_post(layout_id):
    layout = gl.carregar_layout(layout_id)
    if not layout:
        return jsonify({"mensagem": "Layout não encontrado."}), 404

    pdf_layout = int(request.form.get("pdf_layout", 4))
    pasta      = _pasta_saida()
    _limpar_pasta(pasta)

    # ── Origem dos dados: arquivo txt ou formulário manual ────────────────────
    caminho_txt = os.path.join(UPLOAD_FOLDER, "produtos_layout.txt")
    arquivo     = request.files.get("produtos")

    if arquivo and arquivo.filename != "":
        arquivo.save(caminho_txt)
    else:
        # Monta txt a partir dos campos do formulário
        # Campos pré-preenchidos não aparecem no formulário — excluir do parsing
        campos = [c for c in layout.get("campos", [])
                  if not c.get("valor_fixo", "").strip()]
        linhas = []

        # Quantas linhas (produtos) foram enviadas?
        primeiro_campo_id = campos[0]["id"] if campos else ""
        qtd_linhas = len(request.form.getlist(f"{primeiro_campo_id}[]"))

        for i in range(qtd_linhas):
            partes = []
            for campo in campos:
                cid    = campo["id"]
                valores = request.form.getlist(f"{cid}[]")
                partes.append(valores[i] if i < len(valores) else "")
            linha = ":".join(partes)
            if linha.replace(":", "").strip():  # ignora linhas completamente vazias
                linhas.append(linha)

        if not linhas:
            return jsonify({"mensagem": "Nenhum produto informado."}), 400

        with open(caminho_txt, "w", encoding="utf-8") as f:
            f.write("\n".join(linhas) + "\n")

    # ── Imagem base ───────────────────────────────────────────────────────────
    from flask import current_app
    caminho_img = os.path.join(current_app.root_path, "assets",
                                layout.get("imagem_base", ""))

    # ── Geração das imagens ───────────────────────────────────────────────────
    sucesso, falha = gl.processar_lote(
        layout       = layout,
        arquivo_lista = caminho_txt,
        pasta_saida  = pasta,
        arquivo_base  = caminho_img,
    )

    if sucesso == 0:
        return jsonify({"mensagem": "Nenhuma imagem gerada. Verifique o formato."}), 400

    # ── Rotação automática por proporção da imagem ────────────────────────────
    # Layout 1 e 4: espaço retrato → rotaciona imagens horizontais
    # Layout 2: espaço paisagem → rotaciona imagens verticais
    from PIL import Image as _PIL
    try:
        with _PIL.open(caminho_img) as _im:
            img_horizontal = _im.width > _im.height
    except Exception:
        img_horizontal = False

    deve_rotacionar = (pdf_layout in (1, 4) and img_horizontal) or \
                      (pdf_layout == 2 and not img_horizontal)

    if deve_rotacionar:
        gira_imagem.girar_e_adicionar_borda(
            pasta, angulo=90, espessura_borda=2, cor_borda="black"
        )

    # ── PDF ───────────────────────────────────────────────────────────────────
    from flask import current_app
    caminho_pdf = os.path.join(current_app.root_path, OUTPUT_FOLDER,
                                "ofertas_final.pdf")
    gera_pdf.gerar_pdf_preenchimento_total(
        pasta_entrada=pasta,
        nome_saida=caminho_pdf,
        layout=pdf_layout
    )

    # ── Conta páginas do PDF gerado ───────────────────────────────────────────
    try:
        import pikepdf
        with pikepdf.open(caminho_pdf) as _pdf:
            num_paginas = len(_pdf.pages)
    except Exception:
        num_paginas = sucesso  # fallback: nº de imagens geradas

    nome_arquivo = os.path.basename(caminho_pdf)
    return jsonify({
        "ok":        True,
        "pdf_url":   f"/layouts/pdf/{nome_arquivo}",
        "paginas":   num_paginas,
        "geradas":   sucesso,
        "falhas":    falha,
    })


# ── Servir PDF para prévia no navegador ───────────────────────────────────────

@layouts_bp.route("/pdf/<filename>")
def servir_pdf(filename):
    from flask import current_app
    pasta = os.path.join(current_app.root_path, OUTPUT_FOLDER)
    return send_file(os.path.join(pasta, filename), mimetype="application/pdf")


# ── Impressão no servidor via SumatraPDF ──────────────────────────────────────

SUMATRA_PATH = r"C:\Users\CADASTRO\AppData\Local\SumatraPDF\SumatraPDF.exe"

@layouts_bp.route("/imprimir/<filename>", methods=["POST"])
def imprimir(filename):
    import ctypes
    from flask import current_app

    caminho_pdf = os.path.join(current_app.root_path, OUTPUT_FOLDER, filename)

    if not os.path.exists(caminho_pdf):
        return jsonify({
            "ok": False,
            "msg": f"PDF não encontrado em: {caminho_pdf}"
        }), 404

    try:
        # ShellExecute com verbo "print" — envia direto para o programa
        # associado ao .pdf (SumatraPDF) usando a API nativa do Windows.
        # Muito mais rápido que abrir o processo manualmente pois reutiliza
        # a instância já carregada pelo sistema.
        ret = ctypes.windll.shell32.ShellExecuteW(
            None,           # hwnd
            "print",        # verbo
            caminho_pdf,    # arquivo
            None,           # parâmetros
            None,           # diretório
            0               # SW_HIDE
        )
        # ShellExecute retorna > 32 em caso de sucesso
        if ret <= 32:
            return jsonify({
                "ok": False,
                "msg": f"ShellExecute falhou com código {ret}"
            }), 500
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)}), 500


# ── API: preview JSON do layout ───────────────────────────────────────────────

@layouts_bp.route("/<layout_id>/json")
def ver_json(layout_id):
    layout = gl.carregar_layout(layout_id)
    if not layout:
        return jsonify({"erro": "não encontrado"}), 404
    return jsonify(layout)


# ── Preview da placa com valores de exemplo ───────────────────────────────────

@layouts_bp.route("/<layout_id>/preview")
def preview(layout_id):
    import io
    from PIL import Image
    from flask import current_app

    layout = gl.carregar_layout(layout_id)
    if not layout:
        return ("Layout não encontrado", 404)

    # Carrega imagem base
    caminho_img = os.path.join(current_app.root_path, "assets",
                               layout.get("imagem_base", ""))
    try:
        img_base = Image.open(caminho_img).convert("RGB")
    except Exception:
        return ("Imagem base não encontrada", 404)

    # Monta valores de exemplo: preco_split → "12,99" (separar_preco divide em "12"/",99")
    # texto_riscado → "9,99" (renderizar_campo adiciona "R$ " automaticamente)
    valores = {}
    for campo in layout.get("campos", []):
        tipo  = campo.get("tipo", "texto")
        label = campo.get("label", campo["id"])
        if tipo in ("preco_split", "preco_simples"):
            valores[campo["id"]] = "12,99"
        elif tipo == "texto_riscado":
            valores[campo["id"]] = "9,99"
        else:
            valores[campo["id"]] = label

    # Gera a imagem em memória
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        gl.criar_imagem_com_layout(layout, valores, img_base, tmp_path)
        # Lê para memória e fecha o handle imediatamente (evita PermissionError no Windows)
        with Image.open(tmp_path) as raw:
            raw.load()
            img = raw.copy()
        # Se a imagem for landscape, rotaciona 90° para caber no preview A4
        if img.width > img.height:
            img = img.rotate(90, expand=True)
        img.thumbnail((600, 600))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        buf.seek(0)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

    from flask import send_file
    return send_file(buf, mimetype="image/jpeg")
