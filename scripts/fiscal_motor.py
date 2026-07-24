"""
scripts/fiscal_motor.py
Camada de MOTORES do módulo fiscal (custo e preço) + guarda-corpos.

    Ingestão -> Parser -> De-para -> [Motor de custo] -> [Motor de preço] -> Fila

Funções PURAS: recebem o item já parseado, a regra de NCM já resolvida e os
parâmetros de precificação já resolvidos; devolvem os valores calculados e os
alertas. NÃO tocam no banco (a orquestração — de-para, lookups, persistência —
é do fiscal_importacao). Todo cálculo em `Decimal`, NUNCA float.

As regras fiscais moram em DADOS (ncm_fiscal, parametro_precificacao) — aqui só
a mecânica que as aplica. O que está hardcoded são as constantes de lei
(alíquota de crédito de PIS/COFINS não cumulativo) e defaults de degradação.
"""
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR

# Constantes de regime (Lucro Real, não cumulativo). Não são "regra por produto"
# — são a alíquota de crédito de PIS/COFINS da lei, igual para todos.
CREDITO_PIS_COFINS = Decimal("0.0925")

# Degradação graciosa: NCM sem regra cadastrada assume 19% interna, não
# monofásico, sem ST. O motor sinaliza; a tabela vai sendo preenchida.
ALIQUOTA_INTERNA_PADRAO = Decimal("0.19")

# ICMS-ST na base do crédito de PIS/COFINS: flag de configuração, PADRÃO
# DESLIGADO (posição conservadora da Receita). Não recalcular ST devido — só
# sinalizar quando suspeito (feito no parser/guarda).
INCLUIR_ICMS_ST_NA_BASE_PIS = False

Q_MOEDA = Decimal("0.01")
Q_UNIT  = Decimal("0.0001")   # custo unitário com 4 casas (evita compor erro)


def D(v, default="0"):
    if v is None or v == "":
        return Decimal(default)
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v).replace(",", "."))
    except Exception:
        return Decimal(default)


def _verdadeiro(v):
    return str(v).strip().lower() in ("1", "true", "sim", "s", "t")


# ── Motor de custo ────────────────────────────────────────────────────────────
def calcular_custo(item, ncm=None, incluir_st_na_base_pis=INCLUIR_ICMS_ST_NA_BASE_PIS):
    """Custo líquido real do item. `item` traz as parcelas (Decimal) do parser;
    `ncm` é a regra resolvida (dict de ncm_fiscal) ou None.

    Fórmulas (spec):
      custo_bruto = vProd - vDesc + vIPI + vFrete + vSeg + vOutros + vICMS_ST + vFCP_ST
      base_pis   = vProd - vDesc + vIPI + vFrete + vSeg - credito_icms   (exclui ICMS destacado, Lei 14.592/2023)
      credito_pis_cofins = base_pis * 0,0925    (zero se NCM monofásico)
      custo_liquido  = custo_bruto - credito_icms - credito_pis_cofins
      custo_unitario = custo_liquido / (qCom * fator_conversao)
    Fornecedor do Simples ainda gera crédito de PIS/COFINS (nada especial aqui —
    o crédito é do comprador); o crédito de ICMS já veio tratado do parser.
    """
    g = lambda k: D(item.get(k))
    vprod, vdesc, vipi = g("valor_produto"), g("valor_desconto"), g("valor_ipi")
    vfrete, vseg, voutros = g("valor_frete"), g("valor_seguro"), g("valor_outros")
    vst, vfcpst = g("valor_icms_st"), g("valor_fcp_st")
    credito_icms = g("credito_icms")

    custo_bruto = vprod - vdesc + vipi + vfrete + vseg + voutros + vst + vfcpst

    base_pis = vprod - vdesc + vipi + vfrete + vseg - credito_icms
    if incluir_st_na_base_pis:
        base_pis += vst

    alertas = []
    monofasico = bool(ncm and _verdadeiro(ncm.get("monofasico_pis_cofins")))
    if monofasico:
        credito_pis = Decimal("0")
        alertas.append("monofasico_sem_credito_pis_cofins")
    else:
        credito_pis = base_pis * CREDITO_PIS_COFINS
        if credito_pis < 0:                 # base negativa não gera crédito negativo
            credito_pis = Decimal("0")
    credito_pis = credito_pis.quantize(Q_MOEDA, ROUND_HALF_UP)

    custo_liquido = (custo_bruto - credito_icms - credito_pis).quantize(Q_MOEDA, ROUND_HALF_UP)

    qcom = g("quantidade_comercial")
    fator = D(item.get("fator_conversao"), default="1")
    denom = qcom * fator
    if denom > 0:
        custo_unitario = (custo_liquido / denom).quantize(Q_UNIT, ROUND_HALF_UP)
    else:
        custo_unitario = None
        alertas.append("custo_unitario_indefinido")

    return {
        "custo_bruto": custo_bruto.quantize(Q_MOEDA, ROUND_HALF_UP),
        "credito_pis_cofins": credito_pis,
        "custo_liquido": custo_liquido,
        "custo_unitario": custo_unitario,
        "alertas": alertas,
    }


# ── Arredondamento comercial (sempre para cima, até ,49 ou ,99) ───────────────
def arredondar_para_cima(valor):
    """Sobe até a terminação ,49 ou ,99 mais próxima. NUNCA para baixo:
    devolve a menor terminação >= valor."""
    v = D(valor)
    if v <= 0:
        return Decimal("0.49")
    base = v.to_integral_value(rounding=ROUND_FLOOR)
    for term in (Decimal("0.49"), Decimal("0.99"), Decimal("1.49")):
        cand = base + term
        if cand >= v:
            return cand.quantize(Q_MOEDA)
    return (base + Decimal("1.49")).quantize(Q_MOEDA)


# ── Motor de preço ────────────────────────────────────────────────────────────
def calcular_preco(custo_unitario, ncm, params, cst_csosn=None, tem_st_retida=False):
    """Preço sugerido a partir do custo unitário. `ncm` e `params` já resolvidos.
      preco = custo_unitario / (1 - (impostos_saida + despesas_variaveis + margem_alvo))
      impostos_saida = alíquota interna do NCM + PIS/COFINS de saída
        • ICMS de saída = 0 se CST 60 ou houve ST retida
        • PIS/COFINS de saída = 0 se NCM monofásico
      despesas_variaveis = taxa de cartão + quebra da seção
    Divisor <= 0 -> não precifica (alerta). Sem parâmetros -> não precifica.
    Devolve (preco|None, alertas)."""
    if custo_unitario is None:
        return None, ["sem_custo_unitario"]
    if not params:
        return None, ["parametros_ausentes"]

    cu = D(custo_unitario)
    aliq_ncm = (D(ncm.get("aliquota_interna")) if ncm and ncm.get("aliquota_interna") not in (None, "")
                else ALIQUOTA_INTERNA_PADRAO)
    monofasico = bool(ncm and _verdadeiro(ncm.get("monofasico_pis_cofins")))

    icms_saida = Decimal("0") if (cst_csosn == "60" or tem_st_retida) else aliq_ncm
    pis_saida = Decimal("0") if monofasico else D(params.get("pis_cofins"))
    impostos_saida = icms_saida + pis_saida

    despesas = D(params.get("taxa_cartao")) + D(params.get("quebra"))
    margem = D(params.get("margem_alvo"))

    divisor = Decimal("1") - (impostos_saida + despesas + margem)
    if divisor <= 0:
        return None, ["divisor_invalido"]

    preco = arredondar_para_cima(cu / divisor)
    return preco, []


# ── Guarda-corpos (avaliados antes de publicar um preço) ──────────────────────
def avaliar_guarda_corpos(preco_sugerido, custo_unitario, preco_atual=None,
                          variacao_max=Decimal("0.15"), variacao_min=Decimal("-0.10")):
    """Decisões de segurança sobre um preço proposto. Devolve
    {bloqueado, exige_aprovacao, alertas}.
      • Preço abaixo do custo líquido -> BLOQUEIA (salvo aprovação explícita).
      • Variação > +15% ou < -10% vs preço atual -> EXIGE aprovação.
      • (Sem vínculo / fator não confirmado / NCM ausente são tratados no
        orquestrador, que nem chega a precificar / já sinalizou.)
    A margem fora da faixa do subgrupo só alertaria — não há faixa por subgrupo
    cadastrada (parâmetros globais), então não se aplica ainda.
    """
    alertas, bloqueado, exige = [], False, False
    if preco_sugerido is None:
        return {"bloqueado": False, "exige_aprovacao": False, "alertas": alertas}
    preco = D(preco_sugerido)
    if custo_unitario is not None and preco < D(custo_unitario):
        bloqueado = True
        alertas.append("preco_abaixo_do_custo")
    if preco_atual not in (None, "", "0", 0):
        pa = D(preco_atual)
        if pa > 0:
            variacao = (preco - pa) / pa
            if variacao > variacao_max or variacao < variacao_min:
                exige = True
                alertas.append("variacao_fora_da_faixa")
    return {"bloqueado": bloqueado, "exige_aprovacao": exige, "alertas": alertas}
