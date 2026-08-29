# CLAUDE.md — Sistema de Ferramentas

Guia de contexto para desenvolvimento assistido (Claude Code). Descreve a
arquitetura, convenções e armadilhas reais deste projeto. Leia antes de editar.

---

## 1. Visão geral

Aplicação web interna de um supermercado, em **Flask**, que reúne várias
ferramentas operacionais numa só interface. Roda num PC da loja e é acessada
pelo navegador de outros dispositivos (celulares/tablets/PCs) pela **rede
local**. Idioma do projeto: **português (pt-BR)** — nomes de rotas, variáveis,
templates e mensagens são em português; mantenha esse padrão.

Ferramentas atuais, agrupadas por setor:
- **Cadastro:** calculadora de preço, placas de oferta, placas hortifruti,
  relatórios de venda, **controle de vencidos** (avisos → vencidos → baixa).
- **Loja:** lote e vencimento.
- **Financeiro:** débitos, pagamentos e alocações (acompanhamento de quitação
  NF-a-NF).
- **Sistema:** login/usuários e backup/restauração.

Algumas ainda são *placeholders* (ver seção 10).

> **Acesso protegido por login.** Há uma guarda global (`before_request`) que
> exige sessão para tudo, menos `/login`, `/setup` e estáticos. No primeiro
> acesso (banco de usuários vazio) o sistema leva para `/setup` para criar o
> administrador. A guarda é *fail-open*: se o módulo de auth não carregar, o
> sistema segue no ar sem login (aviso no log) — disponibilidade acima de tudo.

---

## 2. Stack e como rodar

- **Python 3.13**, Flask 3.
- **Dois entrypoints:**
  - `app.py` — desenvolvimento. Sobe o servidor de dev do Flask na **porta
    5000** (`python app.py`). Mostra o IP local no terminal.
  - `server.py` — produção. Usa **waitress** na **porta 80**
    (`python server.py`). É este que roda como serviço Windows via NSSM.
- **Persistência:** sem banco central único, mas o padrão hoje é **SQLite** por
  módulo (ver seção 6). Débitos, relatórios, usuários e vencidos usam SQLite;
  layouts de placa usam JSON. Todos os bancos SQLite ganham backup automático
  (ver seção 13).

Rodar em desenvolvimento:
```bash
pip install -r requirements.txt
python app.py          # http://localhost:5000
```

Rodar como em produção (local):
```bash
python server.py       # http://localhost:80  (requer waitress)
```

### Dependências (`requirements.txt`)
- `flask` — framework web.
- `waitress` — servidor WSGI de produção (usado por `server.py`).
- `pillow` — geração/manipulação de imagens das placas de oferta.
- `pdfplumber` — extração dos relatórios Curva ABC em PDF.
- `openpyxl` — leitura/escrita de planilhas Excel (débitos e relatórios).

> Nota: `waitress` foi adicionado ao `requirements.txt`. Ele era importado por
> `server.py` mas estava ausente — se você atualizar um ambiente antigo, rode
> `pip install -r requirements.txt` de novo.

---

## 3. Estrutura de pastas

```
sistema_ferramentas_refatorado/
├── app.py                 # entrypoint dev + rotas diretas (menu, cadastro, loja)
├── server.py              # entrypoint produção (waitress, porta 80)
├── requirements.txt
├── CLAUDE.md              # este arquivo
│
├── scripts/               # CAMADA DE LÓGICA (sem Flask, exceto *_routes.py)
│   ├── __init__.py
│   ├── debitos.py             # lógica + persistência (SQLite) de débitos
│   ├── debitos_routes.py      # Blueprint /debitos
│   ├── fornecedores.py        # cadastro central de fornecedores (seção 14)
│   ├── fornecedores_routes.py # Blueprint /fornecedores (APIs do seletor)
│   ├── gerador_layouts.py     # motor de layouts de placas (Pillow)
│   ├── gera_imagem.py         # geração de imagem de oferta (modelo antigo)
│   ├── gera_imagem_v2.py      # geração de imagem de oferta (modelo novo)
│   ├── gera_pdf.py            # monta PDF final das placas
│   ├── gira_imagem.py         # rotaciona/adiciona borda às imagens
│   ├── layouts_routes.py      # Blueprint /layouts (placas de oferta)
│   ├── curva_abc.py           # EXTRAÇÃO TESTADA de PDF Curva ABC (NÃO ALTERAR — seção 7)
│   ├── relatorios_vendas.py   # lógica + persistência (SQLite) de relatórios
│   ├── relatorios_routes.py   # Blueprint /relatorios
│   ├── reclassificacao.py     # fila + curadoria + controle do agente (seção 10-B)
│   ├── reclassificacao_estrutura.py  # estrutura merceológica e validação do trio
│   └── reclassificacao_routes.py  # Blueprint /reclassificacao + API por token
│
├── templates/             # CAMADA DE VIEW (Jinja2, todas estendem base.html)
│   ├── base.html              # layout + DESIGN SYSTEM (paleta, responsivo) — seção 8
│   ├── index.html             # menu principal (grade de ferramentas + busca)
│   ├── cadastro/              # calculadora, placas_ofertas, placas_hortifruti, registro_perda
│   ├── loja/                  # lote_vencimento
│   ├── debitos/               # debitos_index, debitos_empresa
│   ├── layouts/               # index, cadastrar, gerar
│   ├── relatorios/            # index (processar PDFs + consultar por código de barras)
│   └── reclassificacao/       # painel do lote + console das máquinas, curadoria
│
├── assets/                # estáticos servidos em /assets/<arquivo>
│   ├── *.ttf                  # fontes das placas (Anton, ChelseaMarket, impact)
│   ├── *.png/.jpeg            # modelos de placa
│   └── layouts/*.json         # DEFINIÇÕES DE LAYOUT das placas (dados persistidos)
│
├── dados/                 # dados de runtime
│   ├── debitos.xlsx           # base de débitos/bonificações
│   ├── reclassificacao.db     # estado do lote de reclassificação (seção 10-B)
│   ├── estrutura_grupos.json  # departamentos > seções > subseções (referência)
│   └── relatorios/            # criado em runtime pelo módulo de relatórios
│       ├── entrada/  processados/  saida/   # fluxo de PDFs
│       └── vendas.db          # banco SQLite consolidado
│
├── uploads/               # uploads temporários (ex.: lista de produtos das placas)
└── outputs/               # saídas geradas (ex.: ofertas_final.pdf)
```

---

## 4. Arquitetura — padrão de 3 camadas

Cada módulo grande segue a mesma separação. **Siga este padrão ao criar um
módulo novo.**

1. **Lógica** (`scripts/<modulo>.py`): regras de negócio + persistência. Não
   importa Flask. Funções puras e testáveis.
2. **Rotas** (`scripts/<modulo>_routes.py`): um **Blueprint** Flask com
   `url_prefix="/<modulo>"`. Só orquestra request/response e chama a camada de
   lógica. Registrado em `app.py`.
3. **View** (`templates/<modulo>/*.html`): estende `base.html` e preenche os
   blocos. JS de página no bloco `extra_scripts`.

Blueprints registrados em `app.py` via **registro tolerante** (`registrar_modulos`):
cada módulo é importado isoladamente num try/except. Se um módulo tiver erro de
import/sintaxe, ele é **pulado com um aviso** (`MODULOS_COM_FALHA`) e os demais
continuam funcionando — mexer num módulo não derruba o sistema inteiro. A lista
de módulos fica em `MODULOS`:
```python
MODULOS = [
    ("scripts.auth_routes",       "auth_bp"),       # /login, /setup, /sistema/usuarios
    ("scripts.debitos_routes",    "debitos_bp"),     # /debitos
    ("scripts.layouts_routes",    "layouts_bp"),     # /layouts
    ("scripts.relatorios_routes", "relatorios_bp"),  # /relatorios
    ("scripts.vencidos_routes",   "vencidos_bp"),     # /vencidos
    ("scripts.fornecedores_routes", "fornecedores_bp"), # /fornecedores (APIs)
    ("scripts.fiscal_routes",     "fiscal_bp"),       # /fiscal (custo real por NF-e)
    ("scripts.backup_routes",     "backup_bp"),       # /sistema/backup
]
```
Ao adicionar um módulo novo, some uma entrada aqui — **não** volte a importar o
blueprint no topo do arquivo. As rotas de `cadastro/*` (calculadora) e `loja/*`
ainda estão diretas no `app.py`; ao evoluí-las, considere movê-las para
blueprints próprios.

---

## 5. Módulos e rotas

### Menu e setores simples (`app.py`)
- `GET /` → menu principal (`index.html`).
- `GET /cadastro/calculadora` + `POST /cadastro/calculadora/calcular`
  → calcula preço de venda por margem e imposto (lógica inline no `app.py`).
- `GET /cadastro/placas-hortifruti` + `POST .../gerar` → **placeholder**.
- `GET /cadastro/registro-perda` + `POST .../salvar` → **placeholder**.
- `GET /loja/lote-vencimento` + `POST .../consultar` → **placeholder**.
- `GET /assets/<arquivo>` → serve estáticos da pasta `assets/`.

### Placas de oferta — Blueprint `/layouts` (`layouts_routes.py` + `gerador_layouts.py`)
Editor/gerador de placas de preço a partir de uma lista de produtos, usando
**Pillow**. Layouts são definições em `assets/layouts/*.json`. Rotas principais:
listar (`/`), cadastrar, editar/excluir layout, gerar (`/<id>/gerar`),
PDF (`/pdf/<arquivo>`), imprimir, preview. O template `cadastro/placas_ofertas.html`
existe mas a geração efetiva passa pelo blueprint `/layouts`.

### Débitos, pagamentos e alocações — Blueprint `/debitos` (`debitos_routes.py` + `debitos.py`)
Controle por empresa (CNPJ) com **acompanhamento de quitação NF-a-NF**.
Persistido em `dados/debitos.db` (SQLite). Modelo:
- `debitos` — o que a empresa deve (vencimento por NF ou rebaxa). Acumula
  `valor_pago`; o status (aberto/parcial/quitado) é derivado.
- `pagamentos` — abatimentos, de três **`tipo`**s: `bonificacao` (NF),
  `troca` (troca direta de produtos) ou `desconto_boleto`. Cada um tem uma
  `referencia` (nº NF / nº boleto / descrição — rótulo em `REF_LABEL`). Acumulam
  `valor_alocado`; o resto é o crédito disponível.
- `alocacoes` — ligação N:N: "R$ X do pagamento P quitou o débito D". Excluir um
  débito ou pagamento **reverte automaticamente** suas alocações (soft-delete).

**Fluxo (débito-first):** `adicionar_pagamento(cnpj, valor, tipo, referencia,
..., debito_id=?)`. Com `debito_id`, o valor abate aquele débito e o **excedente
vira crédito** automaticamente; sem `debito_id`, entra como **crédito avulso**.
O crédito (pagamento com `disponivel > 0`, ver `listar_creditos`) pode depois
quitar outro débito via `alocar` (manual) ou `alocar_automatico` (FIFO). Só a
NF de **bonificação** é barrada contra duplicata; troca/desconto podem repetir.
Na tela, cada débito é um **bloco** com seus pagamentos dentro; há um **pool de
crédito** no topo. (O antigo `pagamentos.nf_numero` foi migrado para `referencia`
com rebuild não destrutivo da tabela; a antiga `bonificacoes` já vira `tipo=bonificacao`.)

**Vendedor (dívidas separadas na mesma empresa):** uma empresa pode ter mais de
um vendedor, um por setor, e **cada um responde pelos próprios débitos** — são
dívidas separadas que só compartilham o CNPJ. `debitos.vendedor` e
`pagamentos.vendedor` (colunas por migração, **opcionais** — em branco = empresa
de vendedor único, e é o caso dos débitos antigos). Regras:
- o nome é gravado **como digitado**, mas todo agrupamento e comparação usam a
  **chave normalizada** (`vendedor_chave` = maiúsculas, sem espaço sobrando);
  `_canon_vendedor` ainda reaproveita a grafia já usada naquela empresa, para
  "Vanusa"/"VANUSA" não aparecerem como dois nomes;
- o pagamento **herda o vendedor do débito** que abate; só o **crédito avulso**
  pergunta o vendedor (campo aparece apenas nesse modo no modal);
- `alocar` **barra** crédito de um vendedor em débito de outro (inclusive em
  débito sem vendedor) e `alocar_automatico` só faz FIFO **dentro do mesmo
  vendedor** — foi decisão explícita bloquear, não só avisar;
- trocar o vendedor de um débito **não** mexe nos pagamentos já alocados (eles
  foram lançados sob o vendedor de então);
- `listar_debitos`/`listar_creditos`/`calcular_saldo` aceitam `vendedor=`
  (`'sem'` = os sem vendedor); `vendedores_empresa(cnpj)` alimenta o seletor de
  filtro e o `<datalist>` de sugestão do formulário.

**Período do débito:** cada débito tem um período de referência (obrigatório ao
criar) — `periodo_tipo` (`mes` | `intervalo`) + `periodo_inicio`/`periodo_fim`
(datas ISO). "Mês fechado" vira 1º→último dia do mês; "intervalo" guarda as duas
datas. O `periodo_label` (ex.: "junho/2026" ou "05/04/2026 a 30/06/2026") aparece
no bloco. A página filtra por mês (seletor no topo, `?mes=AAAA-MM` server-side):
um débito com **intervalo aparece em TODOS os meses que cobre** (sobreposição
`inicio <= último_dia_do_mes AND fim >= primeiro_dia`). `meses_debitos(cnpj)` lista
os meses presentes; `?mes=sem` traz débitos antigos sem período. Débitos antigos
recebem as colunas por migração (ficam sem período).

API JSON em `/debitos/api/...`: `debito/vencimento`, `debito/rebaxa`,
`debito/<id>/editar` (corrige NF/valor/produto/descrição/período dentro do mesmo
tipo; barra NF duplicada de outro débito e valor abaixo do já pago; ação `editar`
na auditoria), `pagamento` (aceita `tipo`, `referencia`, `debito_id`), `alocar`,
`alocar/auto`, `desalocar` (+ DELETEs). `/api/bonificacao` segue como **alias
legado** de `/api/pagamento` (mapeia `nf_numero`→`referencia`, `tipo=bonificacao`).
Na tela, cada bloco de débito tem os botões **excluir** e **editar** empilhados;
a edição reaproveita o modal de cadastro em modo `editar` (tipo fixo, campos
pré-preenchidos).
> **Atenção:** a rota é `POST /debitos/api/debito/rebaxa` (grafia "rebaxa", sem
> "i"). É um typo que o frontend já consome — **não "corrija" sem atualizar o
> JS correspondente**, senão quebra.

**Relatório do fechamento** (`scripts/debitos_relatorio.py` + `GET /debitos/relatorio`
e `/relatorio/excel`): fecha o mês juntando os débitos do mês **e os do mês anterior**
— as notas de junho são pagas ao longo de julho. Só leitura, nenhuma tabela nova.
Regra de competência, que é o coração do módulo:
- o **débito** pertence ao mês do seu **período de referência**, nunca ao
  `debitos.data` (as notas de julho são digitadas em agosto — a data de
  digitação mentiria);
- o **pagamento herda o mês do débito que abate**, então uma bonificação
  digitada depois do fechamento ainda conta no mês certo;
- como um pagamento pode se repartir entre débitos de meses diferentes
  (`alocar_automatico` faz FIFO), a **unidade é a ALOCAÇÃO, não o pagamento** —
  cada linha traz o valor aplicado e o total de origem (`parcial`);
- dinheiro **sem débito** (crédito avulso, sobra de pagamento, abatimento de
  débito antigo sem período) não tem mês por essa regra: cai no mês em que foi
  **lançado**, na seção "crédito não aplicado", **fora dos totais** do fechamento.
A unidade do resumo é a **dívida = (empresa, vendedor)**, não a empresa: uma
empresa com dois vendedores rende duas linhas, "MARQUES E MELO (VANUSA)" e
"MARQUES E MELO (FABIOLA)" (`_grupo`). A coluna "Vendedor" só entra nas abas
quando alguma dívida do período tem um (`tem_vendedor`).
Débito com período em **intervalo** que cobre os dois meses conta **uma vez só**,
no mês do relatório (senão o total dobra). Mês padrão = **mês anterior ao
corrente** (é o fechamento em pauta). `cnpj` opcional na query: sem ele é o
consolidado de todas as empresas, com ele a empresa só — a coluna/aba "Empresa"
some. O relatório é **retroativo** por natureza (novo lançamento muda um mês já
impresso), por isso é carimbado com `gerado_em`.

**Leitura em 3 partes** (`rel["partes"]` + `rel["consolidado"]`) — e **nenhum
número somado dos dois meses aparece antes da parte 3**, que foi o pedido
explícito: (1) débitos do mês, (2) débitos do mês anterior, onde estão os
pagamentos mandados ao longo do mês, (3) consolidado. Cada parte é fechada em si:
totais próprios + quebra por dívida (`_por_empresa_parte`); o consolidado usa
`_por_empresa_consolidado`, com as duas colunas de mês. O **crédito livre não
entra no "total pago"** — ainda não abateu nada, somá-lo inflaria o abatimento;
aparece numa linha à parte. `mostrar_quebra` esconde a quebra quando ela não
informa nada (uma empresa só, sem vendedores).

**Saídas** (`GET /debitos/relatorio/pdf` e `/relatorio/excel`, ambas BytesIO):
o **PDF** é o documento de apresentação (capa executiva + as 3 partes) e o
**Excel** é a planilha de análise (4 abas planas com autofiltro). Detalhes na
seção 16 — inclusive por que o antigo Excel de UMA página A4 foi aposentado.
A coluna 1 das tabelas é adaptativa (`_rotulo_linha`/`_titulo_col1` no
`debitos_pdf.py`): "Empresa / vendedor" no consolidado, "Vendedor" na empresa
única com vendedores, "Tipo" quando nenhum dos dois informa nada.

`rel["por_tipo"]` (`_abatimento_por_tipo`) agrega o abatimento por forma de
pagamento (bonificação/troca/desconto) para a capa do PDF. A unidade é a
**alocação**: crédito ainda livre não abateu nada e fica de fora.

### Relatórios de venda — Blueprint `/relatorios` (ver seção 7, é o mais novo)

### Reclassificação merceológica — Blueprint `/reclassificacao` (ver seção 10-B)

Fila do lote de reclassificação do cadastro, **a curadoria dos destinos** e o
**console das máquinas**. A outra metade é um agente sem tela que roda na máquina
do operador e controla o ERP; ele obedece ao que é decidido aqui. **A seção 10-B
tem o contrato da API que esse agente consome** — mudar campo ali quebra ele sem
aviso.

---

## 6. Persistência (mista — atenção)

Não há um banco único. Cada módulo persiste de um jeito:

| Módulo            | Onde                          | Formato |
|-------------------|-------------------------------|---------|
| Débitos/pagamentos/alocações | `dados/debitos.db` | SQLite |
| Fornecedores (cadastro central) | `dados/fornecedores.db` | SQLite |
| Fiscal (NF-e compra, custo, preço, produtos) | `dados/fiscal.db` | SQLite |
| Produtos vencidos | `dados/vencidos.db`           | SQLite |
| Usuários + SECRET_KEY | `dados/sistema.db` + `dados/secret.key` | SQLite + arquivo |
| Layouts de placa  | `assets/layouts/*.json`       | JSON |
| Relatórios venda  | `dados/relatorios/vendas.db`  | SQLite |
| Reclassificação merceológica | `dados/reclassificacao.db` | SQLite |
| Estrutura merceológica (referência, versionada) | `dados/estrutura_grupos.json` | JSON |
| Backups           | `backups/<banco>/*.db`        | cópias SQLite datadas |
| Uploads temporários | `uploads/`                  | arquivos soltos |
| Saídas geradas    | `outputs/`                    | PDF/imagens |

> O antigo `dados/debitos.xlsx` é legado e não é mais usado. A tabela
> `bonificacoes` foi migrada (não destrutivamente) para `pagamentos`; a
> migração roda sozinha ao abrir o banco e é idempotente.

**Convenções de confiabilidade (siga em módulos que gravam dado que importa):**
- Datas em **ISO** (`AAAA-MM-DD HH:MM:SS`) para ordenação correta; formate para
  exibição com um `data_fmt`.
- **Soft-delete** (`excluido_em`/`excluido_por`), nunca `DELETE` físico; filtre
  `WHERE excluido_em IS NULL` nas consultas.
- **Auditoria**: registre criar/excluir na tabela `auditoria` com o `usuario`
  (vindo de `session.get("usuario")` na camada de rotas).
- `PRAGMA busy_timeout = 5000` na conexão (evita erro imediato sob concorrência).

Ao criar um módulo novo, prefira **SQLite** quando houver consulta/cruzamento de
dados (é o caminho adotado no módulo mais recente). Excel/JSON só quando o
artefato em si precisa ser aberto por humanos.

---

## 7. Módulo Relatórios de Venda (detalhado)

Converte relatórios "Curva ABC de Vendas de Produtos" (sistema RADInfo) de PDF
para Excel **e** consolida tudo num SQLite, permitindo consultar quanto cada
produto vendeu mês a mês por código de barras.

### Arquivos
- `scripts/curva_abc.py` — **extração por coordenadas (X/Y) do PDF** + geração
  do Excel mensal. **É código testado e validado em produção. NÃO REESCREVER a
  lógica de extração** (`parse_page`, `extrair_dados`, `COL_BOUNDS`,
  `VALOR_PAT`, `PERC_PAT`). Mexer aqui quebra a leitura de PDFs reais. Se
  precisar de outro layout de PDF, adicione um caminho novo, não altere o atual.
- `scripts/relatorios_vendas.py` — orquestração: SQLite, processamento da pasta
  de entrada, consulta por código de barras, Excel da consulta.
- `scripts/relatorios_routes.py` — Blueprint `/relatorios` (página + APIs).
- `templates/relatorios/index.html` — UI: consulta em destaque; o
  processamento de PDFs fica num **modal** disparado por um botão discreto
  ("⚙️ Atualizar banco de dados — uso mensal"), para evitar que arrastem um PDF
  errado por acidente.

### Fluxo de pastas (`dados/relatorios/`, criadas em runtime)
`entrada/` (PDFs novos) → processa → `saida/` (Excel mensal `curva_abc_AAAA-MM.xlsx`)
+ grava no `vendas.db` → move o PDF para `processados/`. PDF com erro **não** é
movido (fica na entrada para inspeção).

### Esquema SQLite
- `relatorios(mes PK, periodo, loja, total_geral, arquivo, processado_em)`
- `vendas(mes FK, intervalo, codigo, codigo_barras, descricao, qtd, valor_total, perc, classe)`
- índices em `codigo_barras`, `codigo`, `mes`.

### Regras de negócio confirmadas com o usuário
- **Mês** identificado pela **data inicial** do período dentro do PDF (`AAAA-MM`).
- **Reprocessar** um mês **substitui** os dados daquele mês (delete + insert;
  reprocessar é idempotente, não duplica).
- Na consulta, mostrar a **descrição mais recente** entre os meses.
- Resultado da consulta sai **na tela e em Excel** (botão baixar).

### Janela de meses da consulta (`meses=` nas duas APIs)
Os meses se acumulam (7 hoje, e crescendo), então a consulta é recortável:
`consultar_codigos(codigos, meses=None)` + `filtrar_meses`. A janela vale para os
**três** destinos ao mesmo tempo — tabela, gráfico e Excel —, porque a resposta
já vem só com os meses escolhidos.
- **`meses` vazio/None = todos** e é o padrão. Foi escolha: assim um mês recém
  importado nunca fica de fora sem alguém pedir. Por isso também **não há
  persistência** da seleção entre visitas — uma lista salva em `localStorage`
  esconderia silenciosamente o mês seguinte.
- Mês pedido que não existe no banco é **ignorado**; se sobrar nada, cai em
  todos (rede de segurança do servidor — a tela barra antes, com aviso).
- O código continua **"encontrado" mesmo sem venda na janela**: ele existe no
  banco, só não vendeu no período. Dizer "não encontrado" ali seria mentira.
  Daí a descrição vir do último mês com registro (qualquer um), enquanto `qtds`
  e `total` olham só a janela.
- `escopo_consulta` monta o rótulo da janela **num lugar só** (tela e Excel dizem
  a mesma coisa, campo `escopo` da resposta). Seleção **não contígua** é listada
  mês a mês — "jan/2026 a jul/2026" para uma escolha de jan+jul seria mentira
  (`_sequencia_continua`).
- O Excel ganhou **linha 1 de escopo** (e por isso o cabeçalho foi para a linha
  2, congelamento em `D3`); o nome do arquivo carrega a janela
  (`consulta_vendas_2026-05_a_2026-07.xlsx`). Sem isso, duas exportações de
  janelas diferentes ficariam indistinguíveis.
- Na tela, chips de mês (`.mes-chip`) + atalhos **Todos / Últimos 3 / 6 / 12** —
  o atalho só aparece quando encurta algo (com 7 meses, "Últimos 12" some).
  Depois de processar PDFs os chips são redesenhados preservando a marcação, e
  **mês novo entra marcado**. O botão de Excel exporta a janela **da consulta
  exibida** (`_consulta.meses`), não a dos chips: mexer nos chips sem
  reconsultar não pode gerar um arquivo diferente do que está na tela.

### Rotas
`GET /relatorios/` · `GET /relatorios/api/status` ·
`POST /relatorios/api/upload` (envia PDFs p/ a fila) ·
`POST /relatorios/api/processar` (processa a fila) ·
`POST /relatorios/api/consultar` (JSON `{codigos, meses?}`) ·
`POST /relatorios/api/consultar/excel` (mesmo corpo; baixa Excel pivotado).

---

## 8. Design system / frontend

Todo o visual mora em `templates/base.html`. **Mudar a paleta lá reskinna o
sistema inteiro**, pois todos os templates estendem `base.html`.

### Paleta atual — "Corporativo Navy" (tema claro padrão + escuro)
Definida em CSS variables no `:root` de `base.html`. O tema **claro é o
padrão**; o escuro é ativado por `html[data-theme="escuro"]` (switch no header,
persistido em `localStorage.tema`, aplicado antes do paint por um script
inline no `<head>`). Referência visual: mockup aprovado em
https://claude.ai/code/artifact/cef0d907-2727-4fbb-9720-f17610705803
- Superfícies: `--bg`, `--surface`, `--surface-2`, `--surface-3`, `--line`, `--line-2`.
- Texto: `--ink`, `--ink-2`, `--muted`.
- Marca: `--brand` (navy #1F3A5F, fundo de botão primário) + `--brand-2` (hover),
  `--accent` (azul de texto/ícone/link) + `--accent-2`/`--accent-soft`,
  `--on-brand` (texto sobre navy), `--glow` (anel de foco).
- Barra superior: `--appbar`/`--appbar-ink`/`--appbar-muted` — navy nos DOIS
  temas (âncora da identidade); o markup usa `.appbar-top-wrap` (largura total).
- Semânticas: `--success`, `--warning`, `--danger` (+ versões `-bg`) —
  dessaturadas de propósito; o âmbar agora é SÓ warning, não é mais a marca.
- Tipos: `--sans` = Segoe UI (nativa do Windows da loja) e `--mono` = Consolas
  (números, NF, códigos — use a classe `.num`). Corpo 16px (legibilidade).
- Forma: `--radius` 6px / `--radius-sm` 4px; bordas de 1px no lugar de
  sombras/brilhos; rótulos de card/tabela em caixa alta espaçada.
- Aliases mantidos por compatibilidade: `--amber`→`--accent`,
  `--amber-btn`→`--brand`, `--amber-ink`→`--on-brand`, `--amber-2`→`--accent-2`
  (+ os legados `--graphite`, `--gray`, `--border`...). Template antigo que usa
  âmbar vira azul sozinho — em código novo, use os nomes novos.
**Use sempre as variáveis, nunca hex cravado** — um hex claro cravado quebra o
tema escuro (e vice-versa). Ambos os temas precisam funcionar em toda tela nova.

### Responsividade
- `--maxw: 1200px` (largura do app no desktop), `--maxw-read: 880px` (coluna de
  leitura p/ formulários), `--pad-x` fluido via `clamp()`.
- Cartões de conteúdo (`main > .card`) ficam numa coluna centralizada legível.
  Para um bloco ocupar a largura toda, use a classe `.full`.
- Menu (`index.html`): grade `2 colunas (celular) → 3 (≥600px) → 4 (≥900px)`.

### Convenções de template
Todo template começa com:
```jinja
{% extends "base.html" %}
{% block title %}...{% endblock %}
{% block header_title %}...{% endblock %}
{% block breadcrumb %}<a href="/">Menu principal</a> · ...{% endblock %}
{% block extra_head %}<style>...</style>{% endblock %}
{% block content %}...{% endblock %}
{% block extra_scripts %}<script>...</script>{% endblock %}
```
Helpers globais já disponíveis em `base.html`: `postJSON(url, data)`,
`brl(valor)` (moeda BRL), o drag-and-drop automático de `.upload-area` e o
seletor de fornecedor `fornecedorPicker(input, opts)` (CSS `.fpick` — seção 14).

### Outras regras de UI
- **Ícones:** SVG **inline** (não emoji, não CDN). Decisão intencional: os
  aparelhos da loja podem não ter internet, então nada de depender de fontes de
  ícone externas. Ao adicionar um card no menu, copie o padrão de `<svg>` de
  linha já usado em `index.html`.
- A busca do menu filtra os cards no cliente via `data-nome` em cada `.tool-card`.
- Componentes prontos no `base.html`: `.card` (+ `.card-flush`/`.card-head`/
  `.card-body`), `.btn` (`.btn-primary` = navy, `.btn-secondary`, `.btn-danger`,
  `.btn-sm`), `.pill` (status `p-aberto`/`p-parcial`/`p-quitado`), `.tag`,
  `.chip`, `.stat-card` (`s-danger`/`s-success`/`s-amber`), `.alertx`, `.seg`
  (seletor segmentado), `.fseg` + `.search` (filtros/busca), `.ingrp` (campo com
  prefixo R$), o **modal global** (`.modal-overlay`/`.modal` + helpers JS
  `abrirModal`/`fecharModal`/`fecharSeFundo` — o fundo só fecha se o clique
  **começou** no fundo (`window._pressAlvo`, evita perder o formulário ao
  arrastar seleção para fora) e **Esc com campo focado não fecha**, só tira o
  foco (protege digitação e leitores de código de barras com sufixo ESC); o
  módulo de relatórios tem handlers próprios com a mesma regra), o livro-razão (`.ledger`,
  `.lg-head`/`.lg-row`), `.metric`, `.result-box`, `.upload-area`, `.badge`,
  `.spinner`, `.btn-lixo`, `.icon-btn`, `.empty-state`. Reutilize antes de criar novos.
- O header some numa tela ao sobrescrever `{% block header %}{% endblock %}`
  (usado em login/setup).
- **Prints em `scripts/*.py`: só ASCII.** Com stdout em cp1252 (terminal
  Windows), um `print("✓")` derruba a requisição inteira com UnicodeEncodeError.

---

## 9. Convenções de código

- Português em nomes, comentários e mensagens ao usuário.
- Camada de lógica (`scripts/<modulo>.py`) **não importa Flask**.
- Caminhos sempre relativos à raiz do projeto via
  `BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))`
  (padrão já usado em `debitos.py`, `relatorios_vendas.py`, `gerador_layouts.py`).
- Pastas de runtime criadas com `os.makedirs(..., exist_ok=True)` — não comite
  conteúdo gerado.
- APIs JSON retornam `{"ok": bool, ...}` ou `{"erro": "..."}`; o frontend
  consome com `postJSON`.

---

## 10. Pendências e dívidas conhecidas

- **Placeholders** (retornam `{"status": "em breve"}`): `placas-hortifruti/gerar`,
  `lote-vencimento/consultar`. As telas existem; falta a lógica. (O antigo
  placeholder `registro-perda` foi substituído pelo módulo real `/vencidos`; a
  rota antiga ainda existe no `app.py` mas o menu já aponta para `/vencidos`.)
- `cadastro/*` (calculadora) e `loja/*` ainda são rotas diretas no `app.py` —
  candidatas a virar blueprints.
- Typo proposital/legado: rota `/debitos/api/debito/rebaxa` (ver seção 5).
- Há arquivos de mídia do WhatsApp (`.ogg`, `.mp4`) espalhados na raiz, em
  `scripts/` e `templates/layouts/` — são lixo de desenvolvimento e podem ser
  removidos (o `.gitignore` já os ignora).

---

## 10-B. Reclassificação merceológica (`/reclassificacao`)

Incorporado em 28/08/2026, vindo do projeto `C:\dev\pythonprojects\reclassificador`.
Esta seção é autossuficiente: dá para desenvolver o módulo sem abrir o outro
projeto. O que estiver marcado como **contrato** é consumido por um programa que
mora fora daqui — mudar quebra ele em silêncio.

### O problema de negócio

O supermercado está migrando o cadastro para uma estrutura merceológica nova.
**65.876 produtos** estão em departamentos legados e precisam de departamento,
seção e subseção corrigidos no ERP RADGe. À mão, produto por produto, é
inviável.

Volumes: 27.414 ativos. Por confiança da sugestão: ALTA 38.506 · MÉDIA 18.710 ·
BAIXA 4.657 · REVISAR 4.003. O trabalho real são **26.096 ativos com destino**,
e **nenhum deles já está correto** — todo item da lista precisa mesmo mudar.
Desses, 16.852 são ALTA (podem rodar no automático) e 9.244 são MÉDIA/BAIXA, que
exigem confirmação humana e são o gargalo de verdade. O departamento 12 sozinho
é 47% do lote. A ~3 s por produto, são mais de 21 horas de máquina.

### A inversão (28/08/2026) — leia antes de tudo

O sistema tinha duas metades: esta, web, e um **programa de mesa** com janela
tkinter na máquina do operador. A janela sumiu. Não foi maquiagem:

```
ANTES  reservar -> [o operador confirma produto a produto, ERP parado] -> gravar
AGORA  curadoria no navegador -> reservar -> gravar sem parar para perguntar
```

A confirmação humana **não desapareceu; mudou de momento**. Continua havendo uma
pessoa por trás de cada gravação — só que ela decide antes, em lote, e não com o
ERP travado esperando. Três consequências que explicam quase todo o desenho
atual:

1. **`itens.pronto` é a fronteira, e ela é intransponível sem uma pessoa.**
   `reservar` só entrega `pronto=1`, e **nada nasce pronto** — nem os de
   confiança ALTA. Todo produto do lote espera confirmação em `/curadoria`.
2. **O programa da máquina virou um agente sem tela.** Ele pergunta o que fazer,
   executa e conta o que aconteceu. Ligar, pausar, configurar, ver o log: tudo
   no navegador.
3. **O gargalo virou trabalho paralelizável.** Ninguém mais precisa ficar
   sentado ao lado do ERP; qualquer pessoa cura de qualquer tela, inclusive fora
   do horário da loja.

### A confiança é indicativa, não é autorização

Decisão explícita do usuário (29/08/2026), depois de uma versão que liberava os
ALTA sozinhos: **nenhum produto é gravado no ERP sem alguém confirmar.**
ALTA/MÉDIA/BAIXA/REVISAR ordena a fila, escolhe a cor do rótulo e diz o quanto o
palpite da planilha merece atenção — só isso.

O porquê: o palpite acerta muito, mas "muito" não é "sempre", e `concluido` é
terminal. Um cadastro gravado errado no automático não tem desfazer, e ninguém
saberia que ele existe. 38.504 produtos ALTA gravados sem revisão é uma aposta
grande demais para um ganho que a curadoria em lote já entrega barato.

`_migrar` tem uma migração de uma vez só (marcada em
`config.migracao_curadoria_total`) que devolveu à curadoria os `pronto=1` sem
`curado_em` — isto é, os que a versão anterior tinha liberado sem ninguém olhar.
Itens já trabalhados e já curados não foram tocados.

### O que este módulo NÃO faz

**Ele não mexe no ERP.** Quem edita o RADGe é um **agente** (pywinauto) que roda
na máquina de cada operador e controla a janela do ERP por Win32 — coisa que
servidor web não alcança. Esse agente continua em
`C:\dev\pythonprojects\reclassificador` (`reclassificador/agente.py` +
`erp.py`), tem CLAUDE.md próprio, e é a **única** parte que não pôde subir para
a web. Tudo o mais migrou.

Na máquina do operador ficou: `servidor_url` e `token` no `config.json`. Só.
Bloco, simulação, limiar e até o **mapa dos campos do RADGe** vêm do servidor.

### Arquivos

- `scripts/reclassificacao.py` — fila, máquina de estados, curadoria, controle
  remoto do agente, tokens, leitura do xlsx e exportação. Sem Flask.
- `scripts/reclassificacao_estrutura.py` — a estrutura merceológica válida e a
  validação do trio (ver a armadilha abaixo). Sem Flask.
- `dados/estrutura_grupos.json` — 8 departamentos, 35 seções, 114 subseções.
  Mesmo arquivo do agente; é dado de referência e **está no git**.
- `scripts/reclassificacao_routes.py` — blueprint: páginas das pessoas + API do
  agente.
- `templates/reclassificacao/index.html` — painel do lote e **console das
  máquinas** (estado ao vivo, botões, parâmetros, log).
- `templates/reclassificacao/curadoria.html` — a bancada de decisão.
- `dados/reclassificacao.db` — estado do lote. Entra no backup automático
  sozinho, porque o agendador varre todo `.db` dentro de `dados/`.

### A armadilha dos códigos (leia antes de mexer em destino)

O ERP guarda a classificação em três campos: `CodGrp1` = departamento,
`CodGrp2` = seção, `CodGrp3` = subseção. **Os códigos se repetem entre níveis
com significados diferentes:**

| Código | Como seção | Como subseção |
|---|---|---|
| 3 | Higiene Pessoal | Controle de Pragas |
| 22 | Cervejas | Bovinos |
| 28 | Casa Geral | Alimentos Naturais |
| 36 | Cozinha | Salgados |
| 82 | Cosméticos e Perfumaria | Sucos e Néctares |

Também colidem `6`, `7` e `9` entre departamento e seção/subseção.

Consequência: **um trio na ordem errada produz cadastro que o ERP aceita sem
reclamar e que está semanticamente errado.**

Isto **agora é validado no servidor**, em `reclassificacao_estrutura.Estrutura.
validar()`, chamada dentro de `curar` e de `aceitar_sugestao` — não na tela. O
`<select>` em cascata do navegador ajuda o curador a escolher certo; ele não é
quem garante. Quem grava é a API. Se o JSON sumir, `carregada` fica falso,
`validar` reprova tudo com mensagem clara e a curadoria trava — de propósito:
melhor recusar do que aceitar trio não verificado. (Conferido: os 61.871
destinos sugeridos pela planilha passam todos na validação.)

### Contrato da planilha de entrada

Arquivo `RECLASSIFICACAO_PRODUTOS_V3.xlsx`, aba `Lista de Trabalho`. As colunas
são localizadas **pelo nome no cabeçalho**, nunca pela posição (`COLUNAS` em
`reclassificacao.py`): se o layout mudar, o carregamento falha com mensagem
clara em vez de ler a coluna errada calado.

`Cód. Barras` (chave; texto, pode ter zero à esquerda, de 4 a 13 dígitos) ·
`Produto` · `Ativo` (SIM/não) · `Últ. mov.` · `CodGrp1/2/3 atual` ·
`CodGrp1/2/3` (destino) · `Confiança` (ALTA|MÉDIA|BAIXA|REVISAR) ·
`Base da sugestão`.

A planilha é **semente, não estado**: depois da importação o que vale é o banco.
Reimportar é seguro — só acrescenta código que ainda não está lá e não encosta em
nada já trabalhado.

Duas armadilhas conhecidas: itens **REVISAR não têm destino** e entram como
`sem_destino` (e caem na curadoria, sem sugestão); e há **dois códigos de barras
repetidos** (`7898056270316`, `7896183901325`). Como `cod` é chave primária, a
segunda linha é descartada. Hoje os destinos das duplicatas são idênticos, então
não há perda — **se uma planilha futura trouxer duplicata com destinos
diferentes, isso precisa virar erro em vez de descarte silencioso.**

### A curadoria (`/reclassificacao/curadoria`)

É a metade que antes era o painel de confirmação do programa de mesa.

**A unidade da decisão é o produto, não o grupo.** A tela principal é uma
**conferência um a um**: um produto por vez, grande, com a classificação de hoje
e o destino proposto lado a lado, e uma tecla por decisão — `Enter` aprova, `D`
muda o destino, `P` pula, `X` descarta, `Z` desfaz o último.

Foi uma correção de rumo. Uma versão anterior era só a lista com "Confirmar
todos os N", e isso não é aprovação individual: é aprovação automática com um
nome atribuído — o mesmo que se acabara de tirar dos ALTA, com um clique a mais.

**O lote continua existindo, mas é oferecido, não oferecido de graça.** Depois de
`SEGUIDOS_PARA_OFERECER` (3) aprovações **seguidas para o mesmo destino**,
aparece "você aprovou 3 seguidos para X, faltam N iguais — aprovar o resto?"
(tecla `L`). A sequência é do **destino**, não do produto, e **qualquer coisa que
não seja aprovar a quebra** (`quebrarSequencia`): pular, corrigir o destino,
descartar, trocar de filtro. A oferta afirma "você viu N deste grupo e estavam
certos"; uma correção no meio torna essa afirmação falsa.

Detalhes que fazem o fluxo aguentar 27 mil produtos:

- **O `Enter` não espera a rede.** As aprovações entram numa fila local e sobem
  em lotes de 8 (ou a cada 1,5 s). Se o navegador fechar antes de subir, o
  produto continua pendente — o erro cai para o lado seguro, que é não aprovar
  nada sem querer.
- **A fila é buscada 200 por vez e recarregada quando faltam 25.** O `Enter`
  nunca encosta na latência.
- **`descurar(cods)` é o desfazer** e só funciona enquanto o item está `livre`.
  Assim que um agente reserva ou fecha, a janela fecha: voltar atrás no banco não
  desfaz o que já foi escrito no ERP. A tela mostra "tarde demais" com o estado.
  Sem desfazer, um fluxo de uma tecla ensinaria o curador a hesitar — que é
  exatamente o que o torna lento.
- Os atalhos **não disparam com um modal aberto nem com o foco num campo**: o
  "D" de "DOCE" digitado no motivo do descarte não pode abrir a tela de destino.

A **vista em lista** (`/curadoria/lista`) continua, para achar um produto pelo
nome ou varrer um departamento inteiro. Ela confirma em lote sem exigir a
sequência, e é de propósito: quem chega por busca já sabe o que está olhando.

```
sem_destino ─┐
             ├─curar / aceitar_sugestao─> livre + pronto=1  (agente pode pegar)
livre p=0   ─┘
             └─descartar────────────────> descartado        (fora do lote)
```

- **`aceitar_sugestao(cods)`** confirma o destino que a planilha sugeriu, para
  os itens marcados na tela. Passa pela validação mesmo assim: aceitar em lote é
  exatamente onde um trio inválido passaria batido. O que não valida volta em
  `recusados`, com o motivo.
- **`aceitar_destino(dep, sec, sub, confianca, so_ativos)`** confirma **todos**
  os pendentes daquele destino, no lote inteiro — não só os da página. É o que a
  tecla `L` chama, depois da sequência, e o que o botão da lista chama. Existe
  porque os grupos são grandes: o maior tem 1.216 itens e nenhuma página cabe
  isso. Os filtros passados são os mesmos da tela, e por isso o número do botão
  bate com o que a ação faz (verificado nas três combinações). O texto de
  confirmação repete os filtros em voz alta: confirmar centenas de produtos não
  pode depender de o curador lembrar que marcou "só ativos" minutos atrás.
- **`descurar(cods)`** desfaz uma confirmação ainda não trabalhada. Recusa o que
  já saiu de `livre` e devolve o estado em `tarde_demais`.
- **`curar(cods, dep, sec, sub)`** grava um destino escolhido à mão. É o caminho
  dos 4.003 REVISAR, que não têm sugestão.
- **`descartar(cods, motivo)`** tira do lote sem editar no ERP; some da vista,
  não da história (`reverter_descarte` traz todos de volta).
- A lista sai **na mesma ordem da fila de execução** (confiança, depois
  destino), de propósito: o lote vem agrupado por destino. Duas contagens por
  grupo, e elas querem dizer coisas diferentes: `iguais_a_seguir` é o que dá para
  marcar **nesta página**; `no_destino` é o tamanho do grupo **no lote inteiro**,
  e é ele que aparece no botão "Confirmar todos os N". Foi assim que o
  "confirmar em série" saiu do papel.
- **A tela começa escondendo os inativos** (`so_ativos`, marcado por padrão).
  Dois terços da fila são produtos inativos (38.462 de 65.874) e o agente nunca
  os pede — a rodada tem "só ativos" ligado. Curá-los é trabalho que não
  desbloqueia nada. Os números do topo contam só os ativos, com o total ao lado.
- **Concorrência entre curadores** é resolvida por não-sobrescrita, não por
  trava: `_aplicar_curadoria` só mexe em item ainda pendente e devolve
  `ja_curados` para o resto. Duas pessoas na mesma lista se atrapalham um pouco;
  nunca se apagam. Os filtros por departamento existem para elas se dividirem.

### A fila

```
livre(pronto=1) ──reservar──> reservado ──> concluido   (terminal)
                                  │      └─> falhou     (pilha à parte, não volta sozinho)
                                  │      └─> simulado   (não contou como feito)
                                  └──prazo vence──> livre
```

O agente pede um bloco (padrão 100, definido no painel); o servidor seleciona e
marca como dele **na mesma transação**. A reserva é um **prazo, não uma trava**:
vale 20 minutos e o agente renova a cada 3 enquanto roda. Se a máquina travar, os
itens voltam sozinhos. A varredura de vencidos roda preguiçosamente na próxima
reserva, então não há timer no servidor.

Se o bloco vier todo de retomada, ele é **completado** com itens novos até a
quantidade pedida. Sem isso, um operador com pendências que não fecham receberia
sempre os mesmos itens e nunca avançaria.

`simulado` existe porque uma rodada em modo simulação não grava nada no ERP:
marcá-la como concluído faria a rodada de verdade pular o produto. Também não
volta sozinha à fila, senão a simulação giraria nos mesmos itens — o painel tem
botão para devolvê-la.

### O controle remoto do agente

O agente não tem tela, então o painel é o console. Mora tudo em colunas de
`operadores`:

- **`comando`** (`rodar` | `pausar` | `parar`) — o que o agente deve fazer.
- **parâmetros da rodada**: `bloco`, `so_ativos`, `simular`, `pular_certos`,
  `limiar` (similaridade mínima entre a descrição na tela do ERP e a da
  planilha).
- **telemetria devolvida**: `agente_estado`, `agente_msg`, `agente_feitos`,
  `agente_atual`, `agente_em`. `listar_operadores` deriva `vivo` (falou há menos
  de 90 s) e troca o estado por `sem contato` quando o agente sumiu — sem isso
  um programa fechado no meio da rodada ficaria "rodando" para sempre na tela.
- **`agente_log`** — o log que antes rolava na janela do programa. Aparado em
  `APARA_LOG` (400) linhas por operador.
- **`config.mapa_erp`** — o mapa dos campos do RADGe, guardado no servidor.
  Instalar uma máquina nova passa a ser colar o token; recalibrar depois de uma
  atualização do ERP é editar um campo no painel em vez de ir de PC em PC.
  Vazio = cada agente usa o `config.json` local (rede de segurança).

O painel atualiza sozinho a cada 4 s (`/painel/api/estado`).

### As duas autenticações

As páginas das pessoas (painel e curadoria) usam a guarda de sessão normal. A
API do agente **não pode** usar sessão de navegador: autentica por **token** no
cabeçalho `X-Token`, gerado no painel. Por isso os endpoints dela começam com
`api_` e o prefixo `reclassificacao.api_` está em `PREFIXOS_PUBLICOS`
(`auth_routes.py`), isentando-os da guarda de sessão — eles têm guarda própria em
`_guarda_token`.

**Nunca ponha em `PREFIXOS_PUBLICOS` uma rota que não cheque credencial
própria.** O prefixo isenta da sessão; não substitui autenticação.

**Corolário que morde:** nenhum endpoint que não seja do agente pode se chamar
`api_*`. Um `api_curar` ficaria gravável sem login. Os da curadoria se chamam
`cur_*`, com `/api/` só no **caminho** (assim um fetch deslogado recebe 401 JSON
em vez de um redirecionamento para a tela de login).

**O nome do operador vem do token, nunca do corpo da requisição.** Se viesse do
corpo, qualquer um se diria outro e fecharia produto alheio — e `concluido` é
terminal, não tem desfazer.

O token aparece **uma vez só**, na volta do *Gerar token*. Gerar de novo
invalida o anterior. Revogar zera o token e o agente passa a receber 401 com
mensagem pedindo outro ao coordenador.

### Contrato da API (consumido pelo agente)

Base: `http://<servidor>/reclassificacao`. Todas exigem `X-Token`, menos `ping`.

| Rota | Corpo / query | Devolve |
|---|---|---|
| `GET /api/ping` | — | `{ok, servidor, agora}` — sem token, serve para testar o endereço |
| `GET /api/config` | — | `{comando, bloco, so_ativos, simular, pular_certos, limiar, lease_s, batimento_s, mapa_erp}` |
| `POST /api/status` | `{estado, msg, feitos, atual, maquina, log[]}` | o **mesmo corpo de `/api/config`** |
| `GET /api/estado` | — | resumo do lote (ver `resumo()`) |
| `GET /api/eventos` | `limite` | lista de eventos recentes |
| `GET /api/item` | `cod` | `{existe, meu, estado, operador}` |
| `GET /api/todos` | — | `{linhas, contagem}` para exportação |
| `POST /api/reservar` | `{quantidade, so_ativos, confiancas, maquina}` | `{itens, expira_em, retomado, novos}` |
| `POST /api/renovar` | — | `{reservados, expira_em}` |
| `POST /api/concluir` | `{cod, situacao, dep, sec, sub, detalhe}` | `{ok, estado}` ou `{ok:false, motivo}` |
| `POST /api/liberar` | `{cods}` (ou nulo = tudo) | `{liberados}` |

`/api/status` responder o mesmo que `/api/config` é de propósito: o agente faz
uma ida por produto para reportar **e** receber ordem nova. Duas chamadas
dobrariam a conversa num laço que roda a cada 3 segundos.

`situacao` aceita `alterado`, `ja_correto`, `pulado` (viram `concluido`),
`simulado` e `erro` (vira `falhou`). Cada item devolvido em `reservar` tem
`cod, linha, produto, ativo, ano, dep_atual, sec_atual, sub_atual, dep_novo,
sec_novo, sub_novo, confianca, base` (mais `estado`, `pronto` e `curado_por`,
que a tela de curadoria usa e o agente ignora).

**Duas mudanças de semântica em `reservar`** (o agente foi reescrito junto):
`confiancas` **vazio agora significa TODAS** — o agente não escolhe mais nada,
quem filtra é o painel; e a seleção passou a exigir **`pronto=1`**.

**Mudar nome de campo, formato ou semântica aqui quebra o agente sem aviso**,
porque ele está noutro repositório e ninguém vai ver o erro até um operador
tentar trabalhar. Se precisar mudar, mude os dois lados na mesma sessão.

### O que não pode ser refatorado

1. **A reserva seleciona e marca na MESMA transação.** É isso, e só isso, que
   impede dois operadores de receberem o mesmo produto. Separar em "buscar
   livres" e depois "marcar" reabre a corrida que a fila existe para fechar.
2. **O lock serializa dentro de UM processo.** O waitress roda processo único
   com threads, então funciona. Se o sistema passar a vários processos worker,
   a atomicidade cai e a fila precisa de outra trava (advisory lock no SQLite,
   ou trocar por Postgres com `SELECT ... FOR UPDATE SKIP LOCKED`).
3. **`concluido` é terminal** e não tem botão de voltar no painel: reabrir um
   cadastro já editado é exatamente o que a fila existe para impedir. Só
   `falhou` e `simulado` voltam à fila.
4. **Só o dono da reserva fecha o item** (`concluir` recusa os demais).
5. **`reservar` só entrega `pronto=1`, e nada nasce `pronto`.** É o que garante
   que a rodada nunca grave um destino que ninguém olhou. Tirar esse filtro — ou
   voltar a liberar alguma faixa de confiança na importação — devolve ao agente
   uma decisão que é da pessoa. Foi pedido explicitamente que não fosse assim.
6. **A validação do trio mora no servidor.** Não confie no `<select>`.
7. **O `.db` fica atrás deste processo, nunca numa pasta de rede.** O travamento
   do SQLite depende de file locks pouco confiáveis sobre SMB e o modo WAL nem
   funciona em rede. Todo mundo fala HTTP com este processo, que é o único
   escritor.

### O que se perdeu junto com a janela (e por quê)

- **Marca.** O campo só existia para o operador digitar no painel de
  confirmação. Sem painel, o agente não encosta nele. Se voltar a ser preciso, o
  lugar é a curadoria — e aí `_aplicar_marca` no `app.py` antigo é a referência.
- **Pausa pedindo socorro na recusa do ERP.** Antes o programa parava e mostrava
  a caixa ("O código do NCM não foi informado…", visto no 7898586613799).
  Agora o agente registra o produto como `erro` com o texto exato, **fecha a
  caixa** e segue — deixá-la aberta travaria o ERP para todos os produtos
  seguintes. Os falhados ficam na pilha à parte e o coordenador devolve à fila
  quando resolver a causa.
- **Escolher confiança na máquina.** Virou consequência do `pronto`.

Continua na máquina, porque não tem como não continuar: `python mapear.py`, o
assistente que descobre onde ficam os campos do RADGe. É de **instalação**, não
de operação, e o mapa que ele gera pode ser colado no painel para valer em todas
as máquinas de uma vez. `reclassificador/app.py` (a janela antiga) segue no
repositório como referência.

### Como testar

O módulo inteiro é testável sem o ERP — e foi. **Use sempre uma cópia do banco**
(o serviço da porta 80 está com o `dados/reclassificacao.db` aberto; dois
processos escrevendo nele é problema). Cópia consistente é pela API de backup do
SQLite, nunca `shutil.copy` — num banco em WAL isso deixa um `-wal` órfão e o
próximo open acusa `database disk image is malformed`:

```python
o = sqlite3.connect("file:dados/reclassificacao.db?mode=ro", uri=True)
d = sqlite3.connect(r"...\teste.db"); o.backup(d)
```

Depois aponte o módulo para ela **antes de importar o app** (o default de
`FilaBanco.__init__` é avaliado no import, então trocar `BANCO` não adianta):

```python
from scripts import reclassificacao as R
R._banco = R.FilaBanco(COPIA)
from app import app
```

Para as páginas, `test_client` com sessão fingida (`s['usuario']='teste'`). Para
o agente, o próprio `Agente` do outro projeto com um driver de mentira:

```python
agente = Agente({"servidor_url": URL, "operador": "Ana", "token": TOKEN})
agente.garantir_erp = lambda: (setattr(agente, "drv", DriverFake()), True)[1]
threading.Thread(target=agente.rodar, daemon=True).start()
```

Verificado assim (29/08/2026, contra os 65.874 itens reais): a migração devolveu
os 38.504 ALTA à curadoria (`prontos=0`, 65.874 esperando, 27.412 deles ativos);
`reservar` devolvendo **zero** itens enquanto ninguém confirmou nada; o número do
botão "Confirmar todos os N" batendo com o que a ação faz nas três combinações de
filtro (ALTA+ativos: 71/71 · todas+ativos: 91/91 · todas+inativos: 185/185); o
agente voltando a receber trabalho assim que um destino é confirmado; o fluxo
da conferência ponta a ponta (3 aprovações seguidas → oferta de lote com 88
restantes → `Z` desfazendo a última, com `pronto` e `curado_em` voltando a nulo →
`L` aprovando os 88 → `Z` recusado com `tarde_demais` depois de o agente ter
reservado o produto); aceite em série de 38 produtos do mesmo destino num clique;
recusa do trio invertido
(`7/22/22` → "subseção 22 (Bovinos) pertence à seção 13"); agente ocioso
aparecendo **vivo** no painel; **Iniciar** no navegador fazendo 565 produtos
rodarem sem nenhuma interação na máquina; **Pausar** interrompendo o bloco e
devolvendo o resto à fila; log do agente chegando no painel; e fila e ERP
batendo (565 fechados = 565 escritos).

Anteriormente verificado e ainda válido: importação com a planilha real (65.876
linhas · 65.874 novos · 2 já existiam · 4.003 sem destino), blocos disjuntos,
recusa de conclusão por quem não é dono, token ausente/revogado devolvendo 401, e
**seis operadores simultâneos: 1.440 entregas, 1.440 produtos distintos, zero
duplicados, a 405 itens/s**.

Depois de testar, **limpe o resíduo**: item deixado em `concluido` por teste faz
a rodada de verdade pular aquele produto para sempre.

### Pendências e ideias

- **Corte por dia ou turno no painel.** Hoje ele mostra só o acumulado do lote;
  não dá para acompanhar ritmo.
- **Auditoria no padrão da seção 6.** Os eventos usam epoch float e tabela
  própria (`eventos`), não a `auditoria` do sistema nem datas ISO. Funciona, mas
  destoa da convenção; vale alinhar se alguém for mexer ali.
- **Sem reatribuição dirigida** (passar o bloco de A para B). Hoje é liberar e
  deixar a fila redistribuir.
- **Curadoria sem reserva.** Dois curadores na mesma faixa se atrapalham (um vê
  "já decidido por outro"). Se virar incômodo, o caminho é um lease curto por
  bloco de curadoria, como o da fila de execução.
- **O volume da curadoria cresceu 4x** ao trazer os ALTA (27.412 ativos, não
  10.562). A ~1 s por produto são ~7,6 h de leitura, divisíveis entre pessoas —
  menos que as 22,8 h que o ERP gasta gravando de qualquer jeito, e em paralelo
  com elas. O gargalo continua sendo a máquina.
- **Sem "voltar" na conferência.** `Z` desfaz o último; não há navegação livre
  para trás. Se incomodar, o caminho é uma pilha de desfazer em vez de um slot.
- **A conferência não guarda onde parou.** Recarregar a página recomeça do topo
  da fila — o que, como a fila encolhe, não repete trabalho, mas perde a posição
  dentro de um destino grande.
- **A fila coordena a equipe, não a loja.** Ninguém enxerga o pessoal do balcão
  editando cadastro pelo ERP durante o expediente; num Delphi CRUD comum o
  último que salva vence, silenciosamente. Mitigação é rodar fora do horário.


## 11. Deploy (Windows + NSSM)

Roda como serviço Windows pelo **NSSM**, executando `server.py` (waitress,
porta 80).

- Serviço: `SistemaFerramentas`
- Application: `C:\Users\CADASTRO\AppData\Local\Programs\Python\Python313\python.exe`
- AppDirectory: `C:\dev\pythonprojects\sistema_ferramentas_refatorado`
- AppParameters: `server.py`

Comandos úteis:
```bat
nssm restart SistemaFerramentas
nssm get SistemaFerramentas Application      :: qual python o serviço usa
nssm get SistemaFerramentas AppParameters    :: qual arquivo executa
```

### Armadilha importante — pip no Python certo
O serviço usa um **python.exe específico**. Se você instalar dependências com um
`pip` qualquer do PATH, pode cair noutro interpretador e o serviço sobe sem o
pacote (e cai com "connection refused" no navegador, porque a porta nem abre).
Sempre instale com o python do serviço:
```bat
"C:\Users\CADASTRO\AppData\Local\Programs\Python\Python313\python.exe" -m pip install -r requirements.txt
```
Para diagnosticar uma queda, rode o app na mão com esse mesmo python e leia o
traceback:
```bat
cd C:\dev\pythonprojects\sistema_ferramentas_refatorado
"C:\...\Python313\python.exe" server.py
```
Opcional: registrar o stderr do serviço num arquivo
(`nssm set SistemaFerramentas AppStderr C:\...\erro.log`) para não perder o
motivo de futuras quedas.

### Firewall
Se outros dispositivos não acessarem, libere a porta no Windows Defender
Firewall (regra de entrada, TCP, porta 80 — ou 5000 em dev).

---

## 12. Como adicionar um módulo novo (receita)

1. `scripts/<modulo>.py` — lógica + persistência (SQLite de preferência), sem Flask.
2. `scripts/<modulo>_routes.py` — `Blueprint("<modulo>", __name__, url_prefix="/<modulo>")`
   com a página e as APIs.
3. Registrar em `app.py`: adicionar `("scripts.<modulo>_routes", "<modulo>_bp")`
   à lista `MODULOS` (registro tolerante — **não** importe o blueprint no topo).
4. `templates/<modulo>/index.html` — estender `base.html`, usar os componentes
   prontos e as CSS variables da paleta.
5. Adicionar o card no `templates/index.html` (no setor certo), com ícone SVG de
   linha e `data-nome` para a busca.
6. Se houver dependência nova, somar ao `requirements.txt`.
7. Seguir as **convenções de confiabilidade** da seção 6 (ISO, soft-delete,
   auditoria, `busy_timeout`) se o módulo gravar dado que importa. Para gravar o
   autor, leia `session.get("usuario")` na camada de rotas e passe adiante.
8. Testar de verdade antes de entregar: subir o app (`python app.py`) e exercer
   as rotas; para a lógica, um teste rápido importando o módulo direto.

---

## 13. Login, backup e módulos de sistema

### Autenticação (`scripts/auth.py` + `auth_routes.py`)
- Usuários em `dados/sistema.db`; senhas com **PBKDF2-HMAC-SHA256** (stdlib, sem
  dependência externa). A `SECRET_KEY` do Flask fica em `dados/secret.key`
  (gerada uma vez; sessões sobrevivem a reinícios).
- A guarda global é instalada por `instalar_guarda(app)` no `app.py`. Endpoints
  públicos: `auth.login`, `auth.logout`, `auth.setup`, `static`. APIs deslogadas
  recebem `401 JSON`; páginas são redirecionadas para `/login`.
- Primeiro acesso: banco de usuários vazio → `/setup` cria o admin.
- Cada mutação de dado sensível grava o autor na auditoria via
  `session.get("usuario")` (ver `debitos_routes._usuario` / `vencidos_routes._usuario`).

### Backup (`scripts/backup.py` + `backup_routes.py`)
- Copia **todos os `.db` de `dados/`** com a **API de backup online do SQLite**
  (consistente com o banco em uso) para `backups/<banco>/<banco>_AAAA-MM-DD_HHMM.db`.
  Retenção padrão: 30 cópias por banco (`BACKUP_RETENCAO`).
- Um **agendador em thread daemon** (`iniciar_agendador`, chamado no `app.py`)
  faz backup ao subir e a cada `BACKUP_INTERVALO_HORAS` (padrão 24h).
- Destino configurável por `BACKUP_DIR` — **aponte para a pasta do Google Drive
  desktop** para que as cópias saiam do PC automaticamente.
- Restauração (`/sistema/backup`) salva o estado atual em `_pre_restauracao/`
  antes de sobrescrever, então também é reversível.

### Controle de vencidos (`scripts/vencidos.py` + `vencidos_routes.py`)
Persiste em `dados/vencidos.db`. Fluxo em **dois estágios + baixa**:
- **`avisos`** — aviso prévio (a seção deve avisar ≥30 dias antes; `DIAS_MINIMO`).
  Campos: produto, código de barras, quantidade, fornecedor, responsável (quem
  avisou), data de vencimento, custo, venda, valor promocional. Status derivado
  (no_prazo / vence_breve ≤30d / vencido / resolvido) + `no_prazo?` (antecedência
  ≥30d = a seção cumpriu a regra). Regra dos 30 dias **sinaliza, não bloqueia**.
- **`vencidos`** — o produto vencido no escritório. Ao registrar, o sistema
  **cruza pelo código de barras** com um aviso ativo (`buscar_aviso_ativo_por_barras`):
  se achar, `foi_avisado=1`, vincula (`aviso_id`) e **resolve** o aviso (sai da
  vigília). Campos de baixa: `baixa_status` (pendente|baixado), `baixa_tipo`
  (`perda`|`devolucao` — `TIPOS_BAIXA`), `baixa_ref`, `baixa_em/por`.
- API: `checar-aviso` alimenta a **checagem ao vivo** no formulário (mostra ✓/✗
  enquanto se digita o código de barras e pré-preenche produto/fornecedor/custo).
  Rotas: `/api/aviso`, `/api/vencido`, `/api/checar-aviso`,
  `/api/vencido/<id>/baixa`, `/reabrir` (+ DELETEs).
- Tela `/vencidos`: painel (valor perdido, % avisado, baixas pendentes, críticos
  ≤7d, vencendo ≤30d) + abas **Vencidos / Avisos / Análise**. Segue as convenções
  (ISO, soft-delete, auditoria). A versão antiga (registro simples com `motivo`)
  foi substituída; o `vencidos.db` legado (vazio) é recriado no boot.
- **Urgência do aviso em faixas** (`_enriquecer_aviso`): vencido / crítico ≤7d /
  atenção 8–30d / programado 31–90d / antecipado >90d (`URGENCIA_LABEL`).
- **Editar aviso** (`editar_aviso` + `/api/aviso/<id>/editar`): só avisos não
  resolvidos; preserva `criado_em` (antecedência original). No cadastro, o campo
  código de barras alerta duplicidade (reusa `/api/checar-aviso`).
- **Risco de sobra** (`_riscos_para`): cruza avisos ativos com o `vendas.db` do
  módulo de relatórios (import protegido, consulta em LOTE, read-only) e estima
  `sobra = qtd − média_mensal × dias/30`. Códigos no vendas.db podem ter zeros à
  esquerda — casa por `{cb, cb.zfill(14)}`. Falha do módulo de vendas não quebra.
- **Vínculo com quantidades**: `listar_vencidos` faz LEFT JOIN no aviso e expõe
  `vinculo` (avisadas × perdidas × aproveitadas) quando o vencido casou com aviso.
- **Ordenação das listas** (`listar_vencidos(ordem=...)` / `listar_avisos(ordem=...)`
  + `ORDENS_VENCIDOS` / `ORDENS_AVISOS`): o 1º nível é fixo — nos vencidos as
  **baixas pendentes ficam SEMPRE no topo** e nos avisos os **resolvidos vão
  SEMPRE para o fim**; o desempate dentro de cada grupo é escolhido nos
  seletores da tela (server-side, params independentes `?ordem=` p/ vencidos e
  `?ordem_avisos=` p/ avisos — o JS preserva um ao trocar o outro e usa
  `#aba-avisos` para reabrir a aba certa). Critérios: vencidos → `modificacao`
  (padrão), `data`, `valor`; avisos → `vencimento` (padrão, urgência),
  `modificacao`, `data`, `valor` (qtd × custo). A coluna `atualizado_em` existe
  nas DUAS tabelas (migração; backfill = `COALESCE(baixa_em|resolvido_em,
  criado_em)`) e é gravada em toda mutação — inclusive resolver/reabrir aviso
  via vencido. `_enriquecer_*` expõe `atualizado_fmt` e `editado` (só quando a
  última alteração ≠ registro e ≠ baixa/resolução — mostra "editado em" na
  sub-linha).
- **Relatório** (PDF de apresentação + Excel de trabalho): ver seção 16.
  `vencidos.py` só produz os dados — quem formata são os módulos de relatório.
- **Análise** (janela 6 meses): `ranking_reincidencia` (2+ ocorrências),
  `ranking_fornecedores` (perda por custo), `ranking_responsaveis` (antecedência
  média e % no prazo por responsável de seção).

---

## 14. Cadastro central de fornecedores (`/fornecedores`)

`scripts/fornecedores.py` + `fornecedores_routes.py` + `dados/fornecedores.db`.
É a **fonte da verdade** da entidade que débitos chama de "empresa" e vencidos
chama de "fornecedor". Não tem página própria — só APIs que alimentam o
**seletor buscar-ou-cadastrar** (`fornecedorPicker` no `base.html`, CSS `.fpick`).

- `fornecedores(id PK, cnpj UNIQUE (pode ser NULL), nome, ...)` + auditoria,
  soft-delete. CNPJ armazenado **como digitado** (compatível com as chaves de
  débitos); duplicidade comparada **só pelos dígitos** (`_cnpj_digitos`).
- **Fornecedor sem CNPJ pode existir** (criado pelo vencidos só com nome). O
  CNPJ vira obrigatório só para entrar em débitos.
- **Invariante:** fornecedor com CNPJ ⇔ linha em `empresas` no debitos.db (as
  FKs de débitos apontam para `empresas`, que continua local; a sincronização é
  feita pela camada de rotas — `_garantir_empresa` em `fornecedores_routes.py`
  chama `debitos.adicionar_empresa` ao criar/definir CNPJ). Excluir empresa em
  débitos **não** exclui o fornecedor do cadastro central.
- **Semeadura** (uma vez por processo, idempotente, tolerante a falha): importa
  as `empresas` do debitos.db e os nomes distintos de fornecedor do vencidos.db
  (leituras read-only). Compara com TODAS as linhas (inclusive excluídas) para
  não ressuscitar fornecedor removido de propósito.
- **Vencidos**: `avisos`/`vencidos` ganharam `fornecedor_id` (migração +
  backfill por nome normalizado, uma vez por processo). O nome (`fornecedor`)
  segue **denormalizado** para exibição/rankings; o vencido sem seleção
  explícita **herda** o `fornecedor_id` do aviso casado por código de barras.
- APIs: `GET /api/buscar?q=` (nome ou dígitos de CNPJ) ·
  `POST /api/criar {nome, cnpj?}` · `POST /api/<id>/cnpj` · `POST /api/<id>/nome`
  · `POST /api/<id>/editar {nome, cnpj}` (troca de razão social e/ou CNPJ).
  Respostas `{"ok", "msg", "fornecedor"}` — em duplicata, `fornecedor` traz o
  existente (o picker seleciona ele).
- **Edição (razão social / CNPJ)**: botão de lápis na linha da empresa em
  `/debitos` abre o modal que chama `/api/<id>/editar`. A sincronização
  (`_sincronizar_empresa`/`_sincronizar_vencidos` em `fornecedores_routes.py`):
  CNPJ novo → `debitos.editar_empresa` **migra a chave** (empresas + debitos +
  pagamentos, com `PRAGMA foreign_keys=OFF` pontual, pois a FK não tem ON
  UPDATE CASCADE); nome novo → renomeia a empresa e propaga o nome
  denormalizado nos avisos/vencidos vinculados (`vencidos.renomear_fornecedor`).
  Duplicidade de CNPJ barrada pelos dígitos; reformatar o mesmo CNPJ é ok.
- A lista de empresas em `/debitos` mostra **todas as com CNPJ** (=`empresas`
  ativas); o modal "+ Nova empresa" usa o seletor (fornecedor existente sem
  CNPJ → pede o CNPJ; inexistente → cria com nome+CNPJ).

---

## 15. Módulo Fiscal — custo real por NF-e (`/fiscal`)

Importa XMLs de NF-e de **compra**, calcula o **custo líquido real** de cada item
(créditos de ICMS e PIS/COFINS) e sugere preço de venda, alimentando um cadastro
de produtos interno. Supermercado em Lucro Real. Persiste em `dados/fiscal.db`.

### Arquitetura — 3 camadas independentes (NÃO misturar)
```
Ingestão -> Parser -> De-para -> Motor de custo -> Motor de preço -> Fila
```
- `scripts/fiscal.py` — **modelos + schema** (sem Flask). 8 tabelas do spec +
  `produtos` (cadastro interno, criado aqui — o sistema não tinha catálogo).
  `_init_schema`/`_migrar` idempotentes. **Dinheiro e quantidades em `Decimal`,
  armazenados como TEXT** (`D`/`dec_txt`) — nunca float. IDs inteiros
  autoincrementais (desenho relacional).
- `scripts/fiscal_ingestao.py` — interface **`FornecedorDeXml`** (método único
  `obter_novos()` → `XmlBruto`) com `UploadManual` (arquivo/`.zip`) e
  `ApiTerceiro` (REST configurável, só stdlib `urllib`). Ponto de extensão
  documentado `SefazDistribuicaoDFe` (NÃO implementado). Trocar origem não muda
  nenhuma outra camada.
- `scripts/fiscal_parser.py` — **parser NF-e 4.00** (ns portalfiscal). Regras:
  crédito de ICMS **lido do destacado** (vICMS; vCredICMSSN p/ Simples; CST
  40/41/50/51/60 → zero); **rateio** de frete/seguro/outros do total proporcional
  ao `vProd` com centavos exatos (`ratear`); **fator de conversão** derivado
  (qTrib/qCom) + alerta `fator_conversao_suspeito` (uCom==uTrib e embalagem);
  alerta `frete_fob_ausente` (modFrete=1 e frete total 0). Puro, em Decimal.
- `scripts/fiscal_motor.py` — **motores puros** de custo e preço + guarda-corpos.
  Custo: `custo_bruto`, `base_pis` excluindo o ICMS destacado (Lei 14.592/2023),
  crédito PIS/COFINS 0,0925 (**zero se NCM monofásico**), `custo_liquido`,
  `custo_unitario`. Flag `INCLUIR_ICMS_ST_NA_BASE_PIS` (padrão **desligado**).
  Preço: `preco = custo_unit / (1 - (impostos_saida + despesas + margem))`; ICMS
  saída 0 se CST 60/ST retida; PIS/COFINS saída 0 se monofásico; **arredonda
  sempre p/ cima até ,49/,99** (`arredondar_para_cima`).
- `scripts/fiscal_importacao.py` — **orquestrador** (única camada que conhece as
  outras). De-para (`resolver_produto`: vínculo → EAN cria vínculo → fila com
  `produto_nao_vinculado`); precedência do fator confirmado; pipeline completo;
  **conferência** (`vincular_item`, `confirmar_fator`, `preencher_ncm` — recalcula
  na hora, em lote por NCM); **`aprovar_preco`** é a ÚNICA porta que publica preço
  no produto (grava `log_preco`, respeita guarda-corpos).
- `scripts/fiscal_routes.py` + `templates/fiscal/index.html` — Blueprint `/fiscal`
  com 4 sub-abas (Importação, Conferência agrupada por alerta, Detalhe da nota com
  composição de custo + CSV, Tabelas: NCM com import CSV + parâmetros). Aba também
  acessível pelo seletor no topo da calculadora existente.

### Regras que moram em DADOS (nunca hardcoded)
`ncm_fiscal` (alíquota interna, monofásico, ST — lookup do mais específico ao
genérico: 8→6→4→2 dígitos) e `parametro_precificacao` (global/seção/subgrupo;
resolvedor completo mas **só global** usado por ora). **Degradação graciosa**:
NCM ausente assume 19%, não monofásico, sem ST, e sinaliza `ncm_ausente` — o
módulo funciona com as tabelas vazias no 1º dia.

### Guarda-corpos (antes de publicar preço)
Preço < custo líquido → **bloqueia** (salvo `aprovar_abaixo_do_custo`); variação
> +15% ou < -10% vs preço atual → **exige aprovação**; item sem vínculo ou fator
não confirmado → **não precifica**; margem fora da faixa do subgrupo → só
alertaria (sem faixa por subgrupo cadastrada). Idempotência por `chave_acesso`
(reimportar atualiza, nunca duplica).


---

## 16. Relatórios em PDF (apresentação) e Excel (trabalho)

Dois relatórios seguem o mesmo desenho, sobre uma base comum:

| | Vencidos | Débitos e bonificações |
|---|---|---|
| **dados** (fonte única) | `vencidos_relatorio.montar_relatorio` | `debitos_relatorio.montar_relatorio` |
| **PDF** (apresentação) | `vencidos_pdf.py` · `GET /vencidos/pdf` | `debitos_pdf.py` · `GET /debitos/relatorio/pdf` |
| **Excel** (trabalho) | `gerar_excel_vencidos` · `GET /vencidos/excel` | `gerar_excel_relatorio` · `GET /debitos/relatorio/excel` |
| **base visual** | `scripts/relatorio_pdf.py` | `scripts/relatorio_pdf.py` |

**A regra que organiza tudo:** `montar_relatorio` é a fonte única e os
renderizadores são burros — **nenhuma regra de negócio mora num renderizador**.
PDF apresenta, Excel analisa; cada formato faz bem uma coisa. Ambos geram **em
memória** (BytesIO) — no Windows um temporário aberto pelo `send_file` não pode
ser apagado depois.

**Por que PDF e não Excel para apresentar:** o Excel muda de cara conforme a
versão, o zoom e o aparelho de quem abre (e perde a formatação no celular ou no
Google Sheets). O PDF é fixo. Os dois relatórios já tiveram um Excel formatado
para impressão (o de débitos chegou a caber numa página A4 medida a régua) e os
dois perderam isso de propósito: **planilha que vira documento perde o
autofiltro** (mesclagem e sub-linha quebram o filtro) **e não ganha a fidelidade
de um PDF**. Se algum dia precisar daquela página A4 de novo, ela está no git.

### `scripts/relatorio_pdf.py` — a base
Paleta do app, registro de fontes, estilos, formatação pt-BR, `esc()`,
`BarraEmpilhada`, `cartoes_kpi`, `alertas`, `tabela_ranking`, `barra_secao`,
`estilo_tabela`, `linha_detalhe`, `linha_total`, `fabrica_canvas` e `construir`.
- **Mexer aqui mexe em TODOS os relatórios** — é o preço de terem a mesma cara.
  Mudança nesta base pede reteste de todos.
- **`reportlab` importado de forma PROTEGIDA**: sem ele, `DISPONIVEL=False`, as
  rotas de PDF respondem **503 com instrução** e nada mais sai do ar. Instale com
  o python do serviço (armadilha da seção 11) e reinicie o serviço.
- **Fontes**: registra a **Segoe UI** de `%WINDIR%\Fonts` (a do app) e cai para
  Helvetica se não achar. O texto do PDF **usa acentos normalmente** — a regra
  "só ASCII" da seção 8 vale para `print()` no stdout cp1252, não para documento.
- **Grade em MILÍMETROS** somando 182 (A4 retrato, margem de 14 mm). Coluna que
  estoura empurra a tabela para fora da folha.
- `linha_detalhe` amarra o par item+sub-linha com **`NOSPLIT`** para o detalhe
  não ficar órfão na virada de página. **Cuidado**: um bloco NOSPLIT mais alto
  que a página não tem como ser quebrado — quando o item pode ter MUITAS
  sub-linhas (os pagamentos de um débito), amarre só a primeira (`nosplit=False`
  nas demais), que é o que o `debitos_pdf` faz.
- `fabrica_canvas` é um canvas de **duas passadas**: o total de páginas do
  "Página X de Y" só se sabe no fim.

### Estrutura dos dois documentos
**Página 1 é uma CAPA EXECUTIVA** — é o motivo de o PDF existir. Quatro KPIs,
uma barra empilhada, duas caixas de alerta e dois blocos de ranking. Depois, as
partes como detalhamento.

- **Vencidos** — KPIs: valor total, perda efetiva, devolvido, % avisado. Barra:
  devolvido / perda / baixa pendente. Alertas: baixas paradas e valor em risco no
  mês seguinte. Blocos: fornecedores com maior perda e produtos reincidentes
  (janela **fixa de 6 meses**, não o mês do relatório — o rótulo diz isso).
- **Débitos** — KPIs: débito total, total abatido, saldo em aberto, dívidas em
  aberto. Barra: abatido / em aberto do mês / em aberto do mês anterior — a
  terceira fatia é o **atraso**, porque as notas do mês anterior já deveriam ter
  sido pagas. Alertas: saldo atrasado e crédito livre parado. Blocos: maiores
  saldos por dívida (só quando `mostrar_quebra`) e **como a dívida está sendo
  abatida** (`rel["por_tipo"]`). A capa traz a **regra de competência escrita** —
  é a coisa mais fácil de o leitor entender errado.
- **Alerta com valor ZERO vira caixa verde**, com o texto da boa notícia. Uma
  caixa vermelha escrita "R$ 0,00" assusta à toa e ensina o leitor a ignorá-la.
- **Parte vazia não gasta uma folha**: só parte com item abre página nova (a
  parte 1 sempre abre, para não colar na capa). Um mês de vencidos sem nada sai
  em 2 páginas em vez de 6.

### As partes
- **Vencidos, 5 partes**: geral · com troca (`baixa_tipo=devolucao`) · sem troca
  (`perda`) · sem troca e sem aviso (`perda` + `foi_avisado=0`) · avisos de
  vencimento (mês do relatório **e o seguinte**, só o que vence **depois** da
  geração, com venda/mês, sobra estimada e risco). As partes 2–4 são recortes da
  1; quem está com baixa **pendente** não tem tipo e só aparece na parte 1.
  Produto sem venda registrada fica **sem estimativa** ("—", nunca zero).
- **Débitos, 3 partes**: débitos do mês · débitos do mês anterior · consolidado,
  mais o crédito não aplicado. Cada débito leva uma sub-linha de contexto e uma
  sub-linha por pagamento aplicado (o acompanhamento NF a NF).

### Os Excel de trabalho
Tabela **plana**: uma linha por registro, **autofiltro**, painel congelado em
`A3`, linha de TOTAL. Nome de aba ≤31 caracteres (limite do Excel).
- **Vencidos** — 5 abas, uma por parte; a de avisos tem colunas próprias (prazo,
  venda/mês, sobra, risco, antecedência).
- **Débitos** — 4 abas: *Débitos* (as duas partes numa tabela só, com a coluna
  "Mês de referência" para separar no filtro), *Pagamentos aplicados* (uma linha
  por **alocação** — a unidade real, porque um pagamento pode se repartir entre
  débitos de meses diferentes; vem de `alocacoes_planas`), *Resumo por dívida* e
  *Crédito não aplicado*.
