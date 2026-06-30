import os
from PIL import Image

def gerar_pdf_preenchimento_total(pasta_entrada, nome_saida="ofertas_final.pdf", layout=4):
    """
    Gera um PDF onde cada imagem é esticada/distorcida para preencher 
    completamente seu espaço.
    :param layout: 1 (Página inteira), 2 (Metade da página) ou 4 (Um quarto da página).
    """
    
    # Configurações A4 em 300 DPI
    A4_WIDTH = 2480
    A4_HEIGHT = 3508
    
    # --- CONFIGURAÇÃO DINÂMICA DO LAYOUT ---
    if layout == 1:
        tamanho_lote = 1
        item_width = A4_WIDTH
        item_height = A4_HEIGHT
        posicoes = [(0, 0)]
        
    elif layout == 2:
        # Divide a folha na horizontal (2 placas médias, uma em cima da outra)
        tamanho_lote = 2
        item_width = A4_WIDTH
        item_height = A4_HEIGHT // 2
        posicoes = [
            (0, 0),                 # Metade de cima
            (0, item_height)        # Metade de baixo
        ]
        
    elif layout == 4:
        # Divide em 4 quadrantes (Padrão original)
        tamanho_lote = 4
        item_width = A4_WIDTH // 2
        item_height = A4_HEIGHT // 2
        posicoes = [
            (0, 0),                             # Superior Esquerdo
            (item_width, 0),                    # Superior Direito
            (0, item_height),                   # Inferior Esquerdo
            (item_width, item_height)           # Inferior Direito
        ]
        
    else:
        print("❌ Erro: O parâmetro 'layout' deve ser 1, 2 ou 4.")
        return

    extensoes_validas = ('.jpg', '.jpeg', '.png', '.bmp', '.tiff', '.webp')
    
    if not os.path.exists(pasta_entrada):
        print(f"❌ Erro: Pasta '{pasta_entrada}' não encontrada.")
        return

    arquivos = [f for f in os.listdir(pasta_entrada) if f.lower().endswith(extensoes_validas)]
    arquivos.sort()
    
    if not arquivos:
        print("❌ Nenhuma imagem encontrada.")
        return

    paginas_geradas = []

    # Processa em blocos de acordo com o tamanho_lote configurado
    for i in range(0, len(arquivos), tamanho_lote):
        pagina_atual = Image.new('RGB', (A4_WIDTH, A4_HEIGHT), 'white')
        lote = arquivos[i : i + tamanho_lote]
        
        for indice, arquivo in enumerate(lote):
            caminho_img = os.path.join(pasta_entrada, arquivo)
            
            try:
                with Image.open(caminho_img) as img:
                    if img.mode != 'RGB':
                        img = img.convert('RGB')
                    
                    # Redimensiona para o tamanho exato da fatia calculada
                    img_distorcida = img.resize((item_width, item_height), Image.Resampling.LANCZOS)
                    
                    # Cola diretamente na posição correta
                    pagina_atual.paste(img_distorcida, posicoes[indice])
                    
            except Exception as e:
                print(f"❌ Erro em {arquivo}: {e}")
        
        paginas_geradas.append(pagina_atual)

    # Salva o PDF final
    if paginas_geradas:
        primeira = paginas_geradas[0]
        outras = paginas_geradas[1:] if len(paginas_geradas) > 1 else []
        
        primeira.save(
            nome_saida, 
            "PDF", 
            resolution=300.0, 
            save_all=True, 
            append_images=outras
        )
        print(f"\n✅ Sucesso! PDF gerado: {nome_saida}")
        print(f"Layout escolhido: {layout} imagem(ns) por página.")
        print(f"Total de imagens processadas: {len(arquivos)}")
        print(f"Total de páginas: {len(paginas_geradas)}")

if __name__ == "__main__":
    PASTA = "./ofertas_geradas" 
    
    # Exemplo 1: Gerar PDF com 4 imagens por página (Pequenas)
    # gerar_pdf_preenchimento_total(PASTA, "ofertas_pequenas.pdf", layout=4)

    # Exemplo 2: Gerar PDF com 2 imagens por página (Médias)
    # gerar_pdf_preenchimento_total(PASTA, "ofertas_medias.pdf", layout=2)

    # Exemplo 3: Gerar PDF com 1 imagem por página (Grandes)
    gerar_pdf_preenchimento_total(PASTA, "ofertas_grandes.pdf", layout=4)