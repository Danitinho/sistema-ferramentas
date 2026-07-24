"""
scripts/fiscal_parser.py
Camada de PARSER do módulo fiscal: XML de NF-e 4.00 -> estrutura de dados.

    Ingestão -> [Parser] -> De-para -> Motor de custo -> Motor de preço -> Fila

Responsabilidade: extrair cabeçalho e itens de uma NF-e (layout 4.00, namespace
http://www.portalfiscal.inf.br/nfe), aplicar as regras OBRIGATÓRIAS de leitura:
  • Crédito de ICMS lido do DESTACADO (nunca recalculado).
  • Rateio de frete/seguro/outras despesas do total quando ausente nos itens
    (proporcional ao vProd, com centavos exatos — sem centavo perdido).
  • Fator de conversão derivado (qTrib/qCom) + alerta de embalagem suspeita.
  • Alerta de frete FOB ausente (custo subestimado).

O parser NÃO toca no banco, NÃO resolve de-para e NÃO calcula custo líquido/
crédito de PIS-COFINS/preço — isso é das camadas seguintes. Trabalha em
`Decimal` (nunca float). A idempotência por chave_acesso é do modelo
(`fiscal.upsert_nota_fiscal`); aqui só extraímos a chave.
"""
import re
import xml.etree.ElementTree as ET
from decimal import Decimal, ROUND_HALF_UP

NS = {"n": "http://www.portalfiscal.inf.br/nfe"}
CENT = Decimal("0.01")

# Unidades comerciais que denotam EMBALAGEM (fardo, caixa, pacote, display,
# saco). Se uCom == uTrib e a unidade é uma destas, o fator provável não é 1 —
# exige conferência humana (o XML pode estar "achatando" a conversão).
EMBALAGENS = {"CX", "FD", "PC", "PCT", "DP", "SC", "FRD", "CXA"}

# CSTs de ICMS sem direito a crédito (tributação por ST anterior, isenção, não
# tributado, suspensão, diferimento). Resultam em crédito de ICMS zero.
CST_SEM_CREDITO = {"40", "41", "50", "51", "60"}


class NFeInvalida(ValueError):
    """XML que não é uma NF-e 4.00 parseável."""


# ── Helpers de extração ───────────────────────────────────────────────────────
def _txt(el, caminho):
    """Texto de um caminho relativo (com prefixo n:), stripado, ou None."""
    if el is None:
        return None
    achado = el.find(caminho, NS)
    if achado is None or achado.text is None:
        return None
    t = achado.text.strip()
    return t or None


def _filho(el, local):
    """Filho direto pelo nome local (sem precisar do prefixo namespace)."""
    if el is None:
        return None
    return el.find(f"n:{local}", NS)


def _txt_local(el, local):
    f = _filho(el, local)
    return f.text.strip() if (f is not None and f.text) else None


def _dec(el, caminho):
    """Decimal de um caminho, ou None se ausente. NUNCA usa float."""
    t = _txt(el, caminho)
    return Decimal(t) if t is not None else None


def _dec0(v):
    return v if v is not None else Decimal("0")


def _so_digitos(s):
    return re.sub(r"\D", "", s or "")


def _normalizar_data(dh):
    """dhEmi ('2026-07-20T10:30:00-03:00') ou dEmi ('2026-07-20') ->
    'AAAA-MM-DD HH:MM:SS' (data em ISO, para ordenar corretamente)."""
    if not dh:
        return None
    dh = dh.strip()
    m = re.match(r"(\d{4}-\d{2}-\d{2})[T ]?(\d{2}:\d{2}:\d{2})?", dh)
    if not m:
        return dh
    data, hora = m.group(1), m.group(2) or "00:00:00"
    return f"{data} {hora}"


# ── Rateio (frete/seguro/outros do total, proporcional ao vProd) ──────────────
def ratear(total, pesos):
    """Distribui `total` proporcionalmente a `pesos` (vProd de cada item),
    arredondando a centavos SEM perder centavo: o resíduo do arredondamento vai
    para o item de maior peso, garantindo soma exata == total.
    Critério de aceite: 'a soma dos rateios é igual ao total'."""
    total = _dec0(total)
    soma = sum(pesos, Decimal("0"))
    n = len(pesos)
    if n == 0:
        return []
    if soma <= 0 or total <= 0:
        return [Decimal("0.00") for _ in pesos]
    brutos = [(total * p / soma) for p in pesos]
    arred = [b.quantize(CENT, rounding=ROUND_HALF_UP) for b in brutos]
    residuo = total.quantize(CENT) - sum(arred, Decimal("0"))
    if residuo != 0:
        idx = max(range(n), key=lambda i: pesos[i])   # item de maior vProd absorve
        arred[idx] = (arred[idx] + residuo).quantize(CENT)
    return arred


# ── Crédito de ICMS (lido do destacado, não recalculado) ──────────────────────
def credito_icms_do_grupo(grupo):
    """Crédito de ICMS conforme o grupo de ICMS presente no item.
      • Fornecedor do Simples com crédito: usa vCredICMSSN (o destaque do SN).
      • CST 40/41/50/51/60: crédito zero.
      • Demais: usa vICMS destacado (não recalcula).
    """
    if grupo is None:
        return Decimal("0")
    vcred_sn = _txt_local(grupo, "vCredICMSSN")
    if vcred_sn is not None:
        return Decimal(vcred_sn)
    cst = _txt_local(grupo, "CST")
    if cst in CST_SEM_CREDITO:
        return Decimal("0")
    vicms = _txt_local(grupo, "vICMS")
    return Decimal(vicms) if vicms is not None else Decimal("0")


# ── Fator de conversão ────────────────────────────────────────────────────────
def fator_conversao(u_com, q_com, u_trib, q_trib):
    """Deriva o fator uCom->uTrib e sinaliza suspeita. Retorna (fator, alerta|None).
      • uCom != uTrib: fator = qTrib / qCom (conversão legítima de embalagem).
      • uCom == uTrib mas embalagem (CX/FD/PC/DP/SC...): fator provável ≠ 1 →
        alerta 'fator_conversao_suspeito' (exige confirmação humana).
    (A precedência de um fator já confirmado em produto_fornecedor é aplicada na
    camada de de-para, não aqui.)
    """
    u_com = (u_com or "").strip().upper()
    u_trib = (u_trib or "").strip().upper()
    q_com = _dec0(q_com)
    q_trib = _dec0(q_trib)
    if u_com != u_trib:
        fator = (q_trib / q_com) if q_com > 0 else Decimal("1")
        return fator, None
    # unidades iguais
    if u_com in EMBALAGENS:
        return Decimal("1"), "fator_conversao_suspeito"
    return Decimal("1"), None


# ── Parser principal ──────────────────────────────────────────────────────────
def _localizar_infnfe(xml):
    if isinstance(xml, (bytes, bytearray)):
        raiz = ET.fromstring(xml)
    else:
        raiz = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    tag = raiz.tag.split("}")[-1]
    if tag == "infNFe":
        return raiz
    if tag == "NFe":
        return raiz.find("n:infNFe", NS)
    # nfeProc (ou qualquer wrapper): busca em profundidade
    inf = raiz.find(".//n:infNFe", NS)
    if inf is None:
        raise NFeInvalida("infNFe não encontrado — não é uma NF-e 4.00.")
    return inf


def parse_nfe(xml, origem_ingestao=None):
    """Parseia um XML de NF-e 4.00 e devolve {'cabecalho': {...}, 'itens': [...]}
    com valores em Decimal e alertas por item. Levanta NFeInvalida se o XML não
    for uma NF-e parseável."""
    xml_texto = xml.decode("utf-8", errors="replace") if isinstance(xml, (bytes, bytearray)) else str(xml)
    inf = _localizar_infnfe(xml)

    chave = _so_digitos(inf.get("Id"))[-44:] if inf.get("Id") else None
    if not chave or len(chave) != 44:
        # fallback: protNFe/infProt/chNFe
        raiz = ET.fromstring(xml_texto.encode("utf-8"))
        chave = _so_digitos(_txt(raiz, ".//n:protNFe/n:infProt/n:chNFe") or "")
    if len(chave) != 44:
        raise NFeInvalida("Chave de acesso ausente ou inválida (esperado 44 dígitos).")

    ide = _filho(inf, "ide")
    emit = _filho(inf, "emit")
    transp = _filho(inf, "transp")
    icms_tot = inf.find("n:total/n:ICMSTot", NS)

    mod_frete = _txt_local(transp, "modFrete")
    cab = {
        "chave_acesso": chave,
        "numero": _txt_local(ide, "nNF"),
        "serie": _txt_local(ide, "serie"),
        "cnpj_emitente": _txt_local(emit, "CNPJ") or _txt_local(emit, "CPF"),
        "nome_emitente": _txt_local(emit, "xNome"),
        "uf_origem": _txt(emit, "n:enderEmit/n:UF"),
        "crt_emitente": _txt_local(emit, "CRT"),
        "data_emissao": _normalizar_data(_txt_local(ide, "dhEmi") or _txt_local(ide, "dEmi")),
        "modalidade_frete": mod_frete,
        "valor_total": _dec(icms_tot, "n:vNF"),
        "valor_frete_total": _dec0(_dec(icms_tot, "n:vFrete")),
        "xml_bruto": xml_texto,
        "origem_ingestao": origem_ingestao,
    }

    # 1ª passada: extrai cada item e o que ele já traz
    itens = []
    for det in inf.findall("n:det", NS):
        prod = _filho(det, "prod")
        imp = _filho(det, "imposto")
        icms = _filho(imp, "ICMS") if imp is not None else None
        grupo_icms = list(icms)[0] if (icms is not None and len(icms)) else None

        q_com = _dec0(_dec(prod, "n:qCom"))
        u_com = _txt_local(prod, "uCom")
        q_trib = _dec0(_dec(prod, "n:qTrib"))
        u_trib = _txt_local(prod, "uTrib")
        fator, alerta_fator = fator_conversao(u_com, q_com, u_trib, q_trib)

        alertas = []
        if alerta_fator:
            alertas.append(alerta_fator)

        item = {
            "numero_item": int(det.get("nItem")) if det.get("nItem") else None,
            "codigo_fornecedor": _txt_local(prod, "cProd"),
            "ean": _txt_local(prod, "cEAN") if (_txt_local(prod, "cEAN") or "").upper() != "SEM GTIN" else None,
            "descricao_xml": _txt_local(prod, "xProd"),
            "ncm": _txt_local(prod, "NCM"),
            "cest": _txt_local(prod, "CEST"),
            "cfop": _txt_local(prod, "CFOP"),
            "cst_csosn": _txt_local(grupo_icms, "CST") or _txt_local(grupo_icms, "CSOSN"),
            "origem_mercadoria": _txt_local(grupo_icms, "orig"),
            "quantidade_comercial": q_com,
            "unidade_comercial": u_com,
            "quantidade_tributavel": q_trib,
            "unidade_tributavel": u_trib,
            "fator_conversao": fator,
            "valor_produto": _dec0(_dec(prod, "n:vProd")),
            "valor_desconto": _dec0(_dec(prod, "n:vDesc")),
            "valor_ipi": _dec0(_dec(imp, "n:IPI/n:IPITrib/n:vIPI")),
            "valor_frete": _dec(prod, "n:vFrete"),     # None se ausente (decide rateio)
            "valor_seguro": _dec(prod, "n:vSeg"),
            "valor_outros": _dec(prod, "n:vOutro"),
            "valor_icms_st": _dec0(Decimal(_txt_local(grupo_icms, "vICMSST"))
                                   if _txt_local(grupo_icms, "vICMSST") else Decimal("0")),
            "valor_fcp_st": _dec0(Decimal(_txt_local(grupo_icms, "vFCPST"))
                                  if _txt_local(grupo_icms, "vFCPST") else Decimal("0")),
            "credito_icms": credito_icms_do_grupo(grupo_icms),
            "alertas": alertas,
        }
        itens.append(item)

    # 2ª passada: rateio de frete/seguro/outros quando ausentes nos itens.
    # Se a soma nos itens é zero mas o total tem valor, distribui proporcional
    # ao vProd. Se os itens já trazem, usa o que veio.
    pesos = [it["valor_produto"] for it in itens]
    for campo, total_tag in (("valor_frete", "n:vFrete"),
                             ("valor_seguro", "n:vSeg"),
                             ("valor_outros", "n:vOutro")):
        presentes = [it[campo] for it in itens]
        soma_itens = sum((v for v in presentes if v is not None), Decimal("0"))
        total = _dec0(_dec(icms_tot, total_tag))
        if soma_itens > 0:
            for it in itens:                    # usa o que os itens trouxeram
                it[campo] = _dec0(it[campo])
        elif total > 0:
            for it, parcela in zip(itens, ratear(total, pesos)):
                it[campo] = parcela
        else:
            for it in itens:
                it[campo] = Decimal("0")

    # Frete FOB ausente: modFrete=1 (FOB) e frete total zero -> custo
    # subestimado. Sinaliza em cada item (o custo de cada um está incompleto).
    if mod_frete == "1" and _dec0(cab["valor_frete_total"]) == 0:
        for it in itens:
            it["alertas"].append("frete_fob_ausente")

    return {"cabecalho": cab, "itens": itens}
