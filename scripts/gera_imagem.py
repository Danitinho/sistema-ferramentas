import sys
import os
from PIL import Image, ImageDraw, ImageFont

# --- CONFIGURAÇÃO OBRIGATÓRIA ---
# Ajuste estas coordenadas para sua imagem de 437x621
# Área onde o NOME DO PRODUTO será escrito
AREA_PRODUTO = (685, 20, 2020, 640) 
# Área onde o PREÇO será escrito
AREA_PRECO = (965, 770, 1700, 1100)
# Área onde a UNIDADE DE MEDIDA do produto será escrita
AREA_UNIDADE = (1750, 933, 2020, 1050)

# --- CONFIGURAÇÃO DOS ARQUIVOS ---
# O template que será usado como base
ARQUIVO_BASE = "placa_oferta.jpeg"
# O arquivo de texto com a lista de produtos
ARQUIVO_LISTA = "produtos.txt"
# A pasta onde as imagens finalizadas serão salvas
PASTA_SAIDA = "ofertas_geradas"

# --- CONFIGURAÇÃO DA FONTE ---
# Caminho para seu arquivo .ttf (use a fonte que desejar)
#FONTE_PATH = "assets/Anton-Regular.ttf" # ou "Roboto-Bold.ttf", "C:/Windows/Fonts/impact.ttf", etc.
BASE_DIR   = os.path.dirname(os.path.abspath(__file__))
FONTE_PATH = os.path.join(BASE_DIR, "..", "assets", "Anton-Regular.ttf")

# Cores do texto (formato RGB)
COR_PRODUTO = (0, 0, 0) # Preto
COR_PRECO = (203, 0, 0) # Vermelho escuro

def quebrar_texto(draw, texto, fonte, max_largura):
    """
    Pega uma string de texto e insere quebras de linha (\n) para garantir
    que nenhuma linha ultrapasse a 'max_largura'.
    """
    palavras = texto.split()
    linhas = []
    linha_atual = []

    for palavra in palavras:
        # Testa como a linha ficaria com a nova palavra
        linha_atual.append(palavra)
        texto_teste = " ".join(linha_atual)
        
        # Mede a largura da linha de teste
        bbox = draw.textbbox((0, 0), texto_teste, font=fonte)
        largura_teste = bbox[2] - bbox[0]

        # Se ultrapassou a largura máxima da caixa
        if largura_teste > max_largura:
            # Remove a última palavra (que causou o estouro)
            linha_atual.pop()
            
            # Se a linha atual não estiver vazia, guarda ela nas linhas definitivas
            if linha_atual:
                linhas.append(" ".join(linha_atual))
                
            # Começa uma nova linha apenas com a palavra que sobrou
            linha_atual = [palavra]

    # Não esquece de adicionar a última linha que estava sendo montada
    if linha_atual:
        linhas.append(" ".join(linha_atual))

    # Junta todas as linhas com a quebra de linha do Python (\n)
    return "\n".join(linhas)


def get_font_para_caixa(draw, texto, caixa, fonte_path):
    if not fonte_path:
        print("Usando fonte padrão. O ajuste de tamanho não é suportado.")
        return ImageFont.load_default(), texto
    
    #draw.rectangle(caixa, outline="lime", width=5)

    max_largura = caixa[2] - caixa[0]
    max_altura = caixa[3] - caixa[1]
    
    tamanho = 5 
    fonte_final = None
    texto_final = texto # Guardará o texto com os \n

    while True:
        try:
            fonte = ImageFont.truetype(fonte_path, tamanho)
        except IOError:
            print(f"Erro ao carregar a fonte '{fonte_path}'. Saindo.")
            sys.exit(1)
            
        # 1. Tenta quebrar o texto para caber na LARGURA com esta fonte
        texto_quebrado = quebrar_texto(draw, texto, fonte, max_largura)
        
        # 2. Mede a ALTURA e LARGURA TOTAL do bloco de texto (agora com várias linhas)
        # Usamos multiline_textbbox porque o texto agora pode ter '\n'
        bbox = draw.multiline_textbbox((0, 0), texto_quebrado, font=fonte)
        largura_texto = bbox[2] - bbox[0]
        altura_texto = bbox[3] - bbox[1]
        
        # 3. Se a ALTURA total ultrapassar a caixa, ou se uma única palavra 
        # for mais larga que a caixa (impossível quebrar), paramos.
        if largura_texto > max_largura or altura_texto > max_altura:
            break 
            
        # Se coube perfeitamente (largura e altura), salvamos como o ideal até agora
        fonte_final = fonte 
        texto_final = texto_quebrado # Salvamos a versão com \n
        tamanho += 2 
        
    if fonte_final is None:
        print(f"Aviso: A caixa é muito pequena para o texto.")
        return ImageFont.truetype(fonte_path, 5), texto
    
    return fonte_final, texto_final

def safe_filename(nome):
    """Cria um nome de arquivo seguro a partir do nome do produto."""
    # Remove espaços no início/fim e deixa em minúsculas
    nome_seguro = nome.strip().lower()
    # Substitui espaços por underscores
    nome_seguro = nome_seguro.replace(" ", "_")
    # Remove caracteres problemáticos
    caracteres_invalidos = r' \/*?"<>|:'
    for char in caracteres_invalidos:
        nome_seguro = nome_seguro.replace(char, "")
    
    return f"{nome_seguro}.png"

def criar_imagem_oferta(nome_produto, preco, unidade, img_base, caminho_saida):
    """
    Gera uma única imagem de oferta e a salva no caminho especificado.
    """
    # Cria uma cópia da imagem base para não modificar a original
    imagem = img_base.copy()
    draw = ImageDraw.Draw(imagem)

    # --- 4. Processar e desenhar NOME DO PRODUTO ---
    # (Ajuste o '60' se necessário para sua imagem)
    fonte_produto, texto_produto_formatado = get_font_para_caixa(draw, nome_produto, AREA_PRODUTO, FONTE_PATH)
    centro_x_prod = (AREA_PRODUTO[0] + AREA_PRODUTO[2]) / 2
    centro_y_prod = (AREA_PRODUTO[1] + AREA_PRODUTO[3]) / 2

    draw.multiline_text(
        (centro_x_prod, centro_y_prod),
        texto_produto_formatado.upper(), # Coloca em maiúsculas para dar destaque
        fill=COR_PRODUTO,
        font=fonte_produto,
        align="center",
        anchor="mm"
    )

    # --- 5. Processar e desenhar PREÇO ---
    # Adiciona "R$" se não estiver presente, para um visual melhor
    preco_final = preco
    
    print(preco_final)

    # (Ajuste o '90' se necessário para sua imagem)
    fonte_preco, texto_produto_formatado = get_font_para_caixa(draw, preco_final, AREA_PRECO, FONTE_PATH)
    centro_x_preco = (AREA_PRECO[0] + AREA_PRECO[2]) / 2
    centro_y_preco = (AREA_PRECO[1] + AREA_PRECO[3]) / 2

    draw.multiline_text(
        (centro_x_preco, centro_y_preco),
        preco_final,
        fill=COR_PRECO,
        font=fonte_preco,
        anchor="mm"
    )



    #processar e desenhar unidade de medida
    fonte_unidade, texto_produto_formatado = get_font_para_caixa(draw, unidade, AREA_UNIDADE, FONTE_PATH)
    centro_x_uni = (AREA_UNIDADE[0] + AREA_UNIDADE[2]) / 2
    centro_y_uni = (AREA_UNIDADE[1] + AREA_UNIDADE[3]) / 2

    draw.multiline_text(
        (centro_x_uni, centro_y_uni),
        texto_produto_formatado.upper(), # Coloca em maiúsculas para dar destaque
        fill=COR_PRODUTO,
        font=fonte_unidade,
        align="center",
        anchor="mm"
    )

    # --- 6. Salvar o resultado ---
    imagem.save(caminho_saida)
    print(f"  -> Imagem gerada: {caminho_saida}")





def main(arquivo_lista=None, arquivo_base=None, pasta_saida=None):
    _arquivo_lista = arquivo_lista or ARQUIVO_LISTA
    _arquivo_base  = arquivo_base  or ARQUIVO_BASE
    _pasta_saida   = pasta_saida   or PASTA_SAIDA

    print("Iniciando processamento em lote de placas de oferta...")

    # --- 1. Verificar e carregar arquivos ---
    try:
        ImageFont.truetype(FONTE_PATH, 10) 
    except IOError:
        print(f"Erro fatal: Fonte '{FONTE_PATH}' não encontrada.")
        print("Verifique o nome do arquivo ou baixe a fonte.")
        return

    try:
        img_base = Image.open(_arquivo_base).convert("RGB")
    except FileNotFoundError:
        print(f"Erro fatal: Imagem base '{_arquivo_base}' não encontrada.")
        return

    try:
        with open(_arquivo_lista, 'r', encoding='utf-8') as f:
            linhas = f.readlines()
    except FileNotFoundError:
        print(f"Erro fatal: Arquivo de lista '{_arquivo_lista}' não encontrado.")
        print("Crie este arquivo no mesmo diretório do script.")
        return

    # --- 2. Criar pasta de saída ---
    os.makedirs(_pasta_saida, exist_ok=True)
    print(f"Imagens serão salvas em: '{_pasta_saida}/'")

    # --- 3. Loop de processamento ---
    contador_sucesso = 0
    contador_falha = 0
    for i, linha in enumerate(linhas):
        linha = linha.strip()
        if not linha or linha.startswith("#"):
            continue
        
        if ':' not in linha:
            print(f"Aviso: Linha {i+1} ignorada (sem ':'): '{linha}'")
            contador_falha += 1
            continue
            
        try:
            nome_produto, preco, unidade_medida = linha.split(':', 2)
            nome_produto   = nome_produto.strip()
            preco          = preco.strip()
            unidade_medida = unidade_medida.strip()
            
            if not nome_produto or not preco:
                raise ValueError("Nome ou preço vazio.")
                 
        except ValueError:
            print(f"Aviso: Linha {i+1} ignorada (formato inválido): '{linha}'")
            contador_falha += 1
            continue
            
        nome_arquivo  = safe_filename(nome_produto)
        caminho_saida = os.path.join(_pasta_saida, nome_arquivo)
        
        try:
            criar_imagem_oferta(nome_produto, preco, unidade_medida, img_base, caminho_saida)
            contador_sucesso += 1
        except Exception as e:
            print(f"Erro ao processar '{nome_produto}': {e}")
            contador_falha += 1

    print("\n--- Processamento Concluído ---")
    print(f"Imagens geradas com sucesso: {contador_sucesso}")
    print(f"Linhas ignoradas ou com erro: {contador_falha}")

if __name__ == "__main__":
    main()