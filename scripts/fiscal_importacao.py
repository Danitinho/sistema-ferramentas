"""
scripts/fiscal_importacao.py
Camada de ORQUESTRAÇÃO do módulo fiscal: liga as demais camadas sem misturá-las.

    Ingestão -> Parser -> [De-para] -> Motor de custo -> Motor de preço -> Fila
                              └──────────── orquestrado aqui ─────────────┘

Responsabilidades:
  • De-para: resolve cada item da nota a um produto interno (vínculo -> EAN ->
    fila) e aplica a precedência do fator confirmado.
  • Pipeline: parser -> de-para -> motor de custo -> motor de preço -> guarda ->
    persiste (fila) via os modelos de fiscal.py (idempotente por chave).
  • Conferência: vincular produto, confirmar fator, preencher NCM — cada ação
    RECALCULA na hora os itens afetados (inclusive em lote por NCM/nota).

Só esta camada conhece as três outras ao mesmo tempo; parser/motor continuam
puros e o modelo continua burro. Tudo em Decimal.
"""
from decimal import Decimal

from scripts import fiscal
from scripts import fiscal_motor as motor
from scripts.fiscal_parser import parse_nfe, NFeInvalida
from scripts.fiscal_ingestao import UploadManual

# Campos do item que os motores calculam e persistimos de volta.
_CAMPOS_CALCULADOS = ("credito_pis_cofins", "custo_liquido", "custo_unitario",
                      "preco_sugerido")


def _D(v, default="0"):
    return motor.D(v, default)


# ── De-para: item -> produto interno ──────────────────────────────────────────
def resolver_produto(item, cnpj_emitente, usuario=None):
    """Vincula um item a um produto interno, nesta ordem:
      1. vínculo já existente em produto_fornecedor (CNPJ, código do fornecedor);
      2. EAN batendo com produto cadastrado — cria o vínculo automaticamente;
      3. sem match — devolve None com alerta 'produto_nao_vinculado' (vai p/ fila).
    Retorna (produto_id|None, fator_confirmado|None, alertas).
    """
    cnpj = (cnpj_emitente or "").strip()
    codigo = (item.get("codigo_fornecedor") or "").strip()

    if cnpj and codigo:
        vinc = fiscal.buscar_vinculo(cnpj, codigo)
        if vinc:
            fator = vinc.get("fator_conversao_confirmado")
            return vinc["produto_id"], (_D(fator) if fator not in (None, "") else None), []

    ean = (item.get("ean") or "").strip()
    if ean:
        prod = fiscal.buscar_produto_por_ean(ean)
        if prod:
            # cria o vínculo automaticamente (sem fator confirmado ainda)
            if cnpj and codigo:
                fiscal.criar_vinculo(prod["id"], cnpj, codigo, ean=ean, usuario=usuario)
            return prod["id"], None, []

    return None, None, ["produto_nao_vinculado"]


def _regra_ncm(ncm):
    """Regra do NCM resolvida (mais específico -> genérico) ou o default de
    degradação (19%, não monofásico, sem ST) + alerta 'ncm_ausente'."""
    regra = fiscal.buscar_ncm_aplicavel(ncm)
    if regra:
        return regra, None
    return ({"aliquota_interna": str(motor.ALIQUOTA_INTERNA_PADRAO),
             "monofasico_pis_cofins": 0, "sujeito_st_destino": 0}, "ncm_ausente")


def _tem_st_retida(item):
    return item.get("cst_csosn") == "60" or _D(item.get("valor_icms_st")) > 0


# ── Pipeline de um item (de-para + custo + preço + guarda) ─────────────────────
def calcular_item(item, cnpj_emitente, usuario=None):
    """Roda o pipeline completo num item (dict com valores Decimal do parser ou
    recarregados do banco). Muta e devolve o item com produto_id, parcelas de
    custo, preço sugerido e alertas consolidados."""
    alertas = list(item.get("alertas") or [])

    # 1) de-para
    produto_id, fator_conf, alertas_dp = resolver_produto(item, cnpj_emitente, usuario)
    item["produto_id"] = produto_id
    alertas += alertas_dp

    # fator confirmado tem precedência sobre o derivado do XML
    if fator_conf is not None:
        item["fator_conversao"] = fator_conf
        alertas = [a for a in alertas if a != "fator_conversao_suspeito"]
        fator_confirmado = True
    else:
        fator_confirmado = "fator_conversao_suspeito" not in alertas

    # 2) regra de NCM (ou default 19%)
    ncm_regra, alerta_ncm = _regra_ncm(item.get("ncm"))
    if alerta_ncm:
        alertas.append(alerta_ncm)

    # 3) motor de custo
    custo = motor.calcular_custo(item, ncm_regra)
    item["custo_liquido"] = custo["custo_liquido"]
    item["custo_unitario"] = custo["custo_unitario"]
    item["credito_pis_cofins"] = custo["credito_pis_cofins"]
    alertas += custo["alertas"]

    # 4) motor de preço — só item vinculado E com fator confirmado precifica
    preco = None
    if produto_id is None:
        pass  # 'produto_nao_vinculado' já sinalizado; item não precifica
    elif not fator_confirmado:
        alertas.append("fator_nao_confirmado")   # não precifica até confirmar
    else:
        produto = fiscal.buscar_produto(produto_id) or {}
        params = fiscal.resolver_parametros(secao=produto.get("secao"),
                                            subgrupo=produto.get("subgrupo"))
        preco, alertas_preco = motor.calcular_preco(
            item.get("custo_unitario"), ncm_regra, params,
            cst_csosn=item.get("cst_csosn"), tem_st_retida=_tem_st_retida(item))
        alertas += alertas_preco
        if preco is not None:
            guarda = motor.avaliar_guarda_corpos(
                preco, item.get("custo_unitario"), produto.get("preco_atual"))
            alertas += guarda["alertas"]
    item["preco_sugerido"] = preco

    # dedup preservando ordem
    item["alertas"] = list(dict.fromkeys(alertas))
    return item


# ── Importação de uma nota (parser -> pipeline -> persiste) ───────────────────
def processar_nota_parseada(parsed, usuario=None):
    cab = parsed["cabecalho"]
    itens = parsed["itens"]
    for item in itens:
        calcular_item(item, cab.get("cnpj_emitente"), usuario)
    ok, msg, nota_id = fiscal.upsert_nota_fiscal(cab, itens, usuario)
    n_alertas = sum(len(i.get("alertas") or []) for i in itens)
    return {"ok": ok, "msg": msg, "nota_id": nota_id, "chave": cab.get("chave_acesso"),
            "emitente": cab.get("nome_emitente"), "itens": len(itens), "alertas": n_alertas}


def importar_xmls(fornecedor, usuario=None):
    """Consome uma origem (FornecedorDeXml) e importa cada NF-e. XML inválido é
    registrado com erro e NÃO interrompe o lote (nem é persistido)."""
    resultados = []
    for xml in fornecedor.obter_novos():
        try:
            parsed = parse_nfe(xml.conteudo, origem_ingestao=xml.origem)
        except NFeInvalida as e:
            resultados.append({"ok": False, "erro": str(e), "arquivo": xml.nome})
            continue
        resultados.append(processar_nota_parseada(parsed, usuario))
    return resultados


def importar_arquivos(arquivos, usuario=None):
    """Atalho para a UI: recebe [(nome, bytes)] (XMLs soltos e/ou .zip)."""
    return importar_xmls(UploadManual(arquivos), usuario)


# ── Recálculo (usado pela conferência ao resolver alertas) ────────────────────
def _item_para_calculo(row):
    """Converte uma linha de nota_fiscal_item (TEXT/Decimal) num dict de cálculo,
    zerando os campos derivados para o pipeline recomputá-los."""
    campos_valor = ("quantidade_comercial", "fator_conversao", "valor_produto",
                    "valor_desconto", "valor_ipi", "valor_frete", "valor_seguro",
                    "valor_outros", "valor_icms_st", "valor_fcp_st", "credito_icms")
    item = {"id": row["id"], "codigo_fornecedor": row.get("codigo_fornecedor"),
            "ean": row.get("ean"), "ncm": row.get("ncm"),
            "cst_csosn": row.get("cst_csosn")}
    for c in campos_valor:
        item[c] = _D(row.get(c))
    # alertas persistidos que NÃO são recomputáveis pelo pipeline devem ser
    # descartados aqui (o pipeline os regera). Recomeçamos de um alerta base:
    # o 'fator_conversao_suspeito' e 'frete_fob_ausente' vêm do parser e não são
    # regerados no recálculo, então preservamos os que não dependem de cálculo.
    preservar = {"fator_conversao_suspeito", "frete_fob_ausente"}
    import json
    try:
        antigos = set(json.loads(row.get("alertas") or "[]"))
    except Exception:
        antigos = set()
    item["alertas"] = [a for a in ("fator_conversao_suspeito", "frete_fob_ausente")
                       if a in antigos]
    return item


def recalcular_item(item_id, usuario=None):
    """Recarrega um item do banco, roda o pipeline e persiste o resultado.
    Chamado após vincular produto / confirmar fator / preencher NCM."""
    conn = fiscal._conn()
    try:
        row = conn.execute("SELECT nfi.*, nf.cnpj_emitente FROM nota_fiscal_item nfi "
                           "JOIN nota_fiscal nf ON nf.id = nfi.nota_fiscal_id "
                           "WHERE nfi.id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False
    row = dict(row)
    item = _item_para_calculo(row)
    calcular_item(item, row.get("cnpj_emitente"), usuario)
    fiscal.atualizar_item(item_id, produto_id=item.get("produto_id"),
                          fator_conversao=item.get("fator_conversao"),
                          credito_pis_cofins=item.get("credito_pis_cofins"),
                          custo_liquido=item.get("custo_liquido"),
                          custo_unitario=item.get("custo_unitario"),
                          preco_sugerido=item.get("preco_sugerido"),
                          alertas=item.get("alertas"))
    return True


def recalcular_nota(nota_id, usuario=None):
    for it in fiscal.itens_da_nota(nota_id):
        recalcular_item(it["id"], usuario)


def recalcular_por_ncm(ncm, usuario=None):
    """Recalcula todos os itens de um NCM (usado ao preencher a tabela NCM —
    resolve o alerta 'ncm_ausente' em lote)."""
    digitos = "".join(c for c in (ncm or "") if c.isdigit())
    conn = fiscal._conn()
    try:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM nota_fiscal_item WHERE ncm LIKE ?", (digitos + "%",)).fetchall()]
    finally:
        conn.close()
    for iid in ids:
        recalcular_item(iid, usuario)
    return len(ids)


# ── Ações de conferência ──────────────────────────────────────────────────────
def vincular_item(item_id, produto_id, usuario=None):
    """Vincula um item a um produto e persiste o de-para (CNPJ+código da nota),
    depois recalcula. A partir daí toda nota daquele fornecedor resolve sozinha."""
    conn = fiscal._conn()
    try:
        row = conn.execute("SELECT nfi.codigo_fornecedor, nfi.ean, nf.cnpj_emitente "
                           "FROM nota_fiscal_item nfi JOIN nota_fiscal nf "
                           "ON nf.id=nfi.nota_fiscal_id WHERE nfi.id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False, "Item não encontrado."
    fiscal.criar_vinculo(produto_id, row["cnpj_emitente"], row["codigo_fornecedor"],
                         ean=row["ean"], usuario=usuario)
    recalcular_item(item_id, usuario)
    return True, "Produto vinculado."


def confirmar_fator(item_id, fator, usuario=None):
    """Confirma o fator de conversão do item no de-para (precede o derivado) e
    recalcula. Exige que o item já esteja vinculado a um produto."""
    if _D(fator) <= 0:
        return False, "Fator inválido."
    conn = fiscal._conn()
    try:
        row = conn.execute("SELECT nfi.codigo_fornecedor, nfi.ean, nfi.produto_id, "
                           "nf.cnpj_emitente FROM nota_fiscal_item nfi JOIN nota_fiscal nf "
                           "ON nf.id=nfi.nota_fiscal_id WHERE nfi.id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False, "Item não encontrado."
    if not row["produto_id"]:
        return False, "Vincule o produto antes de confirmar o fator."
    fiscal.criar_vinculo(row["produto_id"], row["cnpj_emitente"], row["codigo_fornecedor"],
                         ean=row["ean"], fator_conversao_confirmado=fator, usuario=usuario)
    recalcular_item(item_id, usuario)
    return True, "Fator confirmado."


def preencher_ncm(ncm, aliquota_interna=None, monofasico_pis_cofins=False,
                  sujeito_st_destino=False, observacao=None, usuario=None):
    """Cadastra/edita a regra do NCM e recalcula em lote todos os itens desse
    NCM (resolve 'ncm_ausente' de uma vez)."""
    ok, msg = fiscal.upsert_ncm(ncm, aliquota_interna=aliquota_interna,
                                monofasico_pis_cofins=monofasico_pis_cofins,
                                sujeito_st_destino=sujeito_st_destino,
                                observacao=observacao, usuario=usuario)
    if not ok:
        return False, msg, 0
    n = recalcular_por_ncm(ncm, usuario)
    return True, msg, n


# ── Aprovação de preço (nada chega ao cadastro sem passar por aqui) ───────────
def aprovar_preco(item_id, aprovar_abaixo_do_custo=False, usuario=None, motivo="aprovação"):
    """Publica o preço sugerido de um item no cadastro do produto, respeitando
    os guarda-corpos. Grava em log_preco. Nenhum preço chega ao produto sem
    passar por esta função."""
    conn = fiscal._conn()
    try:
        row = conn.execute("SELECT * FROM nota_fiscal_item WHERE id=?", (item_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return False, "Item não encontrado."
    row = dict(row)
    if not row.get("produto_id"):
        return False, "Item sem produto vinculado — não pode precificar."
    if not row.get("preco_sugerido"):
        return False, "Item sem preço sugerido."
    preco = _D(row["preco_sugerido"])
    custo = _D(row["custo_unitario"]) if row.get("custo_unitario") else None
    produto = fiscal.buscar_produto(row["produto_id"]) or {}
    guarda = motor.avaliar_guarda_corpos(preco, custo, produto.get("preco_atual"))
    if guarda["bloqueado"] and not aprovar_abaixo_do_custo:
        return False, "Preço abaixo do custo líquido — requer aprovação explícita."
    fiscal.registrar_log_preco(row["produto_id"], preco,
                               preco_anterior=produto.get("preco_atual"),
                               custo_na_epoca=custo, motivo=motivo, usuario=usuario)
    fiscal.atualizar_custo_preco(row["produto_id"], custo_unitario=custo, preco=preco,
                                 usuario=usuario)
    return True, "Preço aprovado e publicado no produto."
