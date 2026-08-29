# -*- coding: utf-8 -*-
"""
scripts/reclassificacao_routes.py
Blueprint da reclassificação merceológica (/reclassificacao).

Duas plateias no mesmo módulo:

* **As pessoas**, pelo navegador: o coordenador importa a planilha, acompanha o
  lote, gera tokens, LIGA E PARA o agente de cada máquina e vê o log dele; o
  curador confirma o destino dos produtos em `/curadoria` — de TODOS eles, pois
  nada é gravado no ERP sem uma pessoa passar antes. Protegido pela guarda de
  login do sistema, como todo o resto.

* **O agente de cada operador**, por HTTP: pergunta o que fazer
  (`/api/config`), reserva blocos, fecha itens e conta o que aconteceu
  (`/api/status`). Não tem tela e não tem sessão de navegador, então autentica
  por **token** no cabeçalho `X-Token`. Os endpoints dele começam com `api_` e
  estão isentos da guarda de sessão (ver `PREFIXOS_PUBLICOS` em
  `auth_routes.py`) justamente porque têm guarda própria, logo abaixo.

Duas regras que não podem afrouxar:

1. **O nome do operador vem do token, nunca do corpo da requisição.** Se viesse
   do corpo, qualquer um poderia dizer que é outro e fechar produto alheio — e
   `concluido` é terminal, não tem desfazer.
2. **Nenhum endpoint que não seja do agente pode se chamar `api_*`.** O prefixo
   isenta da guarda de sessão; um `api_curar` ficaria gravável sem login. Os da
   curadoria se chamam `cur_*`, com `/api/` só no caminho.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from flask import (Blueprint, jsonify, redirect, render_template, request,
                   send_file, session, url_for)

from scripts import reclassificacao as r
from scripts import reclassificacao_estrutura as est

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


@reclassificacao_bp.get("/api/config")
def api_config():
    """O que o agente deve fazer agora — e com que parâmetros.

    O agente não tem tela: é aqui que ele descobre se deve rodar, pausar ou
    parar, de quantos em quantos itens, se é simulação, e como achar os campos
    do RADGe (`mapa_erp`). Instalar uma máquina nova é colar o token.
    """
    return jsonify(r.banco().config_do_agente(request.operador))


@reclassificacao_bp.post("/api/status")
def api_status():
    """O agente conta o que está acontecendo e já recebe a ordem seguinte.

    Uma ida só por volta do laço: a resposta é o mesmo corpo de `/api/config`.
    """
    d = _corpo()
    return jsonify(r.banco().reportar_status(
        operador=request.operador,
        estado=d.get("estado", ""), msg=d.get("msg", ""),
        feitos=d.get("feitos", 0), atual=d.get("atual", ""),
        maquina=d.get("maquina", ""), log=d.get("log") or [],
    ))


# ── páginas do coordenador ────────────────────────────────────────────────────
@reclassificacao_bp.route("/")
def index():
    b = r.banco()
    resumo = b.resumo()
    return render_template(
        "reclassificacao/index.html",
        resumo=resumo,
        curadoria=b.resumo_curadoria(),
        eventos=b.eventos_recentes(30),
        operadores=resumo["operadores"],
        mapa_erp=json.dumps(b.ler_config("mapa_erp") or {}, ensure_ascii=False,
                            indent=1) if b.ler_config("mapa_erp") else "",
        estrutura_ok=est.estrutura().carregada,
        estrutura_erro=est.estrutura().erro,
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
               f"{res['ja_existiam']:,} já estavam. Todos vão para a curadoria "
               f"({res['sem_destino']:,} deles sem sugestão de destino) — "
               "nenhum produto é liberado sem confirmação."
               ).replace(",", ".")))


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


# ── curadoria: a decisão humana, que saiu do meio da rodada ───────────────────
# ATENÇÃO ao nome destas funções: a guarda de sessão isenta tudo que começa com
# `api_` (é a API do agente, que tem token próprio). Endpoint de curadoria NUNCA
# pode se chamar `api_alguma_coisa` — ficaria gravável sem login. Por isso o
# prefixo `cur_`, com o `/api/` só no caminho (assim um fetch deslogado recebe
# 401 JSON em vez de um redirecionamento para a tela de login).

@reclassificacao_bp.get("/curadoria")
def cur_pagina():
    """Conferência um a um — o caminho normal da curadoria."""
    b = r.banco()
    e = est.estrutura()
    return render_template(
        "reclassificacao/curadoria.html",
        curadoria=b.resumo_curadoria(),
        arvore=e.arvore(),
        estrutura_ok=e.carregada,
        estrutura_erro=e.erro,
        departamentos=e.lista_departamentos(),
    )


@reclassificacao_bp.get("/curadoria/api/fila")
def cur_fila():
    b = r.banco()
    e = est.estrutura()
    dados = b.fila_curadoria(
        limite=int(request.args.get("limite", 60)),
        confianca=request.args.get("confianca", ""),
        dep=request.args.get("dep", ""),
        busca=(request.args.get("busca", "") or "").strip(),
        so_ativos=request.args.get("so_ativos") == "1",
    )
    # Os nomes vêm prontos do servidor: a tela não deve ter de saber a
    # hierarquia para escrever "Bebidas › Não Alcoólicas › Sucos".
    for it in dados["itens"]:
        it["destino_nome"] = e.rotulo_curto(it["dep_novo"], it["sec_novo"],
                                            it["sub_novo"])
        it["atual_nome"] = e.rotulo_curto(it["dep_atual"], it["sec_atual"],
                                          it["sub_atual"])
    dados["resumo"] = b.resumo_curadoria()
    return jsonify(dados)


@reclassificacao_bp.post("/curadoria/api/aceitar")
def cur_aceitar():
    d = _corpo()
    return jsonify(r.banco().aceitar_sugestao(d.get("cods") or [], _usuario()))


@reclassificacao_bp.post("/curadoria/api/curar")
def cur_curar():
    d = _corpo()
    return jsonify(r.banco().curar(
        d.get("cods") or [], d.get("dep", ""), d.get("sec", ""),
        d.get("sub", ""), por=_usuario(), nota=d.get("nota", "")))


@reclassificacao_bp.post("/curadoria/api/descartar")
def cur_descartar():
    d = _corpo()
    return jsonify(r.banco().descartar(d.get("cods") or [], _usuario(),
                                       d.get("motivo", "")))


@reclassificacao_bp.post("/curadoria/reverter-descartes")
def cur_reverter():
    n = r.banco().reverter_descarte(_usuario())
    return redirect(url_for(".index",
                            aviso=f"{n} descartados voltaram para a curadoria."))


# ── controle do agente pelo painel ───────────────────────────────────────────
# É aqui que "toda a interface na web" se paga: ligar, pausar, parar e
# configurar a máquina do operador sem sair do navegador.

@reclassificacao_bp.post("/agente/comando")
def agente_comando():
    nome = (request.form.get("operador") or "").strip()
    comando = (request.form.get("comando") or "").strip()
    try:
        r.banco().definir_comando(nome, comando, _usuario())
    except ValueError as e:
        return redirect(url_for(".index", aviso=str(e)))
    rotulo = {"rodar": "vai começar", "pausar": "vai pausar",
              "parar": "vai parar"}.get(comando, comando)
    return redirect(url_for(
        ".index", aviso=f"{nome}: o agente {rotulo} na próxima volta do laço "
                        "(alguns segundos)."))


@reclassificacao_bp.post("/agente/parametros")
def agente_parametros():
    nome = (request.form.get("operador") or "").strip()
    try:
        r.banco().definir_parametros(
            nome, por=_usuario(),
            bloco=request.form.get("bloco"),
            so_ativos=request.form.get("so_ativos") == "1",
            simular=request.form.get("simular") == "1",
            pular_certos=request.form.get("pular_certos") == "1",
            limiar=request.form.get("limiar"),
        )
    except (ValueError, TypeError) as e:
        return redirect(url_for(".index", aviso=f"Parâmetro inválido: {e}"))
    return redirect(url_for(".index", aviso=f"Parâmetros de {nome} salvos."))


@reclassificacao_bp.get("/painel/api/estado")
def painel_estado():
    """Alimenta a atualização ao vivo do painel (operadores + log do agente)."""
    b = r.banco()
    resumo = b.resumo()
    return jsonify({
        "operadores": resumo["operadores"],
        "por_estado": resumo["por_estado"],
        "prontos": resumo["prontos"],
        "aguardando_curadoria": resumo["aguardando_curadoria"],
        "log": b.log_do_agente(request.args.get("operador", ""),
                               int(request.args.get("limite", 60))),
        "agora": resumo["agora"],
    })


@reclassificacao_bp.post("/erp/mapa")
def erp_mapa():
    """Mapa dos campos do RADGe, guardado no servidor.

    Antes vivia no config.json de cada PC: recalibrar depois de uma atualização
    do ERP era ir de máquina em máquina. Agora é um campo aqui, e todo agente
    pega o novo na volta seguinte.
    """
    bruto = (request.form.get("mapa") or "").strip()
    if not bruto:
        r.banco().gravar_config("mapa_erp", None, _usuario())
        return redirect(url_for(".index", aviso="Mapa do ERP apagado — os "
                                                "agentes voltam a usar o "
                                                "config.json local."))
    try:
        valor = json.loads(bruto)
    except ValueError as e:
        return redirect(url_for(".index", aviso=f"JSON inválido: {e}"))
    if not isinstance(valor, dict):
        return redirect(url_for(".index", aviso="O mapa precisa ser um objeto JSON."))
    r.banco().gravar_config("mapa_erp", valor, _usuario())
    return redirect(url_for(".index", aviso="Mapa do ERP salvo. Cada agente "
                                            "recebe na próxima volta do laço."))


@reclassificacao_bp.post("/curadoria/api/aceitar-destino")
def cur_aceitar_destino():
    """Confirma de uma vez todos os pendentes de um destino, não só os da tela."""
    d = _corpo()
    return jsonify(r.banco().aceitar_destino(
        d.get("dep", ""), d.get("sec", ""), d.get("sub", ""), por=_usuario(),
        confianca=d.get("confianca", ""), so_ativos=bool(d.get("so_ativos"))))


@reclassificacao_bp.get("/curadoria/lista")
def cur_lista():
    """A vista em lista, para achar um produto ou varrer um departamento.

    A conferência um a um é o caminho normal; esta aqui é para quando se sabe o
    que se procura. Ela também confirma em lote, e é de propósito: quem chega
    por busca já sabe o que está olhando.
    """
    b = r.banco()
    e = est.estrutura()
    return render_template(
        "reclassificacao/curadoria_lista.html",
        curadoria=b.resumo_curadoria(),
        arvore=e.arvore(),
        estrutura_ok=e.carregada,
        estrutura_erro=e.erro,
        departamentos=e.lista_departamentos(),
    )


@reclassificacao_bp.post("/curadoria/api/desfazer")
def cur_desfazer():
    """Desfaz confirmações que ainda não viraram trabalho de nenhum agente."""
    d = _corpo()
    return jsonify(r.banco().descurar(d.get("cods") or [], _usuario()))
