# -*- coding: utf-8 -*-
"""
scripts/reclassificacao_routes.py
Blueprint da reclassificação merceológica (/reclassificacao).

Duas plateias no mesmo módulo:

* **O coordenador**, pelo navegador: importa a planilha, acompanha o lote, gera
  os tokens dos operadores, devolve à fila o que falhou e exporta o resultado.
  Protegido pela guarda de login do sistema, como todo o resto.

* **O programa de mesa de cada operador**, por HTTP: reserva blocos, renova o
  prazo e fecha itens. Não tem sessão de navegador, então autentica por **token**
  no cabeçalho `X-Token`. Os endpoints dele começam com `api_` e estão isentos da
  guarda de sessão (ver `PREFIXOS_PUBLICOS` em `auth_routes.py`) justamente
  porque têm guarda própria, logo abaixo.

Regra que não pode afrouxar: **o nome do operador vem do token, nunca do corpo
da requisição.** Se viesse do corpo, qualquer um poderia dizer que é outro e
fechar produto alheio — e `concluido` é terminal, não tem desfazer.
"""
from __future__ import annotations

import time
from pathlib import Path

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

from scripts import reclassificacao as r

reclassificacao_bp = Blueprint("reclassificacao", __name__,
                               url_prefix="/reclassificacao")

SAIDA = r.RAIZ / "outputs"


def _usuario() -> str:
    return session.get("usuario") or ""


# ── guarda por token, só para os endpoints do programa de mesa ────────────────
@reclassificacao_bp.before_request
def _guarda_token():
    ep = (request.endpoint or "").rsplit(".", 1)[-1]
    if not ep.startswith("api_"):
        return None
    if ep == "api_ping":               # serve para testar o endereço
        return None
    nome = r.banco().operador_do_token(request.headers.get("X-Token", ""))
    if not nome:
        return jsonify({"ok": False,
                        "erro": "token do operador ausente ou revogado"}), 401
    request.operador = nome
    return None


def _corpo() -> dict:
    return request.get_json(silent=True) or {}


@reclassificacao_bp.app_template_filter("fmt_hora")
def fmt_hora(epoch):
    """Os eventos guardam epoch float; a tela mostra data e hora legíveis."""
    try:
        return time.strftime("%d/%m %H:%M:%S", time.localtime(float(epoch or 0)))
    except (TypeError, ValueError):
        return ""


# ── API do programa de mesa ───────────────────────────────────────────────────
@reclassificacao_bp.get("/api/ping")
def api_ping():
    return jsonify({"ok": True, "servidor": "FilaReclassificacao/2.0",
                    "agora": time.time()})


@reclassificacao_bp.get("/api/estado")
def api_estado():
    return jsonify(r.banco().resumo())


@reclassificacao_bp.get("/api/eventos")
def api_eventos():
    return jsonify(r.banco().eventos_recentes(
        int(request.args.get("limite", 40))))


@reclassificacao_bp.get("/api/item")
def api_item():
    # O operador conferido é o do token: é esta chamada que o laço faz
    # imediatamente antes de escrever no ERP, para não gravar por cima de quem
    # pegou o produto depois que o prazo venceu.
    return jsonify(r.banco().estado_do_item(
        request.args.get("cod", ""), request.operador))


@reclassificacao_bp.get("/api/todos")
def api_todos():
    linhas = r.banco().todos()
    contagem = {}
    for l in linhas:
        contagem[l[1]] = contagem.get(l[1], 0) + 1
    return jsonify({"linhas": linhas, "contagem": contagem})


@reclassificacao_bp.post("/api/reservar")
def api_reservar():
    d = _corpo()
    try:
        return jsonify(r.banco().reservar(
            operador=request.operador,
            quantidade=d.get("quantidade", 50),
            so_ativos=bool(d.get("so_ativos", True)),
            confiancas=d.get("confiancas") or [],
            maquina=d.get("maquina", ""),
        ))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 400


@reclassificacao_bp.post("/api/renovar")
def api_renovar():
    return jsonify(r.banco().renovar(request.operador))


@reclassificacao_bp.post("/api/concluir")
def api_concluir():
    d = _corpo()
    return jsonify(r.banco().concluir(
        operador=request.operador,
        cod=d.get("cod", ""),
        situacao=d.get("situacao", "erro"),
        dep=d.get("dep", ""), sec=d.get("sec", ""), sub=d.get("sub", ""),
        detalhe=d.get("detalhe", ""),
    ))


@reclassificacao_bp.post("/api/liberar")
def api_liberar():
    d = _corpo()
    return jsonify({"liberados": r.banco().liberar(request.operador,
                                                   d.get("cods"))})


# ── páginas do coordenador ────────────────────────────────────────────────────
@reclassificacao_bp.route("/")
def index():
    b = r.banco()
    return render_template(
        "reclassificacao/index.html",
        resumo=b.resumo(),
        eventos=b.eventos_recentes(30),
        operadores=b.listar_operadores(),
        token_novo=request.args.get("token", ""),
        nome_novo=request.args.get("nome", ""),
        aviso=request.args.get("aviso", ""),
    )


@reclassificacao_bp.post("/importar")
def importar():
    arquivo = request.files.get("planilha")
    if not arquivo or not arquivo.filename:
        return redirect(url_for(".index", aviso="Escolha a planilha .xlsx."))
    try:
        itens = r.ler_planilha(arquivo, request.form.get("aba") or "Lista de Trabalho")
    except Exception as e:  # noqa: BLE001
        return redirect(url_for(".index", aviso=f"Não consegui ler a planilha: {e}"))

    # Reimportar é seguro de propósito: só acrescenta código que ainda não está
    # no banco e não encosta em nada já trabalhado.
    res = r.banco().semear(itens)
    return redirect(url_for(
        ".index",
        aviso=(f"Planilha lida: {len(itens):,} linhas · {res['novos']:,} novos · "
               f"{res['ja_existiam']:,} já estavam · {res['sem_destino']:,} sem "
               "destino (ficam fora da fila).").replace(",", ".")))


@reclassificacao_bp.post("/operadores")
def criar_operador():
    nome = (request.form.get("nome") or "").strip()
    try:
        novo = r.banco().criar_operador(nome, _usuario())
    except ValueError as e:
        return redirect(url_for(".index", aviso=str(e)))
    return redirect(url_for(".index", token=novo["token"], nome=novo["nome"]))


@reclassificacao_bp.post("/operadores/revogar")
def revogar_operador():
    nome = (request.form.get("nome") or "").strip()
    r.banco().revogar_operador(nome, _usuario())
    return redirect(url_for(".index", aviso=f"Token de {nome} revogado."))


@reclassificacao_bp.post("/liberar")
def liberar():
    nome = (request.form.get("operador") or "").strip()
    n = r.banco().liberar(nome)
    return redirect(url_for(".index",
                            aviso=f"{n} produtos de {nome} voltaram à fila."))


@reclassificacao_bp.post("/devolver")
def devolver():
    estado = (request.form.get("estado") or "").strip()
    try:
        n = r.banco().devolver_a_fila(estado, _usuario())
    except ValueError as e:
        return redirect(url_for(".index", aviso=str(e)))
    return redirect(url_for(".index",
                            aviso=f"{n} produtos em '{estado}' voltaram à fila."))


@reclassificacao_bp.get("/exportar")
def exportar():
    destino = Path(SAIDA) / f"reclassificacao_{time.strftime('%Y%m%d_%H%M')}.xlsx"
    r.exportar_resultado(destino)
    return send_file(destino, as_attachment=True, download_name=destino.name)
