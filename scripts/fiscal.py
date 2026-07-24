"""
scripts/fiscal.py
Módulo fiscal — importação de NF-e de compra, custo líquido e preço sugerido.

ETAPA 1 de 5: migrations (schema idempotente) + modelos (acesso a dados).
Aqui NÃO há Flask, parser, motor de custo/preço nem UI — só o schema e as
funções de persistência que as camadas seguintes vão consumir.

Arquitetura (3 camadas, sem misturar responsabilidades):
    Ingestão -> Parser -> De-para -> Motor de custo -> Motor de preço -> Fila
Este arquivo é a base de dados de todas elas. Persistência em SQLite próprio
(`dados/fiscal.db`), no mesmo padrão dos outros módulos (backup automático pega
qualquer .db novo em dados/).

DECISÕES DE PROJETO (confirmadas / derivadas das convenções):
  • "Migrations" neste stack = schema idempotente em `_init_schema`/`_migrar`
    (não há ORM nem framework de migration — ver debitos.py/vencidos.py).
  • DINHEIRO em `Decimal`, NUNCA float. O SQLite não tem tipo decimal, então
    todo valor monetário e toda QUANTIDADE (qtd, fator de conversão) é
    armazenado como TEXT canônico de Decimal (helpers `dec_txt`/`D`). Some/ordene
    em Python, não confie em aritmética SQL para dinheiro.
  • Cadastro interno de produtos criado aqui (`produtos`) — é o alvo do de-para
    e do preço sugerido. O sistema não tinha catálogo de produtos até então.
  • Parâmetros de precificação começam SÓ globais; o resolvedor
    (subgrupo → seção → global) já está completo e passa a valer quando houver
    produtos classificados por seção/subgrupo.
  • Autoria em TEXT (`usuario` da sessão), como a auditoria dos outros módulos —
    o sistema não tem id numérico de usuário. (O spec chama de `usuario_id`.)
"""
import os
import json
import sqlite3
from datetime import datetime
from decimal import Decimal, InvalidOperation

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DADOS_DIR = os.path.join(BASE_DIR, "dados")
BANCO     = os.path.join(DADOS_DIR, "fiscal.db")

# NCM sem regra fiscal cadastrada: alíquota interna padrão (degradação graciosa —
# o módulo funciona com ncm_fiscal vazia no 1º dia; o motor sinaliza e as tabelas
# vão sendo preenchidas). Aqui é só o default de lookup; o alerta é do motor.
ALIQUOTA_INTERNA_PADRAO = Decimal("0.19")

STATUS_NOTA = ("pendente", "conferida", "descartada")
ORIGENS_INGESTAO = ("upload_manual", "api_terceiro", "sefaz_dfe")


# ── Conexão e dinheiro ────────────────────────────────────────────────────────
def _conn():
    os.makedirs(DADOS_DIR, exist_ok=True)
    conn = sqlite3.connect(BANCO)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    _init_schema(conn)
    return conn


def D(v, default="0"):
    """Converte com segurança para Decimal (nunca via float). Aceita Decimal,
    str, int, ou os TEXT lidos do banco."""
    if v is None or v == "":
        return Decimal(default)
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v).replace(",", "."))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def dec_txt(v):
    """Decimal/num -> TEXT canônico para armazenar. None permanece None."""
    if v is None:
        return None
    return str(D(v))


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ── Schema ────────────────────────────────────────────────────────────────────
def _init_schema(conn):
    conn.executescript("""
        -- Cadastro interno de produtos (alvo do de-para e do preço sugerido).
        CREATE TABLE IF NOT EXISTS produtos (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            codigo_interno       TEXT,          -- código do PDV/RADInfo (opcional)
            ean                  TEXT,           -- código de barras
            descricao            TEXT NOT NULL,
            secao                TEXT,
            subgrupo             TEXT,
            custo_unitario_atual TEXT,           -- Decimal (último custo líquido/un)
            preco_atual          TEXT,           -- Decimal (preço vigente no cadastro)
            criado_em            TEXT NOT NULL,
            criado_por           TEXT,
            atualizado_em        TEXT,
            excluido_em          TEXT,
            excluido_por         TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_prod_ean    ON produtos(ean);
        CREATE INDEX IF NOT EXISTS idx_prod_codigo ON produtos(codigo_interno);

        -- Nota fiscal de compra (cabeçalho). chave_acesso é a identidade (idempotência).
        CREATE TABLE IF NOT EXISTS nota_fiscal (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            chave_acesso      TEXT NOT NULL UNIQUE,   -- 44 dígitos
            numero            TEXT,
            serie             TEXT,
            cnpj_emitente     TEXT,
            nome_emitente     TEXT,
            uf_origem         TEXT,
            crt_emitente      TEXT,           -- 1=Simples, 2=Simples excesso, 3=Regime normal
            data_emissao      TEXT,
            modalidade_frete  TEXT,           -- modFrete: 0 CIF, 1 FOB, 2, 3, 4, 9
            valor_total       TEXT,           -- Decimal
            valor_frete_total TEXT,           -- Decimal
            xml_bruto         TEXT,
            origem_ingestao   TEXT,           -- upload_manual | api_terceiro | sefaz_dfe
            status            TEXT NOT NULL DEFAULT 'pendente',  -- pendente|conferida|descartada
            importado_em      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_nf_status ON nota_fiscal(status);
        CREATE INDEX IF NOT EXISTS idx_nf_cnpj   ON nota_fiscal(cnpj_emitente);

        -- Item da nota. Todas as parcelas de custo ficam visíveis (o usuário
        -- precisa ver a composição). Money/qtd em TEXT (Decimal).
        CREATE TABLE IF NOT EXISTS nota_fiscal_item (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            nota_fiscal_id       INTEGER NOT NULL,
            numero_item          INTEGER,
            codigo_fornecedor    TEXT,           -- cProd (código do produto no fornecedor)
            ean                  TEXT,
            descricao_xml        TEXT,
            ncm                  TEXT,
            cest                 TEXT,
            cfop                 TEXT,
            cst_csosn            TEXT,
            origem_mercadoria    TEXT,           -- orig do ICMS (0..8)
            quantidade_comercial TEXT,           -- Decimal
            unidade_comercial    TEXT,
            quantidade_tributavel TEXT,          -- Decimal
            unidade_tributavel   TEXT,
            fator_conversao      TEXT,           -- Decimal (qTrib/qCom ou confirmado)
            valor_produto        TEXT,           -- Decimal
            valor_desconto       TEXT,
            valor_ipi            TEXT,
            valor_frete          TEXT,
            valor_seguro         TEXT,
            valor_outros         TEXT,
            valor_icms_st        TEXT,
            valor_fcp_st         TEXT,
            credito_icms         TEXT,
            credito_pis_cofins   TEXT,
            custo_liquido        TEXT,
            custo_unitario       TEXT,
            preco_sugerido       TEXT,
            produto_id           INTEGER,        -- nullable até o de-para resolver
            alertas              TEXT,           -- JSON: lista de códigos/descrições
            FOREIGN KEY (nota_fiscal_id) REFERENCES nota_fiscal(id) ON DELETE CASCADE,
            FOREIGN KEY (produto_id)     REFERENCES produtos(id)
        );
        CREATE INDEX IF NOT EXISTS idx_nfi_nota    ON nota_fiscal_item(nota_fiscal_id);
        CREATE INDEX IF NOT EXISTS idx_nfi_produto ON nota_fiscal_item(produto_id);
        CREATE INDEX IF NOT EXISTS idx_nfi_ean     ON nota_fiscal_item(ean);

        -- De-para: código do fornecedor -> produto interno. A peça central; uma
        -- vez confirmado, toda nota daquele fornecedor resolve sozinha.
        CREATE TABLE IF NOT EXISTS produto_fornecedor (
            id                       INTEGER PRIMARY KEY AUTOINCREMENT,
            produto_id               INTEGER NOT NULL,
            cnpj_fornecedor          TEXT NOT NULL,
            codigo_fornecedor        TEXT NOT NULL,
            ean                      TEXT,
            fator_conversao_confirmado TEXT,     -- Decimal; precede o derivado do XML
            confirmado_em            TEXT,
            confirmado_por           TEXT,
            FOREIGN KEY (produto_id) REFERENCES produtos(id) ON DELETE CASCADE
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pf_forn_codigo
            ON produto_fornecedor(cnpj_fornecedor, codigo_fornecedor);

        -- Regras fiscais por NCM (editáveis). PK aceita 2/4/6/8 dígitos; o
        -- lookup resolve do mais específico para o mais genérico.
        CREATE TABLE IF NOT EXISTS ncm_fiscal (
            ncm                   TEXT PRIMARY KEY,
            aliquota_interna      TEXT,           -- Decimal (ex.: 0.19)
            monofasico_pis_cofins INTEGER NOT NULL DEFAULT 0,
            sujeito_st_destino    INTEGER NOT NULL DEFAULT 0,
            observacao            TEXT,
            atualizado_em         TEXT
        );

        -- Parâmetros de precificação por escopo. Resolução do mais específico
        -- ao mais genérico: subgrupo -> secao -> global (só global por ora).
        CREATE TABLE IF NOT EXISTS parametro_precificacao (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            escopo       TEXT NOT NULL,           -- global | secao | subgrupo
            escopo_id    TEXT,                    -- nome da seção/subgrupo (NULL p/ global)
            pis_cofins   TEXT,                    -- Decimal (PIS/COFINS de saída)
            taxa_cartao  TEXT,                    -- Decimal
            quebra       TEXT,                    -- Decimal (quebra da seção)
            margem_alvo  TEXT,                    -- Decimal
            vigente_desde TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_param_escopo
            ON parametro_precificacao(escopo, escopo_id);

        -- Histórico de toda alteração de preço (auditoria de precificação).
        CREATE TABLE IF NOT EXISTS log_preco (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            produto_id     INTEGER,
            preco_anterior TEXT,           -- Decimal
            preco_novo     TEXT,           -- Decimal
            custo_na_epoca TEXT,           -- Decimal
            motivo         TEXT,
            usuario        TEXT,           -- autor (spec: usuario_id; aqui é o TEXT da sessão)
            criado_em      TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_logpreco_prod ON log_preco(produto_id);

        -- Auditoria geral do módulo (mutações de nota, vínculo, NCM, parâmetros).
        CREATE TABLE IF NOT EXISTS auditoria (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            quando      TEXT NOT NULL,
            usuario     TEXT,
            entidade    TEXT NOT NULL,
            entidade_id TEXT,
            acao        TEXT NOT NULL,
            detalhe     TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fiscal_audit ON auditoria(entidade, entidade_id);
    """)
    conn.commit()
    _migrar(conn)


def _migrar(conn):
    """Evolui bancos de versões anteriores sem destruir dado. Vazio por
    enquanto (schema inicial); ponto de extensão para ALTERs idempotentes
    futuros, no padrão de debitos._migrar."""
    conn.commit()


def _auditar(conn, entidade, entidade_id, acao, detalhe="", usuario=None):
    conn.execute(
        "INSERT INTO auditoria (quando, usuario, entidade, entidade_id, acao, detalhe) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_agora(), usuario, entidade, str(entidade_id) if entidade_id is not None else None,
         acao, detalhe))


# ── Produtos ──────────────────────────────────────────────────────────────────
def criar_produto(descricao, codigo_interno=None, ean=None, secao=None,
                  subgrupo=None, usuario=None):
    descricao = (descricao or "").strip()
    if not descricao:
        return False, "Informe a descrição do produto.", None
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO produtos (codigo_interno, ean, descricao, secao, subgrupo, "
            "criado_em, criado_por) VALUES (?,?,?,?,?,?,?)",
            ((codigo_interno or "").strip() or None, (ean or "").strip() or None,
             descricao, (secao or "").strip() or None, (subgrupo or "").strip() or None,
             _agora(), usuario))
        pid = cur.lastrowid
        _auditar(conn, "produto", pid, "criar", descricao, usuario)
        conn.commit()
        return True, "Produto cadastrado.", pid
    finally:
        conn.close()


def buscar_produto(produto_id):
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM produtos WHERE id=? AND excluido_em IS NULL",
                         (produto_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def buscar_produto_por_ean(ean):
    ean = (ean or "").strip()
    if not ean:
        return None
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM produtos WHERE ean=? AND excluido_em IS NULL "
                         "ORDER BY id LIMIT 1", (ean,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def buscar_produto_por_codigo(codigo_interno):
    codigo = (codigo_interno or "").strip()
    if not codigo:
        return None
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM produtos WHERE codigo_interno=? AND excluido_em IS NULL "
                         "ORDER BY id LIMIT 1", (codigo,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def listar_produtos(busca=None, limite=200):
    conn = _conn()
    try:
        sql = "SELECT * FROM produtos WHERE excluido_em IS NULL"
        params = []
        busca = (busca or "").strip()
        if busca:
            sql += (" AND (descricao LIKE ? COLLATE NOCASE OR ean LIKE ? "
                    "OR codigo_interno LIKE ?)")
            params += [f"%{busca}%", f"%{busca}%", f"%{busca}%"]
        sql += " ORDER BY descricao COLLATE NOCASE LIMIT ?"
        params.append(int(limite))
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def atualizar_custo_preco(produto_id, custo_unitario=None, preco=None, usuario=None):
    """Atualiza o custo/preço vigente do produto (chamado quando um preço é
    aprovado). Não registra log_preco — isso é responsabilidade de quem aprova
    (usa registrar_log_preco), para o motivo/aprovação ficarem explícitos."""
    conn = _conn()
    try:
        sets, params = ["atualizado_em=?"], [_agora()]
        if custo_unitario is not None:
            sets.append("custo_unitario_atual=?"); params.append(dec_txt(custo_unitario))
        if preco is not None:
            sets.append("preco_atual=?"); params.append(dec_txt(preco))
        params.append(produto_id)
        r = conn.execute(f"UPDATE produtos SET {', '.join(sets)} WHERE id=? "
                         "AND excluido_em IS NULL", params)
        conn.commit()
        return r.rowcount > 0
    finally:
        conn.close()


# ── De-para (produto_fornecedor) ──────────────────────────────────────────────
def buscar_vinculo(cnpj_fornecedor, codigo_fornecedor):
    """1º passo do de-para: vínculo já confirmado para (CNPJ, código)."""
    conn = _conn()
    try:
        r = conn.execute(
            "SELECT * FROM produto_fornecedor WHERE cnpj_fornecedor=? AND codigo_fornecedor=?",
            ((cnpj_fornecedor or "").strip(), (codigo_fornecedor or "").strip())).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def criar_vinculo(produto_id, cnpj_fornecedor, codigo_fornecedor, ean=None,
                  fator_conversao_confirmado=None, usuario=None):
    """Persiste o de-para. Idempotente pelo índice único (CNPJ, código):
    reconfirmar atualiza o produto/fator em vez de duplicar."""
    cnpj = (cnpj_fornecedor or "").strip()
    codigo = (codigo_fornecedor or "").strip()
    if not produto_id or not cnpj or not codigo:
        return False, "Produto, CNPJ e código do fornecedor são obrigatórios.", None
    conn = _conn()
    try:
        existente = conn.execute(
            "SELECT id FROM produto_fornecedor WHERE cnpj_fornecedor=? AND codigo_fornecedor=?",
            (cnpj, codigo)).fetchone()
        if existente:
            conn.execute(
                "UPDATE produto_fornecedor SET produto_id=?, ean=?, "
                "fator_conversao_confirmado=?, confirmado_em=?, confirmado_por=? WHERE id=?",
                (produto_id, (ean or "").strip() or None,
                 dec_txt(fator_conversao_confirmado), _agora(), usuario, existente["id"]))
            vid = existente["id"]
            _auditar(conn, "vinculo", vid, "reconfirmar", f"{cnpj}/{codigo}", usuario)
        else:
            cur = conn.execute(
                "INSERT INTO produto_fornecedor (produto_id, cnpj_fornecedor, "
                "codigo_fornecedor, ean, fator_conversao_confirmado, confirmado_em, "
                "confirmado_por) VALUES (?,?,?,?,?,?,?)",
                (produto_id, cnpj, codigo, (ean or "").strip() or None,
                 dec_txt(fator_conversao_confirmado), _agora(), usuario))
            vid = cur.lastrowid
            _auditar(conn, "vinculo", vid, "criar", f"{cnpj}/{codigo}", usuario)
        conn.commit()
        return True, "Vínculo confirmado.", vid
    finally:
        conn.close()


# ── NCM fiscal ────────────────────────────────────────────────────────────────
def buscar_ncm_aplicavel(ncm):
    """Resolve a regra do NCM do mais específico ao mais genérico: tenta 8, 6,
    4 e 2 dígitos e devolve a primeira cadastrada (ou None). Assim uma regra por
    capítulo (2 díg.) cobre tudo abaixo até haver algo mais específico."""
    digitos = "".join(c for c in (ncm or "") if c.isdigit())
    if not digitos:
        return None
    conn = _conn()
    try:
        for tam in (8, 6, 4, 2):
            if len(digitos) >= tam:
                r = conn.execute("SELECT * FROM ncm_fiscal WHERE ncm=?",
                                 (digitos[:tam],)).fetchone()
                if r:
                    return dict(r)
        return None
    finally:
        conn.close()


def upsert_ncm(ncm, aliquota_interna=None, monofasico_pis_cofins=False,
               sujeito_st_destino=False, observacao=None, usuario=None):
    digitos = "".join(c for c in (ncm or "") if c.isdigit())
    if len(digitos) not in (2, 4, 6, 8):
        return False, "NCM deve ter 2, 4, 6 ou 8 dígitos."
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO ncm_fiscal (ncm, aliquota_interna, monofasico_pis_cofins, "
            "sujeito_st_destino, observacao, atualizado_em) VALUES (?,?,?,?,?,?) "
            "ON CONFLICT(ncm) DO UPDATE SET aliquota_interna=excluded.aliquota_interna, "
            "monofasico_pis_cofins=excluded.monofasico_pis_cofins, "
            "sujeito_st_destino=excluded.sujeito_st_destino, "
            "observacao=excluded.observacao, atualizado_em=excluded.atualizado_em",
            (digitos, dec_txt(aliquota_interna), 1 if monofasico_pis_cofins else 0,
             1 if sujeito_st_destino else 0, (observacao or "").strip() or None, _agora()))
        _auditar(conn, "ncm", digitos, "upsert", "", usuario)
        conn.commit()
        return True, "NCM salvo."
    finally:
        conn.close()


def listar_ncm(busca=None, limite=500):
    conn = _conn()
    try:
        sql = "SELECT * FROM ncm_fiscal"
        params = []
        busca = "".join(c for c in (busca or "") if c.isdigit())
        if busca:
            sql += " WHERE ncm LIKE ?"; params.append(f"{busca}%")
        sql += " ORDER BY ncm LIMIT ?"; params.append(int(limite))
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def excluir_ncm(ncm, usuario=None):
    digitos = "".join(c for c in (ncm or "") if c.isdigit())
    conn = _conn()
    try:
        r = conn.execute("DELETE FROM ncm_fiscal WHERE ncm=?", (digitos,))
        if r.rowcount:
            _auditar(conn, "ncm", digitos, "excluir", "", usuario)
        conn.commit()
        return r.rowcount > 0
    finally:
        conn.close()


# ── Parâmetros de precificação ────────────────────────────────────────────────
def upsert_parametro(escopo, escopo_id=None, pis_cofins=None, taxa_cartao=None,
                     quebra=None, margem_alvo=None, usuario=None):
    if escopo not in ("global", "secao", "subgrupo"):
        return False, "Escopo inválido."
    escopo_id = (escopo_id or "").strip() or None if escopo != "global" else None
    conn = _conn()
    try:
        existente = conn.execute(
            "SELECT id FROM parametro_precificacao WHERE escopo=? AND "
            "COALESCE(escopo_id,'')=COALESCE(?, '')", (escopo, escopo_id)).fetchone()
        vals = (dec_txt(pis_cofins), dec_txt(taxa_cartao), dec_txt(quebra),
                dec_txt(margem_alvo), _agora())
        if existente:
            conn.execute("UPDATE parametro_precificacao SET pis_cofins=?, taxa_cartao=?, "
                         "quebra=?, margem_alvo=?, vigente_desde=? WHERE id=?",
                         (*vals, existente["id"]))
            pid = existente["id"]
        else:
            cur = conn.execute(
                "INSERT INTO parametro_precificacao (escopo, escopo_id, pis_cofins, "
                "taxa_cartao, quebra, margem_alvo, vigente_desde) VALUES (?,?,?,?,?,?,?)",
                (escopo, escopo_id, *vals))
            pid = cur.lastrowid
        _auditar(conn, "parametro", pid, "upsert", f"{escopo}:{escopo_id or ''}", usuario)
        conn.commit()
        return True, "Parâmetro salvo."
    finally:
        conn.close()


def listar_parametros():
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM parametro_precificacao ORDER BY "
            "CASE escopo WHEN 'subgrupo' THEN 0 WHEN 'secao' THEN 1 ELSE 2 END, escopo_id"
        ).fetchall()]
    finally:
        conn.close()


def resolver_parametros(secao=None, subgrupo=None):
    """Resolve os parâmetros efetivos do mais específico ao mais genérico:
    subgrupo -> secao -> global. Devolve o dict do 1º escopo cadastrado, ou None
    se nem o global existir (aí o motor de preço alerta 'parâmetros ausentes').
    Só global é usado por ora, mas a resolução já está completa."""
    conn = _conn()
    try:
        tentativas = []
        if subgrupo:
            tentativas.append(("subgrupo", subgrupo.strip()))
        if secao:
            tentativas.append(("secao", secao.strip()))
        tentativas.append(("global", None))
        for escopo, escopo_id in tentativas:
            r = conn.execute(
                "SELECT * FROM parametro_precificacao WHERE escopo=? AND "
                "COALESCE(escopo_id,'')=COALESCE(?, '') ORDER BY vigente_desde DESC LIMIT 1",
                (escopo, escopo_id)).fetchone()
            if r:
                return dict(r)
        return None
    finally:
        conn.close()


# ── Notas fiscais e itens ─────────────────────────────────────────────────────
def buscar_nota_por_chave(chave_acesso):
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM nota_fiscal WHERE chave_acesso=?",
                         ((chave_acesso or "").strip(),)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def buscar_nota(nota_id):
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM nota_fiscal WHERE id=?", (nota_id,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def listar_notas(status=None, limite=200):
    conn = _conn()
    try:
        sql = "SELECT * FROM nota_fiscal"
        params = []
        if status:
            sql += " WHERE status=?"; params.append(status)
        sql += " ORDER BY importado_em DESC LIMIT ?"; params.append(int(limite))
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def itens_da_nota(nota_fiscal_id):
    conn = _conn()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM nota_fiscal_item WHERE nota_fiscal_id=? ORDER BY numero_item",
            (nota_fiscal_id,)).fetchall()]
    finally:
        conn.close()


# Colunas do item persistidas a partir do parser/motores (money/qtd em Decimal->TEXT).
_COLS_ITEM_TEXTO = (
    "codigo_fornecedor", "ean", "descricao_xml", "ncm", "cest", "cfop",
    "cst_csosn", "origem_mercadoria", "unidade_comercial", "unidade_tributavel",
)
_COLS_ITEM_DECIMAL = (
    "quantidade_comercial", "quantidade_tributavel", "fator_conversao",
    "valor_produto", "valor_desconto", "valor_ipi", "valor_frete", "valor_seguro",
    "valor_outros", "valor_icms_st", "valor_fcp_st", "credito_icms",
    "credito_pis_cofins", "custo_liquido", "custo_unitario", "preco_sugerido",
)


def _linha_item(item):
    """Normaliza um dict de item (vindo do parser/motores) para a tupla de
    INSERT, convertendo Decimal->TEXT e alertas->JSON."""
    dados = {"numero_item": item.get("numero_item"),
             "produto_id": item.get("produto_id")}
    for c in _COLS_ITEM_TEXTO:
        v = item.get(c)
        dados[c] = (str(v).strip() if v is not None else None)
    for c in _COLS_ITEM_DECIMAL:
        dados[c] = dec_txt(item.get(c))
    dados["alertas"] = json.dumps(item.get("alertas") or [], ensure_ascii=False)
    return dados


def upsert_nota_fiscal(cabecalho, itens, usuario=None):
    """Idempotência (critério de aceite): reimportar a mesma chave_acesso
    ATUALIZA o cabeçalho e SUBSTITUI os itens (delete+insert), nunca duplica.
    O de-para persiste em produto_fornecedor, então a re-resolução dos itens
    reaproveita os vínculos — não se perde trabalho de conferência.
    Preserva o status atual (não "des-confere" uma nota já conferida)."""
    chave = (cabecalho.get("chave_acesso") or "").strip()
    if len(chave) != 44:
        return False, "Chave de acesso inválida (esperado 44 dígitos).", None
    conn = _conn()
    try:
        existente = conn.execute("SELECT id, status FROM nota_fiscal WHERE chave_acesso=?",
                                 (chave,)).fetchone()
        campos = {
            "numero": cabecalho.get("numero"), "serie": cabecalho.get("serie"),
            "cnpj_emitente": cabecalho.get("cnpj_emitente"),
            "nome_emitente": cabecalho.get("nome_emitente"),
            "uf_origem": cabecalho.get("uf_origem"),
            "crt_emitente": cabecalho.get("crt_emitente"),
            "data_emissao": cabecalho.get("data_emissao"),
            "modalidade_frete": cabecalho.get("modalidade_frete"),
            "valor_total": dec_txt(cabecalho.get("valor_total")),
            "valor_frete_total": dec_txt(cabecalho.get("valor_frete_total")),
            "xml_bruto": cabecalho.get("xml_bruto"),
            "origem_ingestao": cabecalho.get("origem_ingestao"),
        }
        if existente:
            nota_id = existente["id"]
            sets = ", ".join(f"{c}=?" for c in campos)
            conn.execute(f"UPDATE nota_fiscal SET {sets}, importado_em=? WHERE id=?",
                         (*campos.values(), _agora(), nota_id))
            conn.execute("DELETE FROM nota_fiscal_item WHERE nota_fiscal_id=?", (nota_id,))
            acao = "reimportar"
        else:
            cols = ", ".join(campos)
            marks = ", ".join("?" * len(campos))
            cur = conn.execute(
                f"INSERT INTO nota_fiscal (chave_acesso, {cols}, status, importado_em) "
                f"VALUES (?, {marks}, 'pendente', ?)",
                (chave, *campos.values(), _agora()))
            nota_id = cur.lastrowid
            acao = "importar"

        col_names = (["nota_fiscal_id", "numero_item", "produto_id"]
                     + list(_COLS_ITEM_TEXTO) + list(_COLS_ITEM_DECIMAL) + ["alertas"])
        placeholders = ", ".join("?" * len(col_names))
        for item in itens:
            linha = _linha_item(item)
            valores = [nota_id, linha["numero_item"], linha["produto_id"]]
            valores += [linha[c] for c in _COLS_ITEM_TEXTO]
            valores += [linha[c] for c in _COLS_ITEM_DECIMAL]
            valores.append(linha["alertas"])
            conn.execute(
                f"INSERT INTO nota_fiscal_item ({', '.join(col_names)}) "
                f"VALUES ({placeholders})", valores)
        _auditar(conn, "nota_fiscal", nota_id, acao, chave, usuario)
        conn.commit()
        return True, "Nota importada." if acao != "reimportar" else "Nota atualizada.", nota_id
    finally:
        conn.close()


def atualizar_item(item_id, **campos):
    """Atualiza colunas de um item (usado pela conferência e pelos motores ao
    recalcular). Converte Decimal->TEXT nas colunas monetárias/quantidade e
    serializa `alertas` (lista) em JSON."""
    if not campos:
        return False
    sets, params = [], []
    for c, v in campos.items():
        if c == "alertas":
            sets.append("alertas=?"); params.append(json.dumps(v or [], ensure_ascii=False))
        elif c in _COLS_ITEM_DECIMAL:
            sets.append(f"{c}=?"); params.append(dec_txt(v))
        elif c in _COLS_ITEM_TEXTO or c in ("produto_id", "numero_item"):
            sets.append(f"{c}=?"); params.append(v)
        else:
            raise ValueError(f"Coluna de item desconhecida: {c}")
    params.append(item_id)
    conn = _conn()
    try:
        r = conn.execute(f"UPDATE nota_fiscal_item SET {', '.join(sets)} WHERE id=?", params)
        conn.commit()
        return r.rowcount > 0
    finally:
        conn.close()


def definir_status_nota(nota_id, status, usuario=None):
    if status not in STATUS_NOTA:
        return False, "Status inválido."
    conn = _conn()
    try:
        r = conn.execute("UPDATE nota_fiscal SET status=? WHERE id=?", (status, nota_id))
        if r.rowcount:
            _auditar(conn, "nota_fiscal", nota_id, "status", status, usuario)
        conn.commit()
        return r.rowcount > 0, "Status atualizado."
    finally:
        conn.close()


# ── Log de preço ──────────────────────────────────────────────────────────────
def registrar_log_preco(produto_id, preco_novo, preco_anterior=None,
                        custo_na_epoca=None, motivo="", usuario=None):
    """Grava toda alteração de preço (spec: 'toda alteração grava em log_preco
    com usuário, valores e motivo')."""
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO log_preco (produto_id, preco_anterior, preco_novo, "
            "custo_na_epoca, motivo, usuario, criado_em) VALUES (?,?,?,?,?,?,?)",
            (produto_id, dec_txt(preco_anterior), dec_txt(preco_novo),
             dec_txt(custo_na_epoca), (motivo or "").strip(), usuario, _agora()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def listar_log_preco(produto_id=None, limite=200):
    conn = _conn()
    try:
        sql = "SELECT * FROM log_preco"
        params = []
        if produto_id is not None:
            sql += " WHERE produto_id=?"; params.append(produto_id)
        sql += " ORDER BY id DESC LIMIT ?"; params.append(int(limite))
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
