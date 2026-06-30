import os
from PIL import Image, ImageOps

def girar_e_adicionar_borda(pasta, angulo=90, espessura_borda=2, cor_borda="black"):
    # Verifica se a pasta existe
    if not os.path.exists(pasta):
        print(f"❌ A pasta '{pasta}' não foi encontrada.")
        return

    # Extensões de imagem aceitas
    extensoes_validas = ('.png', '.jpg', '.jpeg')
    contador = 0

    # Define o tipo de rotação baseada no ângulo desejado
    # ROTATE_90 gira no sentido anti-horário.
    if angulo == 90:
        metodo_rotacao = Image.Transpose.ROTATE_90
    elif angulo == 270 or angulo == -90:
        metodo_rotacao = Image.Transpose.ROTATE_270
    else:
        print("❌ Ângulo inválido para transposição exata (use 90 ou 270).")
        return

    print(f"Processando imagens na pasta '{pasta}'...")
    print(f"Configuração: Girar {angulo}°, Borda {espessura_borda}px {cor_borda}\n")

    for nome_arquivo in os.listdir(pasta):
        if nome_arquivo.lower().endswith(extensoes_validas):
            caminho_completo = os.path.join(pasta, nome_arquivo)
            
            try:
                with Image.open(caminho_completo) as img:
                    
                    # PASSO 1: Girar a imagem (ajusta largura/altura automaticamente)
                    img_processada = img.transpose(metodo_rotacao)
                    
                    # PASSO 2: Adicionar a borda preta
                    img_processada = ImageOps.expand(
                        img_processada, 
                        border=espessura_borda, 
                        fill=cor_borda
                    )
                    
                    img_processada.save(caminho_completo)
                
                print(f"✅ Processada: {nome_arquivo}")
                contador += 1
                
            except Exception as e:
                print(f"❌ Erro ao processar '{nome_arquivo}': {e}")

    print(f"\n🎉 Concluído! {contador} imagens foram giradas e receberam borda.")


# --- MODIFICAÇÃO: envolvido em if __name__ para não executar ao ser importado ---
if __name__ == "__main__":
    pasta_das_ofertas = "ofertas_geradas"
    girar_e_adicionar_borda(pasta_das_ofertas, angulo=90, espessura_borda=2, cor_borda="black")
