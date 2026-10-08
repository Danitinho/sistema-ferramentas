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
| `pareamento.py` | pede acesso ao servidor e guarda o token da máquina |
| `tecla_esc.py` | ESC **segurado** (0,6 s) para o lote na hora |

## Instalar numa máquina

1. Python 3.13 (python.org) e, na pasta do projeto:
   `python -m pip install -r agente/requirements.txt`
2. Abra o RADGe e rode `agente/iniciar_reclassificacao.bat` (tela **Produtos**)
   ou `agente/iniciar_geral.bat` (tela **Entradas**).
3. Na primeira vez a janela preta diz "pediu acesso ao sistema": o
   administrador aprova a máquina no painel `/agente` ("Máquinas pedindo
   acesso", confira nome e IP). Pronto — o agente segue sozinho.

Não há token para gerar nem arquivo para editar: o `config.json` é criado e
preenchido pelo próprio agente (`pareamento.py`). O acesso é da **máquina** e
vale para os dois programas. Revogado no painel, o agente para; aberto de novo,
pede acesso outra vez. Deixar `"forcar_simulacao": true` no `config.json`
continua impedindo qualquer gravação naquela máquina.

Para parar o lote na hora, **segure ESC por um segundo** (um toque não para:
o ESC também fecha as caixas do ERP).

Os pedidos do agente geral são feitos no painel `/agente`; o resultado e o log
aparecem lá. Só usuário com **acesso ao agente** (liberado pelo admin em
Sistema › Usuários) manda pedido ou liga a reclassificação.

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
