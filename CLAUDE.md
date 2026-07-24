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
│   └── relatorios_routes.py   # Blueprint /relatorios
│
├── templates/             # CAMADA DE VIEW (Jinja2, todas estendem base.html)
│   ├── base.html              # layout + DESIGN SYSTEM (paleta, responsivo) — seção 8
│   ├── index.html             # menu principal (grade de ferramentas + busca)
│   ├── cadastro/              # calculadora, placas_ofertas, placas_hortifruti, registro_perda
│   ├── loja/                  # lote_vencimento
│   ├── debitos/               # debitos_index, debitos_empresa
│   ├── layouts/               # index, cadastrar, gerar
│   └── relatorios/            # index (processar PDFs + consultar por código de barras)
│
├── assets/                # estáticos servidos em /assets/<arquivo>
│   ├── *.ttf                  # fontes das placas (Anton, ChelseaMarket, impact)
│   ├── *.png/.jpeg            # modelos de placa
│   └── layouts/*.json         # DEFINIÇÕES DE LAYOUT das placas (dados persistidos)
│
├── dados/                 # dados de runtime
│   ├── debitos.xlsx           # base de débitos/bonificações
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

### Relatórios de venda — Blueprint `/relatorios` (ver seção 7, é o mais novo)

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

### Rotas
`GET /relatorios/` · `GET /relatorios/api/status` ·
`POST /relatorios/api/upload` (envia PDFs p/ a fila) ·
`POST /relatorios/api/processar` (processa a fila) ·
`POST /relatorios/api/consultar` (JSON `{codigos}`) ·
`POST /relatorios/api/consultar/excel` (baixa Excel pivotado).

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
