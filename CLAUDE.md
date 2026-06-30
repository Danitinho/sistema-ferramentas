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
  relatórios de venda, registro de perda.
- **Loja:** lote e vencimento.
- **Financeiro:** débitos e bonificações.

Algumas ainda são *placeholders* (ver seção 10).

---

## 2. Stack e como rodar

- **Python 3.13**, Flask 3.
- **Dois entrypoints:**
  - `app.py` — desenvolvimento. Sobe o servidor de dev do Flask na **porta
    5000** (`python app.py`). Mostra o IP local no terminal.
  - `server.py` — produção. Usa **waitress** na **porta 80**
    (`python server.py`). É este que roda como serviço Windows via NSSM.
- **Persistência:** sem banco central único. Cada módulo guarda do seu jeito
  (ver seção 6). O módulo de relatórios usa **SQLite**; os demais usam Excel ou
  JSON.

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
│   ├── debitos.py             # lógica + persistência (Excel) de débitos
│   ├── debitos_routes.py      # Blueprint /debitos
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

Blueprints registrados em `app.py`:
```python
app.register_blueprint(debitos_bp)     # /debitos
app.register_blueprint(layouts_bp)     # /layouts
app.register_blueprint(relatorios_bp)  # /relatorios
```
As rotas de `cadastro/*` e `loja/*` ainda estão **diretas no `app.py`** (não
foram extraídas para blueprints). Ao evoluí-las, considere movê-las para
blueprints próprios seguindo o padrão acima.

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

### Débitos e bonificações — Blueprint `/debitos` (`debitos_routes.py` + `debitos.py`)
Controle por empresa (CNPJ) de débitos de vencimento, rebaixas e bonificações.
Persistido em `dados/debitos.xlsx`. API JSON em `/debitos/api/...`.
> **Atenção:** a rota é `POST /debitos/api/debito/rebaxa` (grafia "rebaxa", sem
> "i"). É um typo que o frontend já consome — **não "corrija" sem atualizar o
> JS correspondente**, senão quebra.

### Relatórios de venda — Blueprint `/relatorios` (ver seção 7, é o mais novo)

---

## 6. Persistência (mista — atenção)

Não há um banco único. Cada módulo persiste de um jeito:

| Módulo            | Onde                          | Formato |
|-------------------|-------------------------------|---------|
| Débitos           | `dados/debitos.xlsx`          | Excel (openpyxl) |
| Layouts de placa  | `assets/layouts/*.json`       | JSON |
| Relatórios venda  | `dados/relatorios/vendas.db`  | SQLite |
| Uploads temporários | `uploads/`                  | arquivos soltos |
| Saídas geradas    | `outputs/`                    | PDF/imagens |

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

### Paleta atual — "Grafite & Âmbar"
Definida em CSS variables no `:root` de `base.html`:
- `--graphite #26262B` (primária: header, botões), `--graphite-dark #15151A`
- `--amber #E5A23B` (acento: foco de campo, hover, badges), `--amber-dark #8A5B12`, `--amber-light #FBEFD9`
- Aliases mantidos por compatibilidade: `--brand` (= grafite), `--brand-light`,
  `--brand-dark`, `--accent` (= âmbar). **Use sempre as variáveis, nunca hex
  cravado** — assim trocar a paleta continua sendo um ponto único.
- Semânticas: `--success`, `--warning`, `--danger` (+ versões `-bg`).
- Neutros: `--gray`, `--gray-light`, `--ink`, `--border`.

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
`brl(valor)` (moeda BRL) e o drag-and-drop automático de `.upload-area`.

### Outras regras de UI
- **Ícones:** SVG **inline** (não emoji, não CDN). Decisão intencional: os
  aparelhos da loja podem não ter internet, então nada de depender de fontes de
  ícone externas. Ao adicionar um card no menu, copie o padrão de `<svg>` de
  linha já usado em `index.html`.
- A busca do menu filtra os cards no cliente via `data-nome` em cada `.tool-card`.
- Componentes prontos no `base.html`: `.card`, `.btn` (`.btn-primary`,
  `.btn-secondary`, `.btn-accent`), `.metric`, `.result-box`, `.upload-area`,
  `.badge`, `.spinner`. Reutilize antes de criar novos.

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
  `registro-perda/salvar`, `lote-vencimento/consultar`. As telas existem; falta
  a lógica.
- `cadastro/*` e `loja/*` ainda são rotas diretas no `app.py` — candidatas a
  virar blueprints.
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
3. Registrar em `app.py`: importar `<modulo>_bp` e `app.register_blueprint(<modulo>_bp)`.
4. `templates/<modulo>/index.html` — estender `base.html`, usar os componentes
   prontos e as CSS variables da paleta.
5. Adicionar o card no `templates/index.html` (no setor certo), com ícone SVG de
   linha e `data-nome` para a busca.
6. Se houver dependência nova, somar ao `requirements.txt`.
7. Testar de verdade antes de entregar: subir o app (`python app.py`) e exercer
   as rotas; para a lógica, um teste rápido importando o módulo direto.
