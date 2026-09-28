# -*- coding: utf-8 -*-
"""
scripts/agente_erp.py
Agente geral do ERP — o lado do servidor. Sem Flask.

O agente geral é um programa sem tela na máquina de quem usa o RADGe. Ele não
tem lote nem fila própria: executa PEDIDOS avulsos que o sistema faz ("abra a
última nota do fornecedor 819"), um de cada vez, e devolve o resultado. Quem
pede pode ser o painel (/agente) ou, mais adiante, outro módulo (fiscal,
vencidos) — por isso o pedido guarda o `modulo` de origem.

Persistência em `dados/agente.db`. O ESQUEMA é o mesmo do banco que sobreviveu
à queima do HD (24/09/2026): o código se perdeu, os dados não, e este módulo foi
reescrito para lê-los sem migração.

Regras:
  • A máquina é identificada pelo TOKEN, nunca pelo corpo da requisição.
  • `pegar` seleciona e marca o pedido na MESMA transação (sob o lock): dois
    agentes nunca executam o mesmo pedido.
  • Pedido tem prazo para ser PEGO (`expira_em`) e teto de execução
    (`executando_ate`). Quem pede "abra a nota" quer agora; um pedido que ficou
    esperando uma máquina desligada não pode disparar horas depois, com a tela
    de outra pessoa na frente.
  • Capacidade que GRAVA no ERP exige `autorizado_por` (uma pessoa).
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import threading
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANCO = os.path.join(BASE_DIR, "dados", "agente.db")

ESQUEMA = """
CREATE TABLE IF NOT EXISTS maquinas (
    nome        TEXT PRIMARY KEY,
    apelido     TEXT,
    token       TEXT,
    criado_em   TEXT,
    criado_por  TEXT,
    revogado_em TEXT,
    -- Telemetria devolvida pelo agente: é a única janela que o painel tem para
    -- dentro da máquina, já que lá não há tela nenhuma.
    estado      TEXT,
    msg         TEXT,
    visto_em    REAL,
    versao      TEXT,
    capacidades TEXT,        -- JSON: o que aquele agente declarou saber fazer
    -- Controle remoto: quem liga e pausa é o painel.
    comando     TEXT NOT NULL DEFAULT 'ativo',
    comando_em  TEXT,
    comando_por TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_ag_token ON maquinas(token);

CREATE TABLE IF NOT EXISTS pedidos (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    criado_em     TEXT,
    criado_por    TEXT,
    modulo        TEXT,       -- quem pediu: 'vencidos', 'fiscal', 'painel'...
    tipo          TEXT,       -- uma chave de CAPACIDADES
    params        TEXT,       -- JSON
    autorizado_por TEXT,      -- obrigatório quando a capacidade grava
    maquina_alvo  TEXT,       -- '' = qualquer máquina viva
    estado        TEXT NOT NULL DEFAULT 'pendente',
    expira_em     REAL,       -- prazo para ser PEGO (epoch)
    executando_ate REAL,      -- teto de execução depois de pego (epoch)
    maquina       TEXT,       -- quem pegou
    entregue_em   TEXT,
    concluido_em  TEXT,
    resultado     TEXT,       -- JSON devolvido pelo agente
    erro          TEXT
);
CREATE INDEX IF NOT EXISTS ix_ag_pend ON pedidos(estado, id);

CREATE TABLE IF NOT EXISTS log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    quando  TEXT,
    maquina TEXT,
    texto   TEXT
);
CREATE INDEX IF NOT EXISTS ix_ag_log ON log(maquina, id);

CREATE TABLE IF NOT EXISTS config (
    chave TEXT PRIMARY KEY,
    valor TEXT,
    em    TEXT,
    por   TEXT
);

CREATE TABLE IF NOT EXISTS auditoria (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    quando      TEXT NOT NULL,
    usuario     TEXT,
    entidade    TEXT NOT NULL,
    entidade_id TEXT,
    acao        TEXT NOT NULL,
    detalhe     TEXT
);
CREATE INDEX IF NOT EXISTS ix_ag_aud ON auditoria(entidade, entidade_id);
"""

# Estados do pedido
PENDENTE, EXECUTANDO, FEITO, ERRO, CANCELADO, EXPIRADO = (
    "pendente", "executando", "feito", "erro", "cancelado", "expirado")
ABERTOS = (PENDENTE, EXECUTANDO)

# Comandos da máquina
ATIVO, PAUSADO = "ativo", "pausado"

PRAZO_PEGAR_S = 120        # pedido que ninguém pegou em 2 min não vale mais
TETO_EXECUCAO_S = 300      # pego e sem resposta em 5 min = a máquina travou
VIVO_S = 30                # o agente fala a cada ~2 s; 30 s calado = sem contato
APARA_LOG = 600            # linhas de log guardadas por máquina


def _so_digitos(v) -> str:
    return re.sub(r"\D", "", str(v or ""))


def _codigo(v) -> str:
    """Código do RADGe: só dígitos, sem zero à esquerda (como `fornecedores.numero`)."""
    d = _so_digitos(v).lstrip("0")
    return d


def _params_abrir_ultima(p: dict) -> dict:
    forn = _codigo(p.get("fornecedor"))
    if not forn:
        raise ValueError("informe o código do fornecedor no RADGe")
    tipo = _codigo(p.get("tipo") or "3")
    if not tipo:
        raise ValueError("tipo da entrada inválido")
    return {"tipo": tipo, "fornecedor": forn}


MAX_ITENS = 100


def _qtd(v) -> float:
    """Quantidade do pedido: número ou texto pt-BR ("2,5", "1.000,5")."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        q = float(v)
    else:
        s = str(v or "").strip().replace(" ", "")
        if "," in s:
            s = s.replace(".", "").replace(",", ".")
        try:
            q = float(s)
        except ValueError:
            raise ValueError(f"quantidade inválida: '{v}'")
    if not q > 0:
        raise ValueError(f"quantidade precisa ser maior que zero: '{v}'")
    return q


def esperado_do_catalogo(barras: str) -> dict:
    """{codigo, descricao} do catálogo, só com casamento EXATO do código de
    barras (seção 17: zero à esquerda distingue cadastros). Vazio se não
    achar — o agente então confere sem catálogo, e o detalhe diz isso."""
    try:
        from scripts import catalogo
        c = catalogo.consultar(barras)
    except Exception:
        return {}
    if not c.get("encontrado") or not c.get("exato"):
        return {}
    return {"codigo": c.get("codigo_interno") or "", "descricao": c.get("descricao") or ""}


def _params_incluir_itens(p: dict) -> dict:
    itens = p.get("itens")
    if not isinstance(itens, list) or not itens:
        raise ValueError("informe ao menos um item (código de barras e quantidade)")
    if len(itens) > MAX_ITENS:
        raise ValueError(f"no máximo {MAX_ITENS} itens por pedido")
    saida = []
    for n, it in enumerate(itens, 1):
        if not isinstance(it, dict):
            raise ValueError(f"item {n} inválido")
        cb = _so_digitos(it.get("codigo"))
        if not cb:
            raise ValueError(f"item {n}: código de barras vazio")
        saida.append({"codigo": cb, "qtd": _qtd(it.get("qtd")),
                      "esperado": esperado_do_catalogo(cb)})
    out = {"itens": saida, "simular": bool(p.get("simular")),
           "nota": _so_digitos(p.get("nota"))}
    if p.get("fornecedor"):
        out.update(_params_abrir_ultima(p))      # abre a última nota antes
    return out


# O catálogo do que se pode pedir. `grava` = mexe em dado do ERP e por isso
# exige uma pessoa em `autorizado_por`. O agente declara no /api/status o que
# sabe fazer; `pegar` só entrega o que ele declarou.
CAPACIDADES = {
    "nf.abrir_ultima": {
        "rotulo": "Abrir a última nota aberta do fornecedor",
        "grava": False,
        "validar": _params_abrir_ultima,
    },
    "nf.incluir_itens": {
        "rotulo": "Incluir itens na nota de entrada",
        "grava": True,
        "validar": _params_incluir_itens,
    },
}


def teto_execucao(tipo: str, params: dict | None) -> float:
    """Segundos até o pedido pego virar `erro`. Proporcional ao número de
    itens: os 6 itens de 21/09 levaram 53 s, e 5 min fixos derrubariam uma
    lista de 50 no meio."""
    n = len((params or {}).get("itens") or [])
    return max(TETO_EXECUCAO_S, 60 + 25 * n)


def _agora() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def data_fmt(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return time.strftime("%d/%m %H:%M:%S", time.strptime(iso, "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return iso


class BancoAgente:
    """Operações serializadas por um lock (processo único do waitress — ver
    CLAUDE.md, seção 10-B, "O que não pode ser refatorado")."""

    def __init__(self, caminho: str = BANCO):
        os.makedirs(os.path.dirname(caminho), exist_ok=True)
        self.caminho = caminho
        self.con = sqlite3.connect(caminho, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA busy_timeout=5000")
        self.con.executescript(ESQUEMA)
        self.con.commit()
        self.lock = threading.RLock()
        self._avisados = set()

    # ── apoio ────────────────────────────────────────────────────────────────
    def _auditar(self, usuario, entidade, entidade_id, acao, detalhe=""):
        self.con.execute(
            "INSERT INTO auditoria (quando, usuario, entidade, entidade_id, acao, detalhe)"
            " VALUES (?,?,?,?,?,?)",
            (_agora(), usuario or "", entidade, str(entidade_id or ""), acao,
             str(detalhe)[:1000]))

    def _log(self, maquina, texto):
        self.con.execute("INSERT INTO log (quando, maquina, texto) VALUES (?,?,?)",
                         (_agora(), maquina, str(texto)[:500]))

    @staticmethod
    def _pedido_dict(r) -> dict:
        d = dict(r)
        for k in ("params", "resultado"):
            try:
                d[k] = json.loads(d[k]) if d[k] else None
            except (ValueError, TypeError):
                pass
        d["rotulo"] = (CAPACIDADES.get(d["tipo"]) or {}).get("rotulo", d["tipo"])
        d["criado_fmt"] = data_fmt(d["criado_em"])
        d["concluido_fmt"] = data_fmt(d["concluido_em"])
        return d

    def _expirar(self):
        """Varredura preguiçosa (sem timer): roda a cada `pegar` e a cada leitura
        do painel. Pendente que ninguém pegou vira `expirado`; executando além
        do teto vira `erro` — a máquina travou ou fechou no meio."""
        agora = time.time()
        self.con.execute(
            "UPDATE pedidos SET estado=?, concluido_em=?, erro=?"
            " WHERE estado=? AND expira_em IS NOT NULL AND expira_em < ?",
            (EXPIRADO, _agora(), "nenhuma máquina pegou o pedido a tempo",
             PENDENTE, agora))
        self.con.execute(
            "UPDATE pedidos SET estado=?, concluido_em=?, erro=?"
            " WHERE estado=? AND executando_ate IS NOT NULL AND executando_ate < ?",
            (ERRO, _agora(), "a máquina não respondeu dentro do prazo",
             EXECUTANDO, agora))

    # ── máquinas e tokens ────────────────────────────────────────────────────
    def criar_maquina(self, nome: str, apelido: str = "", por: str = "") -> dict:
        """Gera (ou regera) o token da máquina. `nome` = nome do computador
        (o agente manda `socket.gethostname()` só como conferência)."""
        nome = (nome or "").strip().upper()
        if not nome:
            raise ValueError("informe o nome do computador")
        token = secrets.token_urlsafe(24)
        with self.lock:
            existe = self.con.execute("SELECT 1 FROM maquinas WHERE nome=?", (nome,)).fetchone()
            if existe:
                self.con.execute(
                    "UPDATE maquinas SET token=?, apelido=COALESCE(NULLIF(?, ''), apelido),"
                    " criado_em=?, criado_por=?, revogado_em=NULL WHERE nome=?",
                    (token, apelido.strip(), _agora(), por, nome))
            else:
                self.con.execute(
                    "INSERT INTO maquinas (nome, apelido, token, criado_em, criado_por)"
                    " VALUES (?,?,?,?,?)", (nome, apelido.strip(), token, _agora(), por))
            self._auditar(por, "maquina", nome, "token", "token gerado")
            self.con.commit()
        return {"nome": nome, "token": token}

    def revogar_maquina(self, nome: str, por: str = "") -> bool:
        with self.lock:
            cur = self.con.execute(
                "UPDATE maquinas SET token=NULL, revogado_em=? WHERE nome=?", (_agora(), nome))
            if cur.rowcount:
                self._auditar(por, "maquina", nome, "revogar")
            self.con.commit()
            return bool(cur.rowcount)

    def maquina_do_token(self, token: str) -> str | None:
        token = (token or "").strip()
        if not token:
            return None
        with self.lock:
            r = self.con.execute(
                "SELECT nome FROM maquinas WHERE token=? AND revogado_em IS NULL",
                (token,)).fetchone()
        return r["nome"] if r else None

    def definir_comando(self, nome: str, comando: str, por: str = "") -> None:
        if comando not in (ATIVO, PAUSADO):
            raise ValueError("comando inválido")
        with self.lock:
            cur = self.con.execute(
                "UPDATE maquinas SET comando=?, comando_em=?, comando_por=? WHERE nome=?",
                (comando, _agora(), por, nome))
            if not cur.rowcount:
                raise ValueError(f"máquina {nome} não existe")
            self._auditar(por, "maquina", nome, "comando", comando)
            self.con.commit()

    def listar_maquinas(self) -> list[dict]:
        with self.lock:
            linhas = [dict(r) for r in self.con.execute(
                "SELECT nome, apelido, criado_em, criado_por, revogado_em,"
                " (token IS NOT NULL) AS tem_token, estado, msg, visto_em, versao,"
                " capacidades, comando, comando_em, comando_por FROM maquinas"
                " ORDER BY revogado_em IS NOT NULL, nome")]
        agora = time.time()
        for m in linhas:
            m["vivo"] = bool(m["visto_em"]) and agora - m["visto_em"] < VIVO_S
            m["silencio_s"] = int(agora - m["visto_em"]) if m["visto_em"] else None
            try:
                m["capacidades"] = json.loads(m["capacidades"] or "[]")
            except ValueError:
                m["capacidades"] = []
            if not m["vivo"]:
                m["estado"] = "sem contato" if m["visto_em"] else "nunca conectou"
        return linhas

    # ── conversa com o agente ────────────────────────────────────────────────
    def config_do_agente(self, maquina: str) -> dict:
        with self.lock:
            r = self.con.execute("SELECT comando FROM maquinas WHERE nome=?",
                                 (maquina,)).fetchone()
            mapa = self._config_bruta("mapa_erp")
        return {"comando": (r["comando"] if r else PAUSADO) or ATIVO,
                "mapa_erp": mapa, "pausa_s": 2}

    def reportar(self, maquina: str, estado="", msg="", versao="",
                 capacidades=None, log=None, hostname="") -> dict:
        with self.lock:
            self.con.execute(
                "UPDATE maquinas SET estado=?, msg=?, visto_em=?, versao=?,"
                " capacidades=COALESCE(?, capacidades) WHERE nome=?",
                (str(estado)[:40], str(msg)[:300], time.time(), str(versao)[:20],
                 json.dumps(capacidades) if capacidades is not None else None, maquina))
            par = (maquina, (hostname or "").strip().upper())
            if par[1] and par[1] != maquina and par not in self._avisados:
                # token de uma máquina usado noutra: funciona, mas o painel
                # mostra a telemetria no nome errado. Avisa UMA vez por
                # processo — a cada status (2 s) inundava o log e empurrava
                # as linhas de verdade para fora da janela de 600.
                self._avisados.add(par)
                self._log(maquina, f"(aviso) este token esta rodando em {hostname}")
            for linha in (log or [])[:50]:
                self._log(maquina, linha)
            if log:
                self.con.execute(
                    "DELETE FROM log WHERE maquina=? AND id NOT IN (SELECT id FROM log"
                    " WHERE maquina=? ORDER BY id DESC LIMIT ?)",
                    (maquina, maquina, APARA_LOG))
            self.con.commit()
        return {"ok": True, **self.config_do_agente(maquina)}

    def pegar(self, maquina: str, capacidades: list | None = None) -> dict | None:
        """Entrega o pedido mais antigo que esta máquina pode executar.
        Seleciona e marca na mesma transação, sob o lock."""
        with self.lock:
            self._expirar()
            r = self.con.execute("SELECT comando, capacidades FROM maquinas WHERE nome=?",
                                 (maquina,)).fetchone()
            if r is None or (r["comando"] or ATIVO) != ATIVO:
                self.con.commit()
                return None
            if capacidades is None:
                try:
                    capacidades = json.loads(r["capacidades"] or "[]")
                except ValueError:
                    capacidades = []
            if not capacidades:
                self.con.commit()
                return None
            marcas = ",".join("?" * len(capacidades))
            p = self.con.execute(
                f"SELECT * FROM pedidos WHERE estado=? AND tipo IN ({marcas})"
                " AND (COALESCE(maquina_alvo, '')='' OR maquina_alvo=?)"
                " ORDER BY id LIMIT 1", (PENDENTE, *capacidades, maquina)).fetchone()
            if p is None:
                self.con.commit()
                return None
            try:
                prm = json.loads(p["params"] or "{}")
            except ValueError:
                prm = {}
            self.con.execute(
                "UPDATE pedidos SET estado=?, maquina=?, entregue_em=?, executando_ate=?"
                " WHERE id=? AND estado=?",
                (EXECUTANDO, maquina, _agora(), time.time() + teto_execucao(p["tipo"], prm),
                 p["id"], PENDENTE))
            self.con.commit()
            p = self.con.execute("SELECT * FROM pedidos WHERE id=?", (p["id"],)).fetchone()
        d = self._pedido_dict(p)
        return {"id": d["id"], "tipo": d["tipo"], "params": d["params"] or {},
                "criado_por": d["criado_por"], "autorizado_por": d["autorizado_por"]}

    def concluir(self, maquina: str, pedido_id: int, ok: bool,
                 resultado=None, erro: str = "") -> dict:
        with self.lock:
            p = self.con.execute("SELECT estado, maquina FROM pedidos WHERE id=?",
                                 (pedido_id,)).fetchone()
            if p is None:
                return {"ok": False, "motivo": "pedido não existe"}
            if p["maquina"] != maquina:
                return {"ok": False, "motivo": "pedido é de outra máquina"}
            if p["estado"] != EXECUTANDO:
                # teto de execução vencido ou cancelado no painel: o resultado
                # ainda é informação útil e fica guardado, mas o estado não muda
                self.con.execute("UPDATE pedidos SET resultado=? WHERE id=?",
                                 (json.dumps(resultado, ensure_ascii=False), pedido_id))
                self.con.commit()
                return {"ok": False, "motivo": f"pedido já estava {p['estado']}"}
            self.con.execute(
                "UPDATE pedidos SET estado=?, concluido_em=?, resultado=?, erro=? WHERE id=?",
                (FEITO if ok else ERRO, _agora(),
                 json.dumps(resultado, ensure_ascii=False) if resultado is not None else None,
                 (str(erro)[:1000] or None) if not ok else None, pedido_id))
            self.con.commit()
        return {"ok": True}

    # ── pedidos (lado das pessoas) ───────────────────────────────────────────
    def criar_pedido(self, tipo: str, params: dict | None, por: str,
                     modulo: str = "painel", maquina_alvo: str = "",
                     autorizado_por: str = "") -> dict:
        cap = CAPACIDADES.get(tipo)
        if not cap:
            raise ValueError(f"o agente não sabe fazer '{tipo}'")
        params = cap["validar"](params or {})
        if cap["grava"] and not autorizado_por:
            raise ValueError("este pedido grava no ERP e precisa de uma pessoa autorizando")
        with self.lock:
            cur = self.con.execute(
                "INSERT INTO pedidos (criado_em, criado_por, modulo, tipo, params,"
                " autorizado_por, maquina_alvo, estado, expira_em) VALUES (?,?,?,?,?,?,?,?,?)",
                (_agora(), por, modulo, tipo, json.dumps(params, ensure_ascii=False),
                 autorizado_por or None, (maquina_alvo or "").strip().upper(),
                 PENDENTE, time.time() + PRAZO_PEGAR_S))
            pid = cur.lastrowid
            self._auditar(por, "pedido", pid, "pedir", f"{tipo}: {json.dumps(params, ensure_ascii=False)}")
            self.con.commit()
        return self.pedido(pid)

    def cancelar(self, pedido_id: int, por: str = "") -> bool:
        """Só o que ainda não foi pego. O que já está executando está com o ERP
        na mão de um robô; parar no meio é pela máquina (ESC segurado)."""
        with self.lock:
            cur = self.con.execute(
                "UPDATE pedidos SET estado=?, concluido_em=?, erro=? WHERE id=? AND estado=?",
                (CANCELADO, _agora(), f"cancelado por {por}", pedido_id, PENDENTE))
            if cur.rowcount:
                self._auditar(por, "pedido", pedido_id, "cancelar")
            self.con.commit()
            return bool(cur.rowcount)

    def pedido(self, pedido_id: int) -> dict | None:
        with self.lock:
            self._expirar()
            self.con.commit()
            r = self.con.execute("SELECT * FROM pedidos WHERE id=?", (pedido_id,)).fetchone()
        return self._pedido_dict(r) if r else None

    def listar_pedidos(self, limite: int = 30) -> list[dict]:
        with self.lock:
            self._expirar()
            self.con.commit()
            linhas = self.con.execute("SELECT * FROM pedidos ORDER BY id DESC LIMIT ?",
                                      (max(1, min(int(limite), 200)),)).fetchall()
        return [self._pedido_dict(r) for r in linhas]

    def ler_log(self, maquina: str = "", limite: int = 80) -> list[dict]:
        limite = max(1, min(int(limite), 400))
        with self.lock:
            if maquina:
                rs = self.con.execute("SELECT quando, maquina, texto FROM log WHERE maquina=?"
                                      " ORDER BY id DESC LIMIT ?", (maquina, limite))
            else:
                rs = self.con.execute("SELECT quando, maquina, texto FROM log"
                                      " ORDER BY id DESC LIMIT ?", (limite,))
            return [dict(r) for r in rs]

    # ── configuração (mapa do ERP) ───────────────────────────────────────────
    def _config_bruta(self, chave):
        r = self.con.execute("SELECT valor FROM config WHERE chave=?", (chave,)).fetchone()
        if not r or not r["valor"]:
            return None
        try:
            return json.loads(r["valor"])
        except (ValueError, TypeError):
            return None

    def ler_config(self, chave):
        with self.lock:
            return self._config_bruta(chave)

    def gravar_config(self, chave, valor, por=""):
        with self.lock:
            self.con.execute(
                "INSERT INTO config (chave, valor, em, por) VALUES (?,?,?,?)"
                " ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor, em=excluded.em,"
                " por=excluded.por",
                (chave, json.dumps(valor, ensure_ascii=False) if valor not in (None, "") else None,
                 _agora(), por))
            self._auditar(por, "config", chave, "gravar")
            self.con.commit()


_banco = None
_banco_lock = threading.Lock()


def banco() -> BancoAgente:
    global _banco
    with _banco_lock:
        if _banco is None:
            _banco = BancoAgente()
        return _banco
