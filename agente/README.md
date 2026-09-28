# Agentes do ERP (RADGe)

Programas **sem tela** que rodam na máquina do operador e controlam a janela do
RADGe por Win32 (pywinauto). Quem manda é o sistema web: ligar, pausar, bloco,
simulação e o **mapa dos campos do ERP** vêm do painel.

| arquivo | o quê |
|---|---|
| `reclassificacao.py` | agente da reclassificação merceológica (tela Produtos) |
| `geral.py` | agente geral: executa pedidos avulsos do painel `/agente` (ex.: abrir a última nota do fornecedor) |
| `entradas.py` | driver da tela Entradas (notas fiscais) usado pelo agente geral |
| `erp.py` | base comum: achar a janela, localizar campos pelo mapa, digitar, clicar, caixas de diálogo |
| `servidor.py` | conversa HTTP com o sistema (token no `X-Token`) |
| `tecla_esc.py` | ESC **segurado** (0,6 s) para o lote na hora |

## Instalar numa máquina

1. Python 3.13 (python.org) e, na pasta do projeto:
   `python -m pip install -r agente/requirements.txt`
2. No painel `/reclassificacao`, gere o token do operador.
3. Copie `agente/config.exemplo.json` para `agente/config.json` e preencha
   `servidor_url` e `token`. (O `config.json` não vai para o git.)
4. Abra o RADGe na tela **Produtos** e rode `agente/iniciar_reclassificacao.bat`.
   Deixe a janela preta aberta; o resto é pelo navegador.

Para parar o lote na hora, **segure ESC por um segundo** (um toque não para:
o ESC também fecha as caixas do ERP).

### Agente geral

1. No painel `/agente`, gere o token da máquina (nome do computador no Windows).
2. Cole em `token_geral` no mesmo `agente/config.json`.
3. Abra o RADGe na tela **Entradas** e rode `agente/iniciar_geral.bat`.
4. Os pedidos são feitos no painel; o resultado e o log aparecem lá.

Os dois programas podem ficar abertos juntos, mas **não peça tarefa ao geral
com a reclassificação rodando na mesma máquina**: os dois digitam no mesmo ERP
e disputariam o teclado. Pause a rodada antes. O geral só mexe na tela ativa
do ERP e confere o título antes de começar.

## O que o agente faz com cada produto

1. confere no servidor se o produto ainda é seu (`/api/item`);
2. limpa o formulário e busca pelo código de barras;
3. compara a descrição da tela com a da planilha — abaixo do `limiar`, não grava;
4. se já está no destino (e `pular_certos`), fecha como `ja_correto`;
5. traz a aba Grupos, digita departamento/seção/subseção, **confere o que o ERP
   aceitou**, e só então salva;
6. caixa de recusa do ERP (NCM, descrição...) vira `erro` com o texto exato; a
   caixa é fechada para não travar os próximos.

Caixa durante a digitação nunca é respondida com "Sim" ("quer cadastrar?"
criaria uma seção). Formulário que não esvazia para tudo e pede parada no
painel.

## Testar sem o ERP

O agente aceita `fabrica_driver=` com um driver de mentira (mesma interface de
`DriverProdutos`: `conectar`, `viva`, `limpar`, `buscar`, `gravar`). Rode o
servidor contra uma **cópia** do `reclassificacao.db` (receita no CLAUDE.md,
seção 10-B, "Como testar").
