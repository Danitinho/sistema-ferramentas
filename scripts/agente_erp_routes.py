# -*- coding: utf-8 -*-
"""
scripts/agente_erp_routes.py
Blueprint do agente geral do ERP (/agente).

Duas plateias, como na reclassificação:

* **As pessoas**, pelo navegador (guarda de sessão normal): geram o token de
  cada máquina, ligam/pausam, pedem tarefas ("abrir a última nota") e leem o
  resultado e o log.
* **O agente**, por HTTP com `X-Token`: endpoints `api_*`, isentos da guarda de
  sessão via `PREFIXOS_PUBLICOS` (auth_routes.py) porque têm guarda própria
  logo abaixo. **Nenhum endpoint das pessoas pode se chamar `api_*`.**
"""
from __future__ import annotations

import json
import time

from flask import Blueprint, jsonify, redirect, render_template, request, session, url_for

from scripts import agente_erp as ag

agente_bp = Blueprint("agente", __name__, url_prefix="/agente")


def _usuario() -> str:
    return session.get("usuario") or ""


def _corpo() -> dict:
    return request.get_json(silent=True) or {}


@agente_bp.before_request
def _guarda_token():
    ep = (request.endpoint or "").rsplit(".", 1)[-1]
    if not ep.startswith("api_") or ep == "api_ping":
        return None
    nome = ag.banco().maquina_do_token(request.headers.get("X-Token", ""))
    if not nome:
        return jsonify({"ok": False, "erro": "token da máquina ausente ou revogado"}), 401
    request.maquina = nome
    return None


# ── API do agente ─────────────────────────────────────────────────────────────
@agente_bp.get("/api/ping")
def api_ping():
    return jsonify({"ok": True, "servidor": "AgenteGeral/1.0", "agora": time.time()})


@agente_bp.post("/api/status")
def api_status():
    """Telemetria + ordem seguinte numa ida só (mesmo desenho da reclassificação)."""
    d = _corpo()
    caps = d.get("capacidades")
    return jsonify(ag.banco().reportar(
        request.maquina, estado=d.get("estado", ""), msg=d.get("msg", ""),
        versao=d.get("versao", ""),
        capacidades=[c for c in caps if isinstance(c, str)] if isinstance(caps, list) else None,
        log=d.get("log") or [], hostname=d.get("maquina", "")))


@agente_bp.post("/api/pegar")
def api_pegar():
    d = _corpo()
    caps = d.get("capacidades")
    return jsonify({"pedido": ag.banco().pegar(
        request.maquina, caps if isinstance(caps, list) else None)})


@agente_bp.post("/api/concluir")
def api_concluir():
    d = _corpo()
    try:
        pid = int(d.get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "motivo": "id inválido"}), 400
    return jsonify(ag.banco().concluir(request.maquina, pid, bool(d.get("ok")),
                                       d.get("resultado"), d.get("erro") or ""))


# ── páginas das pessoas ───────────────────────────────────────────────────────
@agente_bp.get("/")
def index():
    b = ag.banco()
    mapa = b.ler_config("mapa_erp")
    return render_template(
        "agente/index.html",
        maquinas=b.listar_maquinas(),
        capacidades={k: v["rotulo"] for k, v in ag.CAPACIDADES.items()},
        mapa_txt=json.dumps(mapa, ensure_ascii=False, indent=2) if mapa else "",
        token_novo=request.args.get("token", ""), nome_novo=request.args.get("nome", ""),
        aviso=request.args.get("aviso", ""))


@agente_bp.post("/maquina")
def maquina_criar():
    try:
        m = ag.banco().criar_maquina(request.form.get("nome", ""),
                                     request.form.get("apelido", ""), _usuario())
    except ValueError as e:
        return redirect(url_for(".index", aviso=str(e)))
    return redirect(url_for(".index", token=m["token"], nome=m["nome"]))


@agente_bp.post("/maquina/<nome>/revogar")
def maquina_revogar(nome):
    ag.banco().revogar_maquina(nome, _usuario())
    return redirect(url_for(".index", aviso=f"Token de {nome} revogado."))


@agente_bp.post("/mapa")
def mapa_gravar():
    txt = (request.form.get("mapa") or "").strip()
    try:
        valor = json.loads(txt) if txt else None
    except ValueError as e:
        return redirect(url_for(".index", aviso=f"Mapa não gravado: JSON inválido ({e})."))
    ag.banco().gravar_config("mapa_erp", valor, _usuario())
    return redirect(url_for(".index", aviso="Mapa do ERP gravado; os agentes pegam na próxima volta."))


@agente_bp.get("/painel/api/estado")
def painel_estado():
    b = ag.banco()
    return jsonify({"maquinas": b.listar_maquinas(), "pedidos": b.listar_pedidos(25),
                    "log": b.ler_log(request.args.get("maquina", ""), 80)})


@agente_bp.post("/painel/api/maquina/<nome>/comando")
def painel_comando(nome):
    try:
        ag.banco().definir_comando(nome, _corpo().get("comando", ""), _usuario())
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True})


@agente_bp.post("/painel/api/pedido")
def painel_pedir():
    d = _corpo()
    # Pedido que grava no ERP sai autorizado por QUEM ESTÁ LOGADO, e só
    # quando a tela pediu isso explicitamente (confirmação do usuário). O
    # nome nunca vem do corpo.
    autorizado = _usuario() if d.get("autorizar") else ""
    try:
        p = ag.banco().criar_pedido(d.get("tipo", ""), d.get("params") or {}, _usuario(),
                                    modulo="painel", maquina_alvo=d.get("maquina_alvo", ""),
                                    autorizado_por=autorizado)
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400
    return jsonify({"ok": True, "pedido": p})


@agente_bp.post("/painel/api/conferir-itens")
def painel_conferir_itens():
    """Prévia da lista antes de mandar ao agente: cada código com o produto do
    catálogo (código no ERP + descrição) — é o que o agente vai conferir na
    linha da nota."""
    linhas = []
    for n, it in enumerate(_corpo().get("itens") or [], 1):
        cb = ag._so_digitos((it or {}).get("codigo"))
        linha = {"n": n, "codigo": cb, "qtd": None, "erro": "", "esperado": {}}
        try:
            if not cb:
                raise ValueError("código de barras vazio")
            linha["qtd"] = ag._qtd((it or {}).get("qtd"))
            linha["esperado"] = ag.esperado_do_catalogo(cb)
        except ValueError as e:
            linha["erro"] = str(e)
        linhas.append(linha)
    return jsonify({"ok": True, "linhas": linhas, "maximo": ag.MAX_ITENS})


@agente_bp.get("/painel/api/pedido/<int:pid>")
def painel_pedido(pid):
    p = ag.banco().pedido(pid)
    if not p:
        return jsonify({"erro": "pedido não existe"}), 404
    return jsonify({"ok": True, "pedido": p})


@agente_bp.post("/painel/api/pedido/<int:pid>/cancelar")
def painel_cancelar(pid):
    if not ag.banco().cancelar(pid, _usuario()):
        return jsonify({"erro": "o pedido já foi pego por uma máquina (ou já terminou)"}), 400
    return jsonify({"ok": True})
