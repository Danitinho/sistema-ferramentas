# -*- coding: utf-8 -*-
"""
agente/pareamento.py
Acesso da máquina ao servidor sem ninguém gerar nem colar token.

    1. Sem token no config.json, o agente sorteia uma CHAVE (fica só aqui) e
       pede acesso em /agente/api/parear com o nome do Windows.
    2. A máquina aparece em "Máquinas pedindo acesso" no painel /agente; o
       admin aprova (ou recusa).
    3. Na volta seguinte o servidor entrega o token, UMA vez, a quem tiver a
       chave. O agente grava em `token_maquina` e segue.

O token é da MÁQUINA e serve aos dois agentes (geral e reclassificação). Se o
servidor recusar o token depois (revogado no painel), `esquecer_token` apaga
e a próxima abertura do agente pede acesso de novo.

config.json nem precisa existir: é criado com o servidor padrão. Os dois
agentes podem estar abertos juntos, por isso toda gravação relê o arquivo e só
mexe nas próprias chaves.
"""
import json
import os
import secrets
import socket
import time

from agente.servidor import Cliente, ErroServidor

SERVIDOR_PADRAO = "http://192.168.0.52"
# `token_geral` é o nome antigo (gerado à mão em /agente) — também é token de
# máquina, então continua valendo.
CHAVES_TOKEN = ("token_maquina", "token_geral")
ESPERA_S = 5


def carregar(caminho):
    try:
        with open(caminho, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    if not cfg.get("servidor_url"):
        cfg["servidor_url"] = SERVIDOR_PADRAO
        _gravar(caminho, {"servidor_url": SERVIDOR_PADRAO})
    return cfg


def _gravar(caminho, mudar, apagar=()):
    """Relê, aplica só estas chaves e troca o arquivo de uma vez."""
    try:
        with open(caminho, encoding="utf-8") as f:
            cfg = json.load(f)
    except (FileNotFoundError, ValueError):
        cfg = {}
    cfg.update(mudar)
    for k in apagar:
        cfg.pop(k, None)
    tmp = caminho + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, caminho)
    return cfg


def token_de(cfg, extras=()):
    for k in CHAVES_TOKEN + tuple(extras):
        v = (cfg.get(k) or "").strip()
        if v and " " not in v:          # "cole aqui o token..." não é token
            return v
    return ""


def esquecer_token(caminho, extras=()):
    _gravar(caminho, {}, apagar=CHAVES_TOKEN + tuple(extras))


def obter_token(caminho, programa="agente", extras=(), log=print, parar=lambda: False):
    """Devolve o token da máquina, pedindo acesso e esperando a aprovação se
    preciso. None = recusado ou interrompido."""
    cfg = carregar(caminho)
    tok = token_de(cfg, extras)
    if tok:
        return tok
    chave = cfg.get("chave_pareamento") or ""
    if len(chave) < 20:
        chave = secrets.token_urlsafe(32)
        _gravar(caminho, {"chave_pareamento": chave})
    cli = Cliente(cfg["servidor_url"], "")
    maquina = socket.gethostname()
    avisado = None
    while not parar():
        try:
            r = cli.post("/agente/api/parear", {"maquina": maquina, "chave": chave,
                                                "programa": programa})
        except ErroServidor as e:
            if avisado != "rede":
                log(f"Sem contato com o servidor ({e}). Tentando de novo...")
                avisado = "rede"
            time.sleep(ESPERA_S * 2)
            continue
        estado = r.get("estado")
        if r.get("token"):
            _gravar(caminho, {"token_maquina": r["token"]}, apagar=("chave_pareamento",))
            log(f"Acesso aprovado para {maquina}.")
            return r["token"]
        if estado == "entregue":
            # o outro agente aberto nesta máquina já buscou o token
            tok = token_de(carregar(caminho), extras)
            if tok:
                return tok
            # entregue e perdido (arquivo apagado?): pede de novo
            chave = secrets.token_urlsafe(32)
            _gravar(caminho, {"chave_pareamento": chave})
            continue
        if estado == "recusado":
            _gravar(caminho, {}, apagar=("chave_pareamento",))
            log("O administrador RECUSOU o acesso desta maquina. Feche esta janela; "
                "abrir de novo faz um pedido novo.")
            return None
        if estado != avisado:
            if estado == "lotado":
                log("Muitas maquinas esperando aprovacao no servidor; tentando de novo...")
            else:
                log(f"Esta maquina ({maquina}) pediu acesso ao sistema. Aguardando o "
                    "administrador aprovar no painel do agente (/agente)...")
            avisado = estado
        time.sleep(ESPERA_S)
    return None
