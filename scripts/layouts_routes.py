"""
scripts/layouts_routes.py
Blueprint Flask para gerenciamento e geração de layouts.

Registre no app.py:
    from scripts.layouts_routes import layouts_bp
    app.register_blueprint(layouts_bp)

Adicione ao index.html um card apontando para /layouts
"""
import os
import re
import shutil
import json
import time
import uuid
from flask import (Blueprint, render_template, request, jsonify,
                   send_file, send_from_directory, abort, redirect, url_for)
from scripts import gerador_layouts as gl
from scripts import gira_imagem, gera_pdf

layouts_bp = Blueprint("layouts", __name__, url_prefix="/layouts")

UPLOAD_FOLDER = "uploads"
OUTPUT_FOLDER = "outputs"


# ── Helpers ───────────────────────────────────────────────────────────────────
# Cada geração usa arquivos PRÓPRIOS (lista, pasta de imagens e PDF com um id).
# Antes eram fixos — uploads/produtos_layout.txt, outputs/ofertas_geradas/ e
# outputs/ofertas_final.pdf — e a pasta era apagada a cada geração: duas pessoas
# gerando ao mesmo tempo recebiam as placas uma da outra, ou um PDF pela metade.
PDF_NOME = re.compile(r"^ofertas_[0-9a-f]{12}\.pdf$")
GUARDAR_PDF_S = 24 * 3600        # PDFs gerados ficam um dia (prévia e reimpressão)
EXT_IMAGEM = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _pasta_saida(gid):
    return os.path.join(OUTPUT_FOLDER, "ofertas_geradas", gid)


def _limpar_antigos(pasta_pdfs):
    """Apaga PDFs de geração com mais de um dia (e o PDF fixo da versão
    antiga). Falha ao apagar não impede gerar."""
    agora = time.time()
    try:
        nomes = os.listdir(pasta_pdfs)
    except OSError:
        return
    for nome in nomes:
        if PDF_NOME.match(nome) or nome == "ofertas_final.pdf":
            caminho = os.path.join(pasta_pdfs, nome)
            try:
                if agora - os.path.getmtime(caminho) > GUARDAR_PDF_S:
                    os.remove(caminho)
            except OSError:
                pass


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
    if ext not in EXT_IMAGEM:
        return jsonify({"ok": False, "msg": "Envie uma imagem (PNG ou JPG)."}), 400
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
    gid        = uuid.uuid4().hex[:12]
    pasta      = _pasta_saida(gid)
    os.makedirs(pasta, exist_ok=True)
    os.makedirs(UPLOAD_FOLDER, exist_ok=True)
    try:
        return _gerar(layout, pdf_layout, gid, pasta)
    finally:
        # As imagens e a lista são intermediárias: o que fica é o PDF.
        shutil.rmtree(pasta, ignore_errors=True)
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, f"produtos_{gid}.txt"))
        except OSError:
            pass


def _gerar(layout, pdf_layout, gid, pasta):
    # ── Origem dos dados: arquivo txt ou formulário manual ────────────────────
    caminho_txt = os.path.join(UPLOAD_FOLDER, f"produtos_{gid}.txt")
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
                # ":" separa os campos na lista; dentro de um valor (descrição
                # do catálogo, por exemplo) empurraria os campos seguintes.
                partes.append((valores[i] if i < len(valores) else "").replace(":", " "))
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
    pasta_pdfs = os.path.join(current_app.root_path, OUTPUT_FOLDER)
    _limpar_antigos(pasta_pdfs)
    caminho_pdf = os.path.join(pasta_pdfs, f"ofertas_{gid}.pdf")
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
    if not (PDF_NOME.match(filename) or filename == "ofertas_final.pdf"):
        abort(404)
    pasta = os.path.join(current_app.root_path, OUTPUT_FOLDER)
    return send_from_directory(pasta, filename, mimetype="application/pdf")


# ── Impressão no servidor via SumatraPDF ──────────────────────────────────────

@layouts_bp.route("/imprimir/<filename>", methods=["POST"])
def imprimir(filename):
    import ctypes
    from flask import current_app

    if not PDF_NOME.match(filename):
        return jsonify({"ok": False, "msg": "Arquivo inválido."}), 400

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
