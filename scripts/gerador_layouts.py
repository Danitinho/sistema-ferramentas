"""
scripts/gerador_layouts.py
Gerador universal de placas de oferta baseado em layouts JSON.

Cada layout define:
  - imagem base
  - campos (nome, tipo, área, cor, etc.)

Tipos de campo suportados:
  - texto           : texto simples centralizado na área
  - texto_riscado   : texto + X diagonal (para preço anterior)
  - preco_split     : divide "15,99" em inteiro (área1) + centavos (área2)
  - preco_simples   : preço como texto único (layout antigo)
"""

import sys
import os
import json
import uuid
from PIL import Image, ImageDraw, ImageFont

# ── Caminhos base ─────────────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR   = os.path.join(BASE_DIR, "assets")
LAYOUTS_DIR  = os.path.join(ASSETS_DIR, "layouts")
FONTE_PATH   = os.path.join(ASSETS_DIR, "Anton-Regular.ttf")


# =============================================================================
# FUNÇÕES DE RENDERIZAÇÃO (preservadas do gera_imagem_v2)
# =============================================================================

def quebrar_texto(draw, texto, fonte, max_largura):
    """Insere \\n para garantir que nenhuma linha ultrapasse max_largura."""
    palavras = texto.split()
    linhas, linha_atual = [], []
    for palavra in palavras:
        linha_atual.append(palavra)
        bbox = draw.textbbox((0, 0), " ".join(linha_atual), font=fonte)
        if bbox[2] - bbox[0] > max_largura:
            linha_atual.pop()
            if linha_atual:
                linhas.append(" ".join(linha_atual))
            linha_atual = [palavra]
    if linha_atual:
        linhas.append(" ".join(linha_atual))
    return "\n".join(linhas)


def get_font_para_caixa(draw, texto, caixa, fonte_path=None,
                         tamanho_inicial=5, passo=2):
    """
    Aumenta o tamanho da fonte até o texto preencher ao máximo a caixa
    sem ultrapassar largura nem altura. Retorna (fonte, texto_formatado).
    """
    fp = fonte_path or FONTE_PATH
    max_w = caixa[2] - caixa[0]
    max_h = caixa[3] - caixa[1]
    fonte_final = None
    texto_final = texto
    tamanho = tamanho_inicial

    while True:
        try:
            fonte = ImageFont.truetype(fp, tamanho)
        except IOError:
            print(f"ERRO: Fonte não encontrada: {fp}")
            sys.exit(1)

        texto_quebrado = quebrar_texto(draw, texto, fonte, max_w)
        bbox = draw.multiline_textbbox((0, 0), texto_quebrado, font=fonte)

        if (bbox[2] - bbox[0]) > max_w or (bbox[3] - bbox[1]) > max_h:
            break

        fonte_final = fonte
        texto_final = texto_quebrado
        tamanho += passo

    if fonte_final is None:
        return ImageFont.truetype(fp, tamanho_inicial), texto

    return fonte_final, texto_final


def desenhar_texto_na_caixa(draw, texto, caixa, cor,
                              fonte_path=None, maiusculo=False,
                              alinhamento="center", ancora="mm"):
    """
    Renderiza texto na caixa com fonte auto-ajustada.
    Retorna o bbox real do texto desenhado.
    """
    fonte, texto_fmt = get_font_para_caixa(draw, texto, caixa, fonte_path)
    if maiusculo:
        texto_fmt = texto_fmt.upper()

    cx = (caixa[0] + caixa[2]) / 2
    cy = (caixa[1] + caixa[3]) / 2 if ancora[1] != "t" else caixa[1]

    draw.multiline_text((cx, cy), texto_fmt,
                         fill=tuple(cor), font=fonte,
                         align=alinhamento, anchor=ancora)

    return draw.multiline_textbbox((cx, cy), texto_fmt,
                                    font=fonte, align=alinhamento, anchor=ancora)


def separar_preco(preco_str):
    """
    Divide "15,99" → ("15", ",99").
    Aceita vírgula ou ponto como separador decimal.
    """
    preco_str = preco_str.strip()
    for sep in [',', '.']:
        idx = preco_str.rfind(sep)
        if idx != -1:
            return preco_str[:idx], "," + preco_str[idx + 1:]
    return preco_str, ""


def safe_filename(nome):
    """Gera nome de arquivo seguro."""
    s = nome.strip().lower().replace(" ", "_")
    for c in r' \/*?"<>|:':
        s = s.replace(c, "")
    return f"{s}.png"


# =============================================================================
# RENDERIZAÇÃO DE UM CAMPO
# =============================================================================

def renderizar_campo(draw, campo, valor, bbox_anterior=None):
    """
    Renderiza um campo conforme seu tipo. Retorna bbox do texto desenhado
    (ou None para campos sem texto direto).

    campo: dict com as definições do JSON
    valor: string com o valor a renderizar
    bbox_anterior: bbox do campo anterior (usado para ajuste de centavos)
    """
    tipo      = campo.get("tipo", "texto")
    cor       = campo.get("cor", [0, 0, 0])
    maiusculo = campo.get("maiusculo", False)
    fp        = campo.get("fonte_path", FONTE_PATH)

    # ── texto simples ──────────────────────────────────────────────────────────
    if tipo == "texto":
        area = tuple(campo["area"])
        return desenhar_texto_na_caixa(draw, valor, area, cor,
                                        fonte_path=fp, maiusculo=maiusculo)

    # ── texto riscado (preço anterior) ────────────────────────────────────────
    elif tipo == "texto_riscado":
        area = tuple(campo["area"])
        bbox = desenhar_texto_na_caixa(draw, "R$ "+valor, area, cor,
                                        fonte_path=fp, maiusculo=maiusculo)
        # Desenha X diagonal sobre a área inteira
        x1, y1, x2, y2 = area
        cor_x     = campo.get("cor_x", [180, 0, 0])
        espessura = 20
        draw.line([(x1, y1), (x2, y2)], fill=tuple(cor_x), width=espessura)
        draw.line([(x2, y1), (x1, y2)], fill=tuple(cor_x), width=espessura)
        return bbox

    # ── preço simples (um único campo, sem split) ─────────────────────────────
    elif tipo == "preco_simples":
        area = tuple(campo["area"])
        return desenhar_texto_na_caixa(draw, valor, area, cor,
                                        fonte_path=fp, maiusculo=maiusculo)

    # ── preço split (inteiro + centavos em áreas separadas) ───────────────────
    elif tipo == "preco_split":
        inteiro, centavos = separar_preco(valor)
        area_int  = tuple(campo["area_inteiro"])
        area_cent = tuple(campo["area_centavos"])

        # Desenha a parte inteira
        bbox_int = desenhar_texto_na_caixa(draw, inteiro, area_int, cor,
                                            fonte_path=fp)

        # Centavos: se ajuste habilitado, alinha X e Y com o texto real do inteiro
        if centavos:
            if campo.get("centavos_ajustado", True):
                area_cent = (
                    bbox_int[2],     # x1 = logo após o texto dos reais
                    bbox_int[1],     # y1 = mesma altura do topo real do inteiro
                    area_cent[2],
                    area_cent[3],
                )
            desenhar_texto_na_caixa(draw, centavos, area_cent, cor,
                                     fonte_path=fp)
        return bbox_int

    return None


# =============================================================================
# GERAÇÃO DE UMA IMAGEM
# =============================================================================

def criar_imagem_com_layout(layout, valores, img_base, caminho_saida):
    """
    Gera uma imagem preenchida a partir de um layout e um dict de valores.

    layout : dict carregado do JSON
    valores: dict {campo_id: valor_string}
    """
    imagem = img_base.copy()
    draw   = ImageDraw.Draw(imagem)

    # Modo debug: desenha os retângulos de cada campo
    if layout.get("debug_areas", False):
        cores_debug = [
            (221, 37,  45),   # vermelho
            (255, 214,  0),   # amarelo
            (35,  177, 75),   # verde
            (255, 175, 202),  # rosa
            (113, 146, 187),  # azul
        ]
        for idx, campo in enumerate(layout.get("campos", [])):
            cor_d = cores_debug[idx % len(cores_debug)]
            if campo.get("tipo") == "preco_split":
                draw.rectangle(tuple(campo["area_inteiro"]),  outline=cor_d, width=6)
                draw.rectangle(tuple(campo["area_centavos"]), outline=cor_d, width=6)
            elif "area" in campo:
                draw.rectangle(tuple(campo["area"]), outline=cor_d, width=6)

    # Renderiza cada campo
    bbox_anterior = None
    for campo in layout.get("campos", []):
        campo_id = campo["id"]

        # Campo pré-preenchido: usa valor fixo definido no layout
        valor_fixo = campo.get("valor_fixo", "")
        if valor_fixo:
            valor = valor_fixo.strip()
        else:
            valor = valores.get(campo_id, "").strip()

        if not valor and campo.get("opcional", False):
            continue
        if not valor:
            continue

        bbox_anterior = renderizar_campo(draw, campo, valor, bbox_anterior)

    # Contorno preto fino na borda da imagem
    if layout.get("contorno_preto", False):
        w, h      = imagem.size
        espessura = layout.get("contorno_largura", 6)
        draw.rectangle([(0, 0), (w - 1, h - 1)],
                        outline=(0, 0, 0), width=espessura)

    imagem.save(caminho_saida)
    # NOTA: sem caracteres fora do ASCII no print — com stdout em cp1252
    # (terminal Windows), um "✓" derruba a geração inteira com UnicodeEncodeError.
    print(f"  [ok] {caminho_saida}")


# =============================================================================
# PROCESSAMENTO EM LOTE (txt)
# =============================================================================

def processar_lote(layout, arquivo_lista, pasta_saida,
                   arquivo_base=None, pasta_saida_override=None):
    """
    Lê o arquivo txt e gera uma imagem para cada linha.
    Formato do txt: campo1:campo2:campo3... (ordem dos campos do layout)
    """
    pasta = pasta_saida_override or pasta_saida

    # Campos de entrada: apenas os que NÃO têm valor_fixo definido
    campos_entrada = [c for c in layout.get("campos", [])
                      if not c.get("valor_fixo", "").strip()]
    n_campos = len(campos_entrada)

    # Carrega imagem base
    img_path = arquivo_base or os.path.join(ASSETS_DIR,
                                             layout.get("imagem_base", ""))
    try:
        img_base = Image.open(img_path).convert("RGB")
    except FileNotFoundError:
        print(f"ERRO: Imagem base não encontrada: {img_path}")
        return 0, 0

    # Lê lista
    try:
        with open(arquivo_lista, "r", encoding="utf-8") as f:
            linhas = f.readlines()
    except FileNotFoundError:
        print(f"ERRO: Arquivo não encontrado: {arquivo_lista}")
        return 0, 0

    os.makedirs(pasta, exist_ok=True)
    sucesso = falha = 0

    for i, linha in enumerate(linhas, start=1):
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue

        partes = [p.strip() for p in linha.split(":")]

        if len(partes) < n_campos:
            print(f"Linha {i} ignorada: esperado {n_campos} campos, "
                  f"encontrado {len(partes)}: '{linha}'")
            falha += 1
            continue

        # Monta dict de valores
        valores = {}
        for idx, campo in enumerate(campos_entrada):
            valores[campo["id"]] = partes[idx] if idx < len(partes) else ""

        nome_arquivo = safe_filename(partes[0])
        caminho      = os.path.join(pasta, nome_arquivo)

        try:
            criar_imagem_com_layout(layout, valores, img_base, caminho)
            sucesso += 1
        except Exception as e:
            print(f"ERRO ao processar linha {i} ('{partes[0]}'): {e}")
            import traceback; traceback.print_exc()
            falha += 1

    print(f"\n{'-'*40}")
    print(f"  Geradas com sucesso : {sucesso}")
    print(f"  Ignoradas / com erro: {falha}")
    print(f"{'-'*40}")
    return sucesso, falha


# =============================================================================
# GERENCIAMENTO DE LAYOUTS
# =============================================================================

def listar_layouts():
    """Retorna lista de todos os layouts cadastrados."""
    os.makedirs(LAYOUTS_DIR, exist_ok=True)
    layouts = []
    for arquivo in sorted(os.listdir(LAYOUTS_DIR)):
        if arquivo.endswith(".json"):
            try:
                with open(os.path.join(LAYOUTS_DIR, arquivo),
                          encoding="utf-8") as f:
                    dados = json.load(f)
                dados["_arquivo"] = arquivo
                dados["_id"]      = arquivo[:-5]  # remove .json
                layouts.append(dados)
            except Exception as e:
                print(f"Erro ao ler layout {arquivo}: {e}")
    return layouts


def carregar_layout(layout_id):
    """Carrega e retorna um layout pelo ID (nome do arquivo sem .json)."""
    caminho = os.path.join(LAYOUTS_DIR, f"{layout_id}.json")
    if not os.path.exists(caminho):
        return None
    with open(caminho, encoding="utf-8") as f:
        dados = json.load(f)
    dados["_id"] = layout_id
    return dados


def salvar_layout(dados, layout_id=None):
    """Salva um layout. Gera ID único se não informado. Retorna o ID."""
    os.makedirs(LAYOUTS_DIR, exist_ok=True)
    lid = layout_id or str(uuid.uuid4())[:8]
    caminho = os.path.join(LAYOUTS_DIR, f"{lid}.json")
    with open(caminho, "w", encoding="utf-8") as f:
        json.dump(dados, f, ensure_ascii=False, indent=2)
    return lid


def excluir_layout(layout_id):
    """Remove o arquivo JSON do layout."""
    caminho = os.path.join(LAYOUTS_DIR, f"{layout_id}.json")
    if os.path.exists(caminho):
        os.remove(caminho)
        return True
    return False


def formato_txt(layout):
    """Gera string descritiva do formato do txt para o layout."""
    labels = [c.get("label", c["id"]) for c in layout.get("campos", [])
              if not c.get("valor_fixo", "").strip()]
    return ":".join(labels)
