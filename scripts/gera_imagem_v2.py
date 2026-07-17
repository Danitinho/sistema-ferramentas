import sys
import os
from PIL import Image, ImageDraw, ImageFont

# =============================================================================
# CONFIGURAÇÃO DAS ÁREAS  (x1, y1, x2, y2)
# Mapeadas automaticamente a partir do modelo_marcado.png (4419 x 6250 px)
# =============================================================================

# 🔴 VERMELHO  — Nome do produto
# 🔴 VERMELHO  — Nome do produto
AREA_PRODUTO    = (16,   310,  1395,  852)

# 🟡 AMARELO   — Preço anterior ("DE R$")
AREA_PRECO_ANT  = (182,  964,  1225, 1206)

# 🟢 VERDE     — Parte inteira do preço  (ex.: "15" de "15,90")
AREA_INTEIRO    = (434,  1272, 1021, 1811)

# 🩷 ROSA      — Parte fracionária do preço  (ex.: ",90" de "15,90")
AREA_CENTAVOS   = (1027, 1306, 1302, 1548)

# 🔵 AZUL      — Unidade de medida  (ex.: "UN", "KG")
AREA_UNIDADE    = (1027, 1620, 1301, 1814)

# =============================================================================
# ARQUIVOS
# =============================================================================
ARQUIVO_BASE  = "modelo_novo.png"   # template sem textos
ARQUIVO_LISTA = "ofertas3.txt"        # lista de produtos
PASTA_SAIDA   = "ofertas_geradas"     # onde salvar as imagens

# =============================================================================
# FONTE
# =============================================================================
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
FONTE_PATH = os.path.join(BASE_DIR, "..", "assets", "Anton-Regular.ttf")
FONTE_PATH2 = os.path.join(BASE_DIR, "..", "assets", "impact.ttf")


# =============================================================================
# CORES  (RGB)
# =============================================================================
COR_PRODUTO  = (0,   0,   0)    # preto  — nome do produto
COR_PRECO    = (203, 0,   0)    # vermelho escuro — preços
COR_UNIDADE  = (0,   0,   0)    # preto  — unidade de medida
COR_PRECO_ANT= (0,   0,   0)    # preto  — preço anterior

# =============================================================================
# MODO DEBUG — se True, desenha os retângulos coloridos na imagem de saída
# Útil para verificar o alinhamento das áreas antes da produção
# =============================================================================
DEBUG_AREAS = False

# =============================================================================
# FUNÇÕES UTILITÁRIAS
# =============================================================================

def quebrar_texto(draw, texto, fonte, max_largura):
    """Insere \n para garantir que nenhuma linha ultrapasse max_largura."""
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


def get_font_para_caixa(draw, texto, caixa, fonte_path, tamanho_inicial=5, passo=2):
    """
    Aumenta o tamanho da fonte até o texto preencher ao máximo a caixa
    sem ultrapassar largura nem altura.
    Retorna (fonte, texto_com_quebras_de_linha).
    """
    if not fonte_path:
        return ImageFont.load_default(), texto

    max_w = caixa[2] - caixa[0]
    max_h = caixa[3] - caixa[1]

    fonte_final = None
    texto_final = texto
    tamanho = tamanho_inicial

    while True:
        try:
            fonte = ImageFont.truetype(fonte_path, tamanho)
        except IOError:
            print(f"Erro ao carregar a fonte '{fonte_path}'. Saindo.")
            sys.exit(1)

        texto_quebrado = quebrar_texto(draw, texto, fonte, max_w)
        bbox = draw.multiline_textbbox((0, 0), texto_quebrado, font=fonte)
        largura = bbox[2] - bbox[0]
        altura  = bbox[3] - bbox[1]

        if largura > max_w or altura > max_h:
            break

        fonte_final = fonte
        texto_final = texto_quebrado
        tamanho += passo

    if fonte_final is None:
        print(f"Aviso: Caixa muito pequena para o texto '{texto[:30]}...'")
        return ImageFont.truetype(fonte_path, tamanho_inicial), texto

    return fonte_final, texto_final


def desenhar_texto_na_caixa(draw, texto, caixa, fonte_path, cor,
                             maiusculo=False, alinhamento="center",
                             ancora="mm", tamanho_inicial=5, passo=2):
    """
    Obtém a fonte ideal, posiciona o texto dentro da caixa e
    retorna o bbox real do texto desenhado (x1, y1, x2, y2).
    ancora controla o ponto de referência do texto (Pillow anchor):
      "mm" = centro horizontal + centro vertical  (padrão)
      "mt" = centro horizontal + topo
    Se maiusculo=True, converte o texto para caixa alta.
    """
    fonte, texto_fmt = get_font_para_caixa(draw, texto, caixa, fonte_path,
                                            tamanho_inicial, passo)
    if maiusculo:
        texto_fmt = texto_fmt.upper()

    cx = (caixa[0] + caixa[2]) / 2

    # Escolhe o ponto Y de acordo com o ancora
    if ancora[1] == "t":
        cy = caixa[1]
    elif ancora[1] == "b":
        cy = caixa[3]
    else:
        cy = (caixa[1] + caixa[3]) / 2

    draw.multiline_text(
        (cx, cy),
        texto_fmt,
        fill=cor,
        font=fonte,
        align=alinhamento,
        anchor=ancora
    )

    # Retorna o bbox real do texto desenhado (posição efetiva na imagem)
    return draw.multiline_textbbox((cx, cy), texto_fmt, font=fonte,
                                   align=alinhamento, anchor=ancora)


def separar_preco(preco_str):
    """
    Divide um preço em parte inteira e parte fracionária.
    Aceita vírgula ou ponto como separador decimal.

    Exemplos:
      "2,99"  → ("2",  ",99")
      "12.50" → ("12", ",50")
      "5"     → ("5",  "")
      "1.299,90" → ("1.299", ",90")  (formato brasileiro com milhar)
    """
    preco_str = preco_str.strip()

    # Detectar separador decimal (último , ou .)
    for sep in [',', '.']:
        idx = preco_str.rfind(sep)
        if idx != -1:
            inteiro    = preco_str[:idx]
            fracao     = preco_str[idx:]        # inclui o separador (",99")
            # Normaliza separador para vírgula no display
            fracao = "," + fracao[1:]
            return inteiro, fracao

    return preco_str, ""   # sem parte decimal


def safe_filename(nome):
    """Cria um nome de arquivo seguro."""
    nome_seguro = nome.strip().lower().replace(" ", "_")
    for char in r' \/*?"<>|:':
        nome_seguro = nome_seguro.replace(char, "")
    return f"{nome_seguro}.png"


# =============================================================================
# GERAÇÃO DE UMA IMAGEM
# =============================================================================

def criar_imagem_oferta(nome_produto, preco_anterior, preco_oferta,
                         unidade, img_base, caminho_saida):
    """Gera e salva uma placa de oferta preenchida."""
    imagem = img_base.copy()
    draw   = ImageDraw.Draw(imagem)

    # --- Modo debug: desenha os retângulos coloridos ---
    if DEBUG_AREAS:
        draw.rectangle(AREA_PRODUTO,   outline=(221, 37, 45),  width=8)
        draw.rectangle(AREA_PRECO_ANT, outline=(255, 214, 0),  width=8)
        draw.rectangle(AREA_INTEIRO,   outline=(35,  177, 75), width=8)
        draw.rectangle(AREA_CENTAVOS,  outline=(255, 175,202), width=8)
        draw.rectangle(AREA_UNIDADE,   outline=(113, 146,187), width=8)

    # 1. Nome do produto (vermelho, maiúsculo)
    desenhar_texto_na_caixa(draw, nome_produto, AREA_PRODUTO,
                             FONTE_PATH, COR_PRODUTO, maiusculo=True)

    # 2. Preço anterior (amarelo box) + X de riscado por cima
    if preco_anterior:
        desenhar_texto_na_caixa(draw, "DE R$ "+preco_anterior, AREA_PRECO_ANT,
                                 FONTE_PATH2, COR_PRECO_ANT)

        # Desenha o X diagonal sobre a caixa do preço anterior
        x1, y1, x2, y2 = AREA_PRECO_ANT
        espessura_x = 20   # proporcional ao tamanho da caixa
        cor_x = (180, 0, 0)                       # vermelho escuro
        draw.line([(x1, y1), (x2, y2)], fill=cor_x, width=espessura_x)  # \ diagonal
        draw.line([(x2, y1), (x1, y2)], fill=cor_x, width=espessura_x)  # /  diagonal

    # 3. Preço oferta — separa inteiro / centavos
    inteiro, centavos = separar_preco(preco_oferta)

    # Desenha o valor inteiro e captura o bbox real do texto renderizado
    bbox_inteiro = desenhar_texto_na_caixa(draw, inteiro, AREA_INTEIRO,
                                            FONTE_PATH, COR_PRECO)

    if centavos:
        # Desloca a caixa dos centavos horizontalmente para começar
        # imediatamente após a borda direita do texto dos reais
        borda_direita_reais = bbox_inteiro[2]
        area_cent_ajustada = (
            borda_direita_reais,   # x1 = logo após o texto dos reais
            AREA_CENTAVOS[1],      # y1 mantém o topo original
            AREA_CENTAVOS[2],      # x2 mantém o limite direito original
            AREA_CENTAVOS[3],      # y2 mantém o limite inferior original
        )
        desenhar_texto_na_caixa(draw, centavos, area_cent_ajustada,
                                 FONTE_PATH, COR_PRECO)

    # 4. Unidade de medida (azul box, maiúsculo)
    if unidade:
        desenhar_texto_na_caixa(draw, unidade, AREA_UNIDADE,
                                 FONTE_PATH, COR_UNIDADE, maiusculo=True)

    # Contorno preto fino ao redor da imagem
    largura, altura = imagem.size
    draw.rectangle(
        [(0, 0), (largura - 1, altura - 1)],
        outline=(0, 0, 0),
        width=6          
        # ajuste a espessura aqui
    )

    imagem.save(caminho_saida)
    print(f"  [ok] {caminho_saida}")


# =============================================================================
# PROCESSAMENTO EM LOTE
# =============================================================================

def main(arquivo_lista=None, arquivo_base=None, pasta_saida=None):
    _arquivo_lista = arquivo_lista or ARQUIVO_LISTA
    _arquivo_base  = arquivo_base  or ARQUIVO_BASE
    _pasta_saida   = pasta_saida   or PASTA_SAIDA

    # DEBUG
    print(">>> [v2.main] arquivo_lista recebido:", arquivo_lista)
    print(">>> [v2.main] _arquivo_lista usado:", _arquivo_lista)

    print("=" * 55)
    print(" Gerador de Placas de Oferta — v2")
    print("=" * 55)
    print(f"Formato do arquivo '{_arquivo_lista}':")
    print("  nome:preço_anterior:preço_oferta:unidade")
    print("  Ex: Arroz 5kg:8,99:6,99:PCT")
    print("  (linhas com # são ignoradas)\n")

    # Verificar fonte
    try:
        ImageFont.truetype(FONTE_PATH, 10)
    except IOError:
        print(f"ERRO: Fonte não encontrada: {FONTE_PATH}")
        return

    # Carregar template
    try:
        img_base = Image.open(_arquivo_base).convert("RGB")
    except FileNotFoundError:
        print(f"ERRO: Template '{_arquivo_base}' não encontrado.")
        return

    # Carregar lista de produtos
    try:
        with open(_arquivo_lista, 'r', encoding='utf-8') as f:
            linhas = f.readlines()
    except FileNotFoundError:
        print(f"ERRO: Arquivo '{_arquivo_lista}' não encontrado.")
        return

    os.makedirs(_pasta_saida, exist_ok=True)
    print(f"Saída em: {_pasta_saida}/\n")

    sucesso = falha = 0

    for i, linha in enumerate(linhas, start=1):
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue

        partes = [p.strip() for p in linha.split(':')]
        if len(partes) < 4:
            print(f"Linha {i} ignorada (esperado 4 campos separados por ':'):")
            print(f"  '{linha}'")
            print( "  Formato correto: nome:preco_anterior:preco_oferta:unidade")
            print( "  Exemplo:         File de Frango:18,90:15,99:Kg")
            falha += 1
            continue

        nome         = partes[0]
        preco_ant    = partes[1]
        preco_oferta = partes[2]
        unidade      = partes[3]

        if len(partes) > 4:
            print(f"Aviso linha {i}: {len(partes)} campos encontrados, esperado 4.")
            print( "  Verifique se o preco usa virgula (ex: 15,99) e nao dois-pontos.")

        if not nome or not preco_oferta:
            print(f"Linha {i} ignorada (nome ou preço vazio): '{linha}'")
            falha += 1
            continue

        caminho = os.path.join(_pasta_saida, safe_filename(nome))
        try:
            criar_imagem_oferta(nome, preco_ant, preco_oferta, unidade,
                                  img_base, caminho)
            sucesso += 1
        except Exception as e:
            print(f"ERRO ao processar '{nome}': {e}")
            falha += 1

    print(f"\n{'-'*40}")
    print(f"  Geradas com sucesso : {sucesso}")
    print(f"  Ignoradas / com erro: {falha}")
    print(f"{'-'*40}")


if __name__ == "__main__":
    main()
