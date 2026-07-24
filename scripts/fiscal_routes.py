"""
scripts/fiscal_routes.py
Blueprint /fiscal — UI do módulo fiscal (aba de custo real por NF-e).
Só orquestra request/response; a lógica está em fiscal / fiscal_importacao / motor.
"""
import csv
import io
from flask import (Blueprint, render_template, request, jsonify, session,
                   Response, redirect, url_for)

from scripts import fiscal
from scripts import fiscal_importacao as imp

fiscal_bp = Blueprint("fiscal", __name__, url_prefix="/fiscal")


def _usuario():
    return session.get("usuario")


# Rótulos pt-BR dos códigos de alerta (a fila agrupa por estes).
ALERTA_LABEL = {
    "produto_nao_vinculado":          "Produto não vinculado",
    "fator_conversao_suspeito":       "Fator de conversão suspeito",
    "fator_nao_confirmado":           "Fator não confirmado",
    "ncm_ausente":                    "NCM sem regra fiscal",
    "frete_fob_ausente":              "Frete FOB ausente",
    "monofasico_sem_credito_pis_cofins": "Monofásico (sem crédito PIS/COFINS)",
    "custo_unitario_indefinido":      "Custo unitário indefinido",
    "preco_abaixo_do_custo":          "Preço abaixo do custo",
    "variacao_fora_da_faixa":         "Variação de preço fora da faixa",
    "divisor_invalido":               "Margem/impostos inviabilizam o preço",
    "parametros_ausentes":            "Parâmetros de precificação ausentes",
    "sem_custo_unitario":             "Sem custo unitário",
}
# Ordem de prioridade na fila (o que trava precificação primeiro).
ALERTA_ORDEM = ["produto_nao_vinculado", "fator_nao_confirmado", "fator_conversao_suspeito",
                "ncm_ausente", "custo_unitario_indefinido", "preco_abaixo_do_custo",
                "variacao_fora_da_faixa", "divisor_invalido", "parametros_ausentes",
                "frete_fob_ausente", "monofasico_sem_credito_pis_cofins", "sem_custo_unitario"]


def _fmt(v, casas=2):
    try:
        return f"{fiscal.D(v):.{casas}f}" if v not in (None, "") else ""
    except Exception:
        return ""


def _alertas(item):
    import json
    try:
        return json.loads(item.get("alertas") or "[]")
    except Exception:
        return []


# ── Página ────────────────────────────────────────────────────────────────────
@fiscal_bp.route("/")
def index():
    return render_template(
        "fiscal/index.html",
        notas=fiscal.listar_notas(limite=100),
        ncms=fiscal.listar_ncm(limite=500),
        parametros=fiscal.listar_parametros(),
    )


# ── Importação ────────────────────────────────────────────────────────────────
@fiscal_bp.route("/api/importar", methods=["POST"])
def api_importar():
    arquivos = []
    for f in request.files.getlist("arquivos"):
        conteudo = f.read()
        if conteudo:
            arquivos.append((f.filename or "arquivo.xml", conteudo))
    if not arquivos:
        return jsonify({"ok": False, "msg": "Nenhum arquivo recebido."})
    resultados = imp.importar_arquivos(arquivos, usuario=_usuario())
    ok = sum(1 for r in resultados if r.get("ok"))
    erros = [r for r in resultados if not r.get("ok")]
    return jsonify({"ok": True, "importadas": ok, "resultados": resultados,
                    "erros": len(erros)})


@fiscal_bp.route("/api/notas")
def api_notas():
    notas = fiscal.listar_notas(status=request.args.get("status") or None, limite=200)
    saida = []
    for n in notas:
        itens = fiscal.itens_da_nota(n["id"])
        saida.append({
            "id": n["id"], "chave": n["chave_acesso"], "numero": n["numero"],
            "emitente": n["nome_emitente"], "data": n["data_emissao"],
            "status": n["status"], "itens": len(itens),
            "alertas": sum(len(_alertas(i)) for i in itens),
            "valor_total": _fmt(n["valor_total"]),
        })
    return jsonify({"ok": True, "notas": saida})


# ── Conferência (fila agrupada por tipo de alerta) ────────────────────────────
def _item_dto(item, nota):
    return {
        "id": item["id"], "nota_id": nota["id"], "chave": nota["chave_acesso"],
        "emitente": nota["nome_emitente"], "cnpj": nota["cnpj_emitente"],
        "numero_item": item["numero_item"], "codigo_fornecedor": item["codigo_fornecedor"],
        "ean": item["ean"], "descricao": item["descricao_xml"], "ncm": item["ncm"],
        "unidade_comercial": item["unidade_comercial"], "unidade_tributavel": item["unidade_tributavel"],
        "quantidade_comercial": _fmt(item["quantidade_comercial"], 3),
        "fator_conversao": _fmt(item["fator_conversao"], 4),
        "custo_liquido": _fmt(item["custo_liquido"]),
        "custo_unitario": _fmt(item["custo_unitario"], 4),
        "preco_sugerido": _fmt(item["preco_sugerido"]),
        "produto_id": item["produto_id"],
        "alertas": _alertas(item),
    }


@fiscal_bp.route("/api/fila")
def api_fila():
    """Itens pendentes agrupados por tipo de alerta (só notas não descartadas)."""
    grupos = {}
    for nota in fiscal.listar_notas(limite=500):
        if nota["status"] == "descartada":
            continue
        for item in fiscal.itens_da_nota(nota["id"]):
            als = _alertas(item)
            if not als:
                continue
            dto = _item_dto(item, nota)
            for a in als:
                grupos.setdefault(a, []).append(dto)
    fila = [{"alerta": a, "label": ALERTA_LABEL.get(a, a), "itens": grupos[a]}
            for a in ALERTA_ORDEM if a in grupos]
    total = sum(len(g["itens"]) for g in fila)
    return jsonify({"ok": True, "fila": fila, "total": total})


# ── Detalhe da nota ───────────────────────────────────────────────────────────
@fiscal_bp.route("/api/nota/<int:nota_id>")
def api_nota(nota_id):
    nota = fiscal.buscar_nota(nota_id)
    if not nota:
        return jsonify({"ok": False, "msg": "Nota não encontrada."})
    itens = [_item_dto(i, nota) for i in fiscal.itens_da_nota(nota_id)]
    # devolve também as parcelas de custo cruas para a tabela de composição
    for dto, raw in zip(itens, fiscal.itens_da_nota(nota_id)):
        dto.update({
            "valor_produto": _fmt(raw["valor_produto"]), "valor_desconto": _fmt(raw["valor_desconto"]),
            "valor_ipi": _fmt(raw["valor_ipi"]), "valor_frete": _fmt(raw["valor_frete"]),
            "valor_icms_st": _fmt(raw["valor_icms_st"]), "credito_icms": _fmt(raw["credito_icms"]),
            "credito_pis_cofins": _fmt(raw["credito_pis_cofins"]),
        })
    return jsonify({"ok": True, "nota": {
        "id": nota["id"], "chave": nota["chave_acesso"], "numero": nota["numero"],
        "emitente": nota["nome_emitente"], "cnpj": nota["cnpj_emitente"],
        "data": nota["data_emissao"], "status": nota["status"],
        "valor_total": _fmt(nota["valor_total"]),
    }, "itens": itens})


@fiscal_bp.route("/api/nota/<int:nota_id>/csv")
def api_nota_csv(nota_id):
    nota = fiscal.buscar_nota(nota_id)
    if not nota:
        return redirect(url_for("fiscal.index"))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["item", "codigo_fornecedor", "ean", "descricao", "ncm", "cfop",
                "qtd_comercial", "un", "fator", "valor_produto", "desconto", "ipi",
                "frete", "icms_st", "credito_icms", "credito_pis_cofins",
                "custo_liquido", "custo_unitario", "preco_sugerido"])
    for i in fiscal.itens_da_nota(nota_id):
        w.writerow([i["numero_item"], i["codigo_fornecedor"], i["ean"], i["descricao_xml"],
                    i["ncm"], i["cfop"], _fmt(i["quantidade_comercial"], 3),
                    i["unidade_comercial"], _fmt(i["fator_conversao"], 4),
                    _fmt(i["valor_produto"]), _fmt(i["valor_desconto"]), _fmt(i["valor_ipi"]),
                    _fmt(i["valor_frete"]), _fmt(i["valor_icms_st"]), _fmt(i["credito_icms"]),
                    _fmt(i["credito_pis_cofins"]), _fmt(i["custo_liquido"]),
                    _fmt(i["custo_unitario"], 4), _fmt(i["preco_sugerido"])])
    nome = f"nota_{nota['numero'] or nota_id}.csv"
    return Response("﻿" + buf.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{nome}"'})


# ── Produtos (para o de-para na conferência) ──────────────────────────────────
@fiscal_bp.route("/api/produtos")
def api_produtos():
    prods = fiscal.listar_produtos(busca=request.args.get("q", ""), limite=30)
    return jsonify({"ok": True, "produtos": [
        {"id": p["id"], "descricao": p["descricao"], "ean": p["ean"],
         "codigo": p["codigo_interno"], "secao": p["secao"]} for p in prods]})


@fiscal_bp.route("/api/produto", methods=["POST"])
def api_criar_produto():
    d = request.get_json() or {}
    ok, msg, pid = fiscal.criar_produto(
        d.get("descricao", ""), codigo_interno=d.get("codigo_interno"),
        ean=d.get("ean"), secao=d.get("secao"), subgrupo=d.get("subgrupo"),
        usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg,
                    "produto": {"id": pid, "descricao": (d.get("descricao") or "").strip()} if ok else None})


# ── Ações de conferência ──────────────────────────────────────────────────────
@fiscal_bp.route("/api/item/<int:item_id>/vincular", methods=["POST"])
def api_vincular(item_id):
    d = request.get_json() or {}
    ok, msg = imp.vincular_item(item_id, d.get("produto_id"), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


@fiscal_bp.route("/api/item/<int:item_id>/fator", methods=["POST"])
def api_fator(item_id):
    d = request.get_json() or {}
    ok, msg = imp.confirmar_fator(item_id, d.get("fator"), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


@fiscal_bp.route("/api/item/<int:item_id>/aprovar", methods=["POST"])
def api_aprovar(item_id):
    d = request.get_json() or {}
    ok, msg = imp.aprovar_preco(item_id, aprovar_abaixo_do_custo=bool(d.get("aprovar_abaixo_do_custo")),
                                usuario=_usuario(), motivo=d.get("motivo", "aprovação"))
    return jsonify({"ok": ok, "msg": msg})


@fiscal_bp.route("/api/nota/<int:nota_id>/status", methods=["POST"])
def api_status_nota(nota_id):
    d = request.get_json() or {}
    ok, msg = fiscal.definir_status_nota(nota_id, d.get("status", ""), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})


# ── Tabelas: NCM ──────────────────────────────────────────────────────────────
@fiscal_bp.route("/api/ncm", methods=["POST"])
def api_ncm():
    """Cadastra/edita a regra do NCM e RECALCULA em lote os itens desse NCM
    (resolve 'ncm_ausente' de uma vez)."""
    d = request.get_json() or {}
    ok, msg, n = imp.preencher_ncm(
        d.get("ncm", ""), aliquota_interna=d.get("aliquota_interna"),
        monofasico_pis_cofins=bool(d.get("monofasico_pis_cofins")),
        sujeito_st_destino=bool(d.get("sujeito_st_destino")),
        observacao=d.get("observacao"), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg, "recalculados": n})


@fiscal_bp.route("/api/ncm/<ncm>", methods=["DELETE"])
def api_del_ncm(ncm):
    return jsonify({"ok": fiscal.excluir_ncm(ncm, usuario=_usuario())})


@fiscal_bp.route("/api/ncm/csv", methods=["POST"])
def api_ncm_csv():
    """Importa regras de NCM por CSV. Colunas aceitas (cabeçalho, ; ou ,):
    ncm; aliquota_interna; monofasico_pis_cofins; sujeito_st_destino; observacao."""
    f = request.files.get("arquivo")
    if not f:
        return jsonify({"ok": False, "msg": "Envie um arquivo CSV."})
    texto = f.read().decode("utf-8-sig", errors="replace")
    dialeto = ";" if texto.count(";") >= texto.count(",") else ","
    leitor = csv.DictReader(io.StringIO(texto), delimiter=dialeto)
    n = 0
    for linha in leitor:
        linha = { (k or "").strip().lower(): (v or "").strip() for k, v in linha.items() }
        ncm = linha.get("ncm")
        if not ncm:
            continue
        ok, _msg = fiscal.upsert_ncm(
            ncm, aliquota_interna=linha.get("aliquota_interna") or None,
            monofasico_pis_cofins=linha.get("monofasico_pis_cofins") in ("1", "sim", "true", "s"),
            sujeito_st_destino=linha.get("sujeito_st_destino") in ("1", "sim", "true", "s"),
            observacao=linha.get("observacao"), usuario=_usuario())
        if ok:
            imp.recalcular_por_ncm(ncm, usuario=_usuario())
            n += 1
    return jsonify({"ok": True, "msg": f"{n} NCM(s) importado(s).", "importados": n})


# ── Tabelas: parâmetros de precificação ───────────────────────────────────────
@fiscal_bp.route("/api/parametro", methods=["POST"])
def api_parametro():
    d = request.get_json() or {}
    ok, msg = fiscal.upsert_parametro(
        d.get("escopo", "global"), escopo_id=d.get("escopo_id"),
        pis_cofins=d.get("pis_cofins"), taxa_cartao=d.get("taxa_cartao"),
        quebra=d.get("quebra"), margem_alvo=d.get("margem_alvo"), usuario=_usuario())
    return jsonify({"ok": ok, "msg": msg})
