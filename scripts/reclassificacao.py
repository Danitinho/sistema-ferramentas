# -*- coding: utf-8 -*-
"""
scripts/reclassificacao.py
Fila compartilhada da reclassificação merceológica — lógica e persistência.

Contexto: 65 mil produtos do cadastro estão em departamentos legados e precisam
de departamento, seção e subseção corrigidos no ERP RADGe. Quem edita o ERP é um
programa de mesa que roda na máquina de cada operador (pywinauto, controla a
janela do RADGe). Este módulo é a outra metade: decide **quem fica com qual
produto**, e é o único escritor do banco do lote.

Por que a divisão: o programa de mesa precisa da área de trabalho do Windows com
o ERP aberto, coisa que um servidor web não alcança. Já a fila não sabe nada de
ERP nem de interface — é só estado. Esta metade cabe aqui; a outra não.

TODA A INTERFACE MORA AQUI. O programa de mesa virou um **agente sem tela**: ele
pergunta o que fazer (`config_do_agente`), executa e conta o que aconteceu
(`reportar_status`). Quem liga, pausa, configura e vê o log é o navegador. Isso
só é possível porque a decisão saiu de dentro da rodada:

    ANTES  reservar -> [operador confirma produto a produto, ERP parado] -> gravar
    AGORA  curadoria no navegador -> reservar -> gravar sem parar para perguntar

A confirmação humana não desapareceu: ela mudou de momento. Continua havendo uma
pessoa por trás de cada gravação — só que ela decide antes, em lote, e não com o
ERP travado esperando.

`itens.pronto` é a fronteira, e ela é **intransponível sem uma pessoa**: nenhum
produto nasce pronto, nem os de confiança ALTA. Todo item espera alguém confirmar
na tela de curadoria — em lote, por destino, longe do ERP e paralelizável — e só
então `reservar` o entrega ao agente, que grava pelo driver do ERP.

**A confiança é indicativa, não é autorização.** ALTA/MÉDIA/BAIXA/REVISAR ordena
a fila, escolhe a cor do rótulo e diz o quanto o palpite da planilha merece
atenção. Não libera gravação. Foi uma decisão explícita: o palpite acerta muito,
mas "muito" não é "sempre", e `concluido` é terminal — um cadastro gravado errado
no automático não tem desfazer.

A atomicidade da reserva é a garantia inteira do sistema: selecionar e marcar
acontecem na MESMA transação. Separar em "buscar livres" e depois "marcar"
reabre a corrida que a fila existe para fechar. Não refatore isso.

ATENÇÃO AO DEPLOY: o lock abaixo serializa dentro de UM processo. O waitress
roda em processo único com threads, então funciona. Se um dia o sistema passar a
vários processos worker, a atomicidade cai e a fila precisa de outra trava.

Estados do item:

    sem_destino ─curadoria─┐
    livre (pronto=0) ──────┴─> livre (pronto=1) ─reservar─> reservado
                           └─> descartado        (fora do lote, guarda o motivo)

    reservado -> concluido    (terminal, nunca volta)
              -> falhou       (pilha à parte, não volta sozinho)
              -> simulado     (não contou como feito)
              -> livre        (quando o prazo vence)
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
BANCO = RAIZ / "dados" / "reclassificacao.db"

LIVRE = "livre"
RESERVADO = "reservado"
CONCLUIDO = "concluido"
FALHOU = "falhou"
SIMULADO = "simulado"
SEM_DESTINO = "sem_destino"
DESCARTADO = "descartado"      # curador decidiu que este produto não entra no lote

# Comandos que o painel dá ao agente de cada máquina. O agente não tem tela:
# ele pergunta ("GET /api/config") e obedece. Ver `config_do_agente`.
RODAR = "rodar"
PAUSAR = "pausar"
PARAR = "parar"
COMANDOS = (RODAR, PAUSAR, PARAR)

# Como cada resultado enviado pelo programa de mesa cai na máquina de estados.
# `simulado` é separado porque uma rodada em simulação não grava nada no ERP:
# marcá-la como concluído faria a rodada de verdade pular o produto. E também
# não volta sozinho à fila, senão a simulação giraria nos mesmos itens.
ESTADO_DE = {
    "alterado": CONCLUIDO,
    "ja_correto": CONCLUIDO,
    "pulado": CONCLUIDO,
    "simulado": SIMULADO,
    "erro": FALHOU,
}

ORDEM_CONF = {"ALTA": 0, "MEDIA": 1, "BAIXA": 2, "REVISAR": 3}
ORDEM_CONF["MÉDIA"] = 1          # a planilha usa a forma acentuada

LEASE_PADRAO_S = 20 * 60         # a reserva é prazo, não trava

COLUNAS = {
    "cod": "Cód. Barras",
    "produto": "Produto",
    "ativo": "Ativo",
    "ano": "Últ. mov.",
    "dep_atual": "CodGrp1 atual",
    "sec_atual": "CodGrp2 atual",
    "sub_atual": "CodGrp3 atual",
    "dep_novo": "CodGrp1",
    "sec_novo": "CodGrp2",
    "sub_novo": "CodGrp3",
    "confianca": "Confiança",
    "base": "Base da sugestão",
}

ESQUEMA = """
CREATE TABLE IF NOT EXISTS itens (
    cod          TEXT PRIMARY KEY,
    linha        INTEGER,
    produto      TEXT,
    ativo        INTEGER,
    ano          TEXT,
    dep_atual    TEXT, sec_atual TEXT, sub_atual TEXT,
    dep_novo     TEXT, sec_novo  TEXT, sub_novo  TEXT,
    confianca    TEXT,
    ordem_conf   INTEGER,
    base         TEXT,
    estado       TEXT NOT NULL DEFAULT 'livre',
    operador     TEXT,
    reservado_em REAL,
    expira_em    REAL,
    situacao     TEXT,
    dep_grav     TEXT, sec_grav TEXT, sub_grav TEXT,
    detalhe      TEXT,
    concluido_em REAL,
    -- Curadoria: `pronto` é o que separa o que o agente pode executar sozinho
    -- do que ainda espera decisão humana no navegador.
    pronto       INTEGER NOT NULL DEFAULT 0,
    curado_em    REAL,
    curado_por   TEXT,
    curado_nota  TEXT
);
-- Os índices que citam `pronto` são criados em `_migrar`, DEPOIS do ALTER
-- TABLE: num banco antigo esta seção roda antes de a coluna existir.
CREATE INDEX IF NOT EXISTS ix_fila
    ON itens(estado, ordem_conf, dep_novo, sec_novo, sub_novo);
CREATE INDEX IF NOT EXISTS ix_expira ON itens(estado, expira_em);
CREATE INDEX IF NOT EXISTS ix_oper   ON itens(operador, estado);

CREATE TABLE IF NOT EXISTS eventos (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    quando    REAL,
    operador  TEXT,
    cod       TEXT,
    acao      TEXT,
    detalhe   TEXT,
    dep_antes TEXT, sec_antes TEXT, sub_antes TEXT,
    dep_dep   TEXT, sec_dep   TEXT, sub_dep   TEXT
);
CREATE INDEX IF NOT EXISTS ix_ev_quando ON eventos(quando);

CREATE TABLE IF NOT EXISTS operadores (
    nome       TEXT PRIMARY KEY,
    maquina    TEXT,
    visto_em   REAL,
    token      TEXT,
    criado_em  TEXT,
    criado_por TEXT,
    revogado_em TEXT,
    -- Controle remoto do agente daquela máquina (o programa de mesa não tem
    -- tela: quem liga, pausa e configura é o painel).
    comando     TEXT NOT NULL DEFAULT 'parar',
    comando_em  REAL,
    comando_por TEXT,
    bloco        INTEGER NOT NULL DEFAULT 100,
    so_ativos    INTEGER NOT NULL DEFAULT 1,
    simular      INTEGER NOT NULL DEFAULT 0,
    pular_certos INTEGER NOT NULL DEFAULT 1,
    limiar       REAL    NOT NULL DEFAULT 0.8,
    -- Telemetria devolvida pelo agente (é o que o painel mostra ao vivo).
    agente_estado TEXT,
    agente_msg    TEXT,
    agente_feitos INTEGER NOT NULL DEFAULT 0,
    agente_atual  TEXT,
    agente_em     REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_token ON operadores(token);

-- Log do agente. Ele não tem janela para escrever, então escreve aqui e o
-- painel lê. Aparado em APARA_LOG linhas por operador a cada gravação.
CREATE TABLE IF NOT EXISTS agente_log (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    quando   REAL,
    operador TEXT,
    texto    TEXT
);
CREATE INDEX IF NOT EXISTS ix_log_oper ON agente_log(operador, id);

-- Configuração global do lote, inclusive o MAPA DO ERP (nomes de classe e
-- âncoras dos campos do RADGe). Fica no servidor para que instalar uma máquina
-- nova seja só colar o token: o resto o agente baixa.
CREATE TABLE IF NOT EXISTS config (
    chave TEXT PRIMARY KEY,
    valor TEXT,
    em    TEXT,
    por   TEXT
);
"""

APARA_LOG = 400          # linhas de log mantidas por operador


# ── leitura da planilha ───────────────────────────────────────────────────────
@dataclass
class Item:
    linha: int
    cod: str
    produto: str
    ativo: bool
    ano: str
    dep_atual: str
    sec_atual: str
    sub_atual: str
    dep_novo: str
    sec_novo: str
    sub_novo: str
    confianca: str
    base: str


def _txt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def ler_planilha(origem, aba: str = "Lista de Trabalho") -> list[Item]:
    """Lê o xlsx de reclassificação. Aceita caminho ou arquivo enviado.

    As colunas são localizadas pelo NOME no cabeçalho, nunca pela posição: se a
    planilha mudar de layout o carregamento falha com mensagem clara, em vez de
    ler a coluna errada em silêncio.
    """
    from openpyxl import load_workbook

    wb = load_workbook(origem, read_only=True, data_only=True)
    ws = wb[aba] if aba in wb.sheetnames else wb.worksheets[0]

    linhas = ws.iter_rows(values_only=True)
    cabecalho = [_txt(c) for c in next(linhas)]
    faltando = [n for n in COLUNAS.values() if n not in cabecalho]
    if faltando:
        wb.close()
        raise ValueError(
            "A planilha não tem as colunas esperadas: " + ", ".join(faltando)
        )
    idx = {k: cabecalho.index(v) for k, v in COLUNAS.items()}

    itens: list[Item] = []
    for n, row in enumerate(linhas, start=2):
        def g(k):
            return _txt(row[idx[k]]) if idx[k] < len(row) else ""

        cod = g("cod")
        if not cod:
            continue
        itens.append(Item(
            linha=n, cod=cod, produto=g("produto"),
            ativo=g("ativo").upper() == "SIM", ano=g("ano"),
            dep_atual=g("dep_atual"), sec_atual=g("sec_atual"),
            sub_atual=g("sub_atual"),
            dep_novo=g("dep_novo"), sec_novo=g("sec_novo"), sub_novo=g("sub_novo"),
            confianca=g("confianca"), base=g("base"),
        ))
    wb.close()
    return itens


# ── banco ─────────────────────────────────────────────────────────────────────
class FilaBanco:
    """Todas as operações são serializadas por um lock.

    A carga é mínima (poucos operadores, uma requisição a cada segundos), então
    não vale disputar granularidade: um lock só elimina qualquer dúvida sobre a
    atomicidade da reserva, que é a única coisa que não pode dar errado aqui.
    """

    def __init__(self, caminho: str | Path = BANCO):
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(self.caminho), check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.execute("PRAGMA synchronous=NORMAL")
        self.con.execute("PRAGMA busy_timeout=5000")
        self.con.executescript(ESQUEMA)
        self._migrar()
        self.con.commit()
        self.lock = threading.RLock()

    def _migrar(self):
        """Acrescenta colunas novas em banco antigo. Idempotente."""
        tem = {r[1] for r in self.con.execute("PRAGMA table_info(operadores)")}
        for coluna, tipo in (
                ("token", "TEXT"), ("criado_em", "TEXT"),
                ("criado_por", "TEXT"), ("revogado_em", "TEXT"),
                ("comando", "TEXT NOT NULL DEFAULT 'parar'"),
                ("comando_em", "REAL"), ("comando_por", "TEXT"),
                ("bloco", "INTEGER NOT NULL DEFAULT 100"),
                ("so_ativos", "INTEGER NOT NULL DEFAULT 1"),
                ("simular", "INTEGER NOT NULL DEFAULT 0"),
                ("pular_certos", "INTEGER NOT NULL DEFAULT 1"),
                ("limiar", "REAL NOT NULL DEFAULT 0.8"),
                ("agente_estado", "TEXT"), ("agente_msg", "TEXT"),
                ("agente_feitos", "INTEGER NOT NULL DEFAULT 0"),
                ("agente_atual", "TEXT"), ("agente_em", "REAL")):
            if coluna not in tem:
                self.con.execute(
                    f"ALTER TABLE operadores ADD COLUMN {coluna} {tipo}")

        tem_i = {r[1] for r in self.con.execute("PRAGMA table_info(itens)")}
        novas_curadoria = "pronto" not in tem_i
        for coluna, tipo in (("pronto", "INTEGER NOT NULL DEFAULT 0"),
                             ("curado_em", "REAL"), ("curado_por", "TEXT"),
                             ("curado_nota", "TEXT")):
            if coluna not in tem_i:
                self.con.execute(f"ALTER TABLE itens ADD COLUMN {coluna} {tipo}")

        if novas_curadoria:
            # Item já trabalhado não volta para a fila de curadoria.
            self.con.execute(
                "UPDATE itens SET pronto=1 WHERE estado IN (?,?,?)",
                (CONCLUIDO, FALHOU, SIMULADO))

        self.con.execute(
            "CREATE INDEX IF NOT EXISTS ix_curadoria"
            " ON itens(pronto, estado, ordem_conf, dep_novo, sec_novo, sub_novo)")

        # Uma versão anterior liberava sozinha os de confiança ALTA. A regra
        # mudou: NENHUM produto é gravado sem uma pessoa confirmar, e confiança
        # é só indicativa. Isto devolve à curadoria o que foi liberado sem
        # ninguém olhar — reconhecível por `pronto=1` sem `curado_em`. Roda uma
        # vez; itens já trabalhados e já curados não são tocados.
        marca = self.con.execute(
            "SELECT valor FROM config WHERE chave='migracao_curadoria_total'"
        ).fetchone()
        if not marca:
            cur = self.con.execute(
                "UPDATE itens SET pronto=0 WHERE pronto=1 AND curado_em IS NULL"
                " AND estado IN (?,?)", (LIVRE, SEM_DESTINO))
            self.con.execute(
                "INSERT INTO config (chave, valor, em, por) VALUES (?,?,?,?)",
                ("migracao_curadoria_total", json.dumps(cur.rowcount),
                 time.strftime("%Y-%m-%d %H:%M:%S"), "migracao"))
            if cur.rowcount:
                self._evento("migrou", "", "",
                             f"{cur.rowcount} itens voltaram para a curadoria "
                             "(nada mais e liberado automaticamente)")

    # ── apoio ────────────────────────────────────────────────────────────────
    def _evento(self, acao, cod="", operador="", detalhe="",
                antes=("", "", ""), depois=("", "", "")):
        self.con.execute(
            "INSERT INTO eventos (quando, operador, cod, acao, detalhe,"
            " dep_antes, sec_antes, sub_antes, dep_dep, sec_dep, sub_dep)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (time.time(), operador, cod, acao, str(detalhe)[:500],
             antes[0], antes[1], antes[2], depois[0], depois[1], depois[2]),
        )

    def _expirar_vencidos(self) -> int:
        """Devolve à fila o que ficou reservado além do prazo.

        Roda preguiçosamente, no momento da próxima reserva, para não precisar
        de um timer. Se a máquina do operador travou ou ele fechou o programa no
        meio do bloco, os itens voltam sozinhos.
        """
        agora = time.time()
        vencidos = self.con.execute(
            "SELECT cod, operador FROM itens WHERE estado=? AND expira_em < ?",
            (RESERVADO, agora),
        ).fetchall()
        if not vencidos:
            return 0
        self.con.execute(
            "UPDATE itens SET estado=?, operador=NULL, reservado_em=NULL,"
            " expira_em=NULL WHERE estado=? AND expira_em < ?",
            (LIVRE, RESERVADO, agora),
        )
        for r in vencidos:
            self._evento("expirou", r["cod"], r["operador"] or "",
                         "reserva vencida, item devolvido à fila")
        return len(vencidos)

    @staticmethod
    def _linha_item(r: sqlite3.Row) -> dict:
        return {
            "cod": r["cod"], "linha": r["linha"], "produto": r["produto"],
            "ativo": bool(r["ativo"]), "ano": r["ano"] or "",
            "dep_atual": r["dep_atual"] or "", "sec_atual": r["sec_atual"] or "",
            "sub_atual": r["sub_atual"] or "",
            "dep_novo": r["dep_novo"] or "", "sec_novo": r["sec_novo"] or "",
            "sub_novo": r["sub_novo"] or "",
            "confianca": r["confianca"] or "", "base": r["base"] or "",
            # Acrescentados para a tela de curadoria. O agente ignora o que não
            # conhece, então o contrato de `reservar` continua valendo.
            "estado": r["estado"], "pronto": bool(r["pronto"]),
            "curado_por": r["curado_por"] or "",
        }

    # ── operadores e tokens ──────────────────────────────────────────────────
    def criar_operador(self, nome: str, criado_por: str = "") -> dict:
        """Cadastra um operador e devolve o token dele.

        O token é o que o programa de mesa manda no cabeçalho. Sem ele a fila
        aceitaria qualquer nome digitado, e a garantia de "este produto é meu"
        valeria só no papel.
        """
        nome = (nome or "").strip()
        if not nome:
            raise ValueError("informe o nome do operador")
        with self.lock:
            existe = self.con.execute(
                "SELECT nome FROM operadores WHERE nome=?", (nome,)).fetchone()
            token = secrets.token_urlsafe(24)
            agora = time.strftime("%Y-%m-%d %H:%M:%S")
            if existe:
                self.con.execute(
                    "UPDATE operadores SET token=?, criado_em=?, criado_por=?,"
                    " revogado_em=NULL WHERE nome=?",
                    (token, agora, criado_por, nome))
            else:
                self.con.execute(
                    "INSERT INTO operadores (nome, maquina, visto_em, token,"
                    " criado_em, criado_por) VALUES (?,?,?,?,?,?)",
                    (nome, "", None, token, agora, criado_por))
            self._evento("token", "", nome, f"token gerado por {criado_por}")
            self.con.commit()
            return {"nome": nome, "token": token, "criado_em": agora}

    def revogar_operador(self, nome: str, por: str = "") -> bool:
        with self.lock:
            agora = time.strftime("%Y-%m-%d %H:%M:%S")
            cur = self.con.execute(
                "UPDATE operadores SET token=NULL, revogado_em=? WHERE nome=?",
                (agora, nome))
            if cur.rowcount:
                self._evento("revogou", "", nome, f"token revogado por {por}")
            self.con.commit()
            return bool(cur.rowcount)

    def operador_do_token(self, token: str) -> str | None:
        token = (token or "").strip()
        if not token:
            return None
        with self.lock:
            r = self.con.execute(
                "SELECT nome FROM operadores WHERE token=? AND revogado_em IS NULL",
                (token,)).fetchone()
            return r["nome"] if r else None

    def listar_operadores(self) -> list[dict]:
        with self.lock:
            linhas = [dict(r) for r in self.con.execute(
                "SELECT nome, maquina, visto_em, criado_em, criado_por,"
                " revogado_em, (token IS NOT NULL) AS tem_token,"
                " comando, comando_em, comando_por, bloco, so_ativos, simular,"
                " pular_certos, limiar, agente_estado, agente_msg,"
                " agente_feitos, agente_atual, agente_em,"
                " (SELECT COUNT(*) FROM itens i WHERE i.operador=o.nome"
                "   AND i.estado='reservado') AS reservados,"
                " (SELECT COUNT(*) FROM itens i WHERE i.operador=o.nome"
                "   AND i.estado='concluido') AS concluidos"
                " FROM operadores o ORDER BY o.nome"
            )]
        agora = time.time()
        for o in linhas:
            # "Vivo" é o agente que falou com o servidor há pouco. Sem isso, um
            # programa fechado no meio da rodada continuaria escrito "rodando"
            # no painel para sempre.
            desde = agora - (o["agente_em"] or 0)
            o["vivo"] = bool(o["agente_em"]) and desde < 90
            o["silencio_s"] = int(desde) if o["agente_em"] else None
            if not o["vivo"] and (o["agente_estado"] or "") in ("rodando", "pausado"):
                o["agente_estado"] = "sem contato"
        return linhas

    # ── importação ───────────────────────────────────────────────────────────
    def semear(self, itens, recriar: bool = False) -> dict:
        """Carrega a planilha no banco. Sem recriar, só acrescenta o que falta.

        Não mexe em item que já existe: reimportar a planilha nunca pode apagar
        trabalho já feito.
        """
        with self.lock:
            if recriar:
                self.con.execute("DELETE FROM itens")
                self.con.execute("DELETE FROM eventos")
                self.con.commit()

            existentes = {r[0] for r in self.con.execute("SELECT cod FROM itens")}
            novos = ignorados = sem_destino = curadoria = 0
            for it in itens:
                if it.cod in existentes:
                    ignorados += 1
                    continue
                tem_destino = bool(it.dep_novo and it.sec_novo and it.sub_novo)
                if not tem_destino:
                    sem_destino += 1
                # NADA nasce liberado. Todo produto passa por uma pessoa antes
                # de ser gravado no ERP — inclusive os de confiança ALTA. A
                # confiança é indicativa: ordena a fila e sugere o destino, mas
                # não autoriza gravação (ver docstring do módulo).
                curadoria += 1
                self.con.execute(
                    "INSERT INTO itens (cod, linha, produto, ativo, ano,"
                    " dep_atual, sec_atual, sub_atual, dep_novo, sec_novo,"
                    " sub_novo, confianca, ordem_conf, base, estado, pronto)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it.cod, it.linha, it.produto, int(it.ativo), it.ano,
                     it.dep_atual, it.sec_atual, it.sub_atual,
                     it.dep_novo, it.sec_novo, it.sub_novo,
                     it.confianca, ORDEM_CONF.get(it.confianca, 9), it.base,
                     LIVRE if tem_destino else SEM_DESTINO, 0),
                )
                existentes.add(it.cod)
                novos += 1
            self._evento("importou", "", "", f"{novos} novos, {ignorados} já existiam")
            self.con.commit()
            return {"novos": novos, "ja_existiam": ignorados,
                    "sem_destino": sem_destino, "para_curadoria": curadoria}

    # ── operações da fila ────────────────────────────────────────────────────
    def reservar(self, operador: str, quantidade: int, so_ativos: bool,
                 confiancas: list, lease_s: float = LEASE_PADRAO_S,
                 maquina: str = "") -> dict:
        """Entrega um bloco de itens já marcado como do operador.

        Seleção e marcação acontecem na mesma transação: é isso que impede dois
        operadores de receberem o mesmo produto. Nunca separe em duas chamadas.
        """
        operador = (operador or "").strip()
        if not operador:
            raise ValueError("operador não informado")
        quantidade = max(1, min(int(quantidade), 500))

        with self.lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                self._expirar_vencidos()
                self.con.execute(
                    "UPDATE operadores SET maquina=?, visto_em=? WHERE nome=?",
                    (maquina, time.time(), operador),
                )
                expira = time.time() + lease_s

                # Retomada: o que o operador já tinha em mãos vem antes de
                # qualquer item novo. Sem isso, um programa reiniciado
                # abandonaria o bloco anterior até o prazo vencer.
                pendentes = self.con.execute(
                    "SELECT * FROM itens WHERE operador=? AND estado=?"
                    " ORDER BY ordem_conf, dep_novo, sec_novo, sub_novo, produto",
                    (operador, RESERVADO),
                ).fetchall()
                if pendentes:
                    self.con.execute(
                        "UPDATE itens SET expira_em=? WHERE operador=? AND estado=?",
                        (expira, operador, RESERVADO),
                    )

                # O bloco é completado até a quantidade pedida. Devolver só os
                # pendentes faria o operador girar em falso caso algum item não
                # feche: receberia sempre os mesmos e nunca avançaria.
                faltam = quantidade - len(pendentes)
                escolhidos = []
                # `pronto=1` é a trava nova: o agente só recebe o que já foi
                # decidido (ALTA na importação, ou curado no navegador). Sem
                # ela, um item de confiança MÉDIA cairia numa rodada automática
                # sem ninguém ter olhado o destino.
                # Lista de confianças vazia agora significa TODAS — o agente não
                # escolhe mais nada, quem filtra é o painel.
                confs = [c for c in (confiancas or []) if c in ORDEM_CONF]
                if faltam > 0:
                    filtro_conf = ""
                    if confs:
                        filtro_conf = (" AND confianca IN ("
                                       + ",".join("?" * len(confs)) + ")")
                    sql = (
                        "SELECT * FROM itens WHERE estado=? AND pronto=1"
                        + filtro_conf
                        + (" AND ativo=1" if so_ativos else "")
                        + " ORDER BY ordem_conf, dep_novo, sec_novo, sub_novo,"
                          " produto LIMIT ?"
                    )
                    escolhidos = self.con.execute(
                        sql, [LIVRE, *confs, faltam]
                    ).fetchall()
                    agora = time.time()
                    for r in escolhidos:
                        self.con.execute(
                            "UPDATE itens SET estado=?, operador=?, reservado_em=?,"
                            " expira_em=? WHERE cod=? AND estado=?",
                            (RESERVADO, operador, agora, expira, r["cod"], LIVRE),
                        )

                if escolhidos:
                    self._evento("reservou", "", operador, f"{len(escolhidos)} itens")
                self.con.commit()
                return {
                    "itens": [self._linha_item(r) for r in [*pendentes, *escolhidos]],
                    "expira_em": expira,
                    "retomado": bool(pendentes),
                    "novos": len(escolhidos),
                }
            except Exception:
                self.con.rollback()
                raise

    def renovar(self, operador: str, lease_s: float = LEASE_PADRAO_S) -> dict:
        """Estende o prazo do bloco em mãos. Chamado enquanto o lote roda."""
        with self.lock:
            expira = time.time() + lease_s
            cur = self.con.execute(
                "UPDATE itens SET expira_em=? WHERE operador=? AND estado=?",
                (expira, operador, RESERVADO),
            )
            self.con.execute(
                "UPDATE operadores SET visto_em=? WHERE nome=?",
                (time.time(), operador),
            )
            self.con.commit()
            return {"reservados": cur.rowcount, "expira_em": expira}

    def estado_do_item(self, cod: str, operador: str) -> dict:
        """Reconfirmação imediatamente antes de gravar no ERP.

        Entre receber o bloco e chegar no último item pode ter passado meia
        hora; o prazo pode ter vencido e outra pessoa já ter o produto.
        """
        with self.lock:
            r = self.con.execute(
                "SELECT estado, operador, expira_em FROM itens WHERE cod=?", (cod,)
            ).fetchone()
            if r is None:
                return {"existe": False, "meu": False, "estado": ""}
            meu = (
                r["estado"] == RESERVADO
                and (r["operador"] or "") == operador
                and (r["expira_em"] or 0) > time.time()
            )
            return {"existe": True, "meu": meu, "estado": r["estado"],
                    "operador": r["operador"] or ""}

    def concluir(self, operador: str, cod: str, situacao: str,
                 dep: str = "", sec: str = "", sub: str = "",
                 detalhe: str = "") -> dict:
        """Fecha um item. Só o dono da reserva pode fechar."""
        with self.lock:
            r = self.con.execute("SELECT * FROM itens WHERE cod=?", (cod,)).fetchone()
            if r is None:
                return {"ok": False, "motivo": "código não está no lote"}
            if r["estado"] == CONCLUIDO:
                return {"ok": False, "motivo": "já foi concluído"}
            if r["estado"] == RESERVADO and (r["operador"] or "") != operador:
                return {"ok": False, "motivo": f"reservado por {r['operador']}"}

            estado = ESTADO_DE.get(situacao, FALHOU)
            self.con.execute(
                "UPDATE itens SET estado=?, situacao=?, dep_grav=?, sec_grav=?,"
                " sub_grav=?, detalhe=?, concluido_em=?, operador=?,"
                " expira_em=NULL WHERE cod=?",
                (estado, situacao, dep, sec, sub, str(detalhe)[:500],
                 time.time(), operador, cod),
            )
            self._evento(
                situacao, cod, operador, detalhe,
                antes=(r["dep_atual"] or "", r["sec_atual"] or "",
                       r["sub_atual"] or ""),
                depois=(dep, sec, sub),
            )
            self.con.commit()
            return {"ok": True, "estado": estado}

    def liberar(self, operador: str, cods: list | None = None) -> int:
        """Devolve à fila o que o operador reservou e não usou."""
        with self.lock:
            if cods:
                marcadores = ",".join("?" * len(cods))
                cur = self.con.execute(
                    "UPDATE itens SET estado=?, operador=NULL, reservado_em=NULL,"
                    " expira_em=NULL WHERE operador=? AND estado=?"
                    f" AND cod IN ({marcadores})",
                    [LIVRE, operador, RESERVADO, *cods],
                )
            else:
                cur = self.con.execute(
                    "UPDATE itens SET estado=?, operador=NULL, reservado_em=NULL,"
                    " expira_em=NULL WHERE operador=? AND estado=?",
                    (LIVRE, operador, RESERVADO),
                )
            if cur.rowcount:
                self._evento("liberou", "", operador, f"{cur.rowcount} itens")
            self.con.commit()
            return cur.rowcount

    def devolver_a_fila(self, estado_origem: str, por: str = "") -> int:
        """Ação do painel: manda falhados ou simulados de volta para a fila.

        `concluido` de propósito não entra: é terminal, e reabrir um produto já
        editado é o que a fila existe para impedir.
        """
        if estado_origem not in (FALHOU, SIMULADO):
            raise ValueError("só falhou ou simulado podem voltar à fila")
        with self.lock:
            cur = self.con.execute(
                "UPDATE itens SET estado=?, operador=NULL, situacao=NULL,"
                " expira_em=NULL WHERE estado=?",
                (LIVRE, estado_origem),
            )
            self._evento("devolveu", "", por or "painel",
                         f"{cur.rowcount} itens de {estado_origem}")
            self.con.commit()
            return cur.rowcount

    # ── curadoria (a decisão humana, no navegador) ───────────────────────────
    # Antes, o operador decidia produto a produto DURANTE a gravação, com o ERP
    # travado esperando. Agora ele decide antes, aqui, e a rodada no ERP não
    # pergunta nada. `pronto=1` é a fronteira entre os dois mundos.

    def _filtro_curadoria(self, confianca="", dep="", busca="",
                          incluir_sem_destino=True, so_ativos=False):
        onde = ["pronto=0", "estado IN ({})".format(
            ",".join("?" * (2 if incluir_sem_destino else 1)))]
        params: list = [LIVRE] + ([SEM_DESTINO] if incluir_sem_destino else [])
        if confianca:
            onde.append("confianca=?")
            params.append(confianca)
        if dep:
            onde.append("dep_novo=?")
            params.append(str(dep))
        if busca:
            onde.append("(produto LIKE ? OR cod LIKE ?)")
            params += [f"%{busca}%", f"%{busca}%"]
        if so_ativos:
            # Dois terços da fila são produtos inativos, e o agente nunca os
            # pede (o padrão da rodada é "só ativos"). Curá-los é trabalho que
            # não desbloqueia nada, então a tela começa escondendo-os.
            onde.append("ativo=1")
        return " AND ".join(onde), params

    def fila_curadoria(self, limite: int = 60, confianca: str = "",
                       dep: str = "", busca: str = "",
                       incluir_sem_destino: bool = True,
                       so_ativos: bool = False) -> dict:
        """Próximos itens que esperam decisão humana.

        Sai na MESMA ordem da fila de execução (confiança, depois destino), e é
        de propósito: o lote vem agrupado por destino — num bloco de 100 medido,
        os 68 primeiros iam para o mesmo lugar. Com a lista agrupada, o curador
        aceita dezenas de uma vez em lugar de uma por uma.
        """
        limite = max(1, min(int(limite), 300))
        onde, params = self._filtro_curadoria(confianca, dep, busca,
                                              incluir_sem_destino, so_ativos)
        with self.lock:
            linhas = self.con.execute(
                f"SELECT * FROM itens WHERE {onde}"
                " ORDER BY ordem_conf, dep_novo, sec_novo, sub_novo, produto"
                " LIMIT ?", [*params, limite]).fetchall()
            total = self.con.execute(
                f"SELECT COUNT(*) FROM itens WHERE {onde}", params).fetchone()[0]
        itens = [self._linha_item(r) for r in linhas]

        # Duas contagens por destino, e elas querem dizer coisas diferentes:
        # `iguais_a_seguir` é o que dá para marcar NESTA página; `no_destino` é
        # o tamanho real do grupo no lote inteiro. O maior bloco tem 1.216 itens
        # e nenhuma página cabe isso — sem a contagem do servidor, liberar um
        # destino grande viraria paginação a perder de vista.
        with self.lock:
            totais = {
                (r[0] or "", r[1] or "", r[2] or ""): r[3]
                for r in self.con.execute(
                    f"SELECT dep_novo, sec_novo, sub_novo, COUNT(*)"
                    f" FROM itens WHERE {onde} GROUP BY 1,2,3", params)
            }
        for i, it in enumerate(itens):
            destino = (it["dep_novo"], it["sec_novo"], it["sub_novo"])
            n = 0
            if all(destino):
                for outro in itens[i:]:
                    if (outro["dep_novo"], outro["sec_novo"],
                            outro["sub_novo"]) != destino:
                        break
                    n += 1
            it["iguais_a_seguir"] = n
            it["no_destino"] = totais.get(destino, 0) if all(destino) else 0
        return {"itens": itens, "total": total, "mostrando": len(itens)}

    def aceitar_destino(self, dep: str, sec: str, sub: str, por: str = "",
                        confianca: str = "", so_ativos: bool = False) -> dict:
        """Confirma o destino sugerido de TODOS os pendentes daquele destino.

        Complementa `aceitar_sugestao`, que só alcança o que está na tela. Os
        grupos grandes (1.216 no maior) são a maior parte do lote: 10 destinos
        cobrem 46% dos ativos com sugestão. Sem esta porta, confirmar o lote
        seria rolar página atrás de página.
        """
        from scripts.reclassificacao_estrutura import estrutura

        dep, sec, sub = str(dep).strip(), str(sec).strip(), str(sub).strip()
        if not (dep and sec and sub):
            return {"ok": False, "erro": "destino incompleto"}
        ok, motivo = estrutura().validar(dep, sec, sub)
        if not ok:
            return {"ok": False, "erro": motivo}

        with self.lock:
            sql = ("SELECT cod FROM itens WHERE pronto=0 AND estado=?"
                   " AND dep_novo=? AND sec_novo=? AND sub_novo=?"
                   + (" AND ativo=1" if so_ativos else "")
                   + (" AND confianca=?" if confianca else ""))
            p = [LIVRE, dep, sec, sub] + ([confianca] if confianca else [])
            cods = [r[0] for r in self.con.execute(sql, p)]
        if not cods:
            return {"ok": True, "aplicados": 0, "ja_curados": 0}
        return self._aplicar_curadoria(cods, dep, sec, sub, por, "",
                                       "aceitou_destino")

    def resumo_curadoria(self) -> dict:
        with self.lock:
            por_conf = dict(self.con.execute(
                "SELECT confianca, COUNT(*) FROM itens WHERE pronto=0"
                " AND estado IN (?,?) GROUP BY confianca",
                (LIVRE, SEM_DESTINO)).fetchall())
            por_dep = [
                {"dep": r[0] or "", "n": r[1]} for r in self.con.execute(
                    "SELECT dep_novo, COUNT(*) FROM itens WHERE pronto=0"
                    " AND estado=? GROUP BY dep_novo ORDER BY 2 DESC",
                    (LIVRE,)).fetchall()]
            pendentes = self.con.execute(
                "SELECT COUNT(*) FROM itens WHERE pronto=0 AND estado IN (?,?)",
                (LIVRE, SEM_DESTINO)).fetchone()[0]
            pendentes_ativos = self.con.execute(
                "SELECT COUNT(*) FROM itens WHERE pronto=0 AND ativo=1"
                " AND estado IN (?,?)", (LIVRE, SEM_DESTINO)).fetchone()[0]
            por_conf_ativos = dict(self.con.execute(
                "SELECT confianca, COUNT(*) FROM itens WHERE pronto=0 AND ativo=1"
                " AND estado IN (?,?) GROUP BY confianca",
                (LIVRE, SEM_DESTINO)).fetchall())
            curados = self.con.execute(
                "SELECT COUNT(*) FROM itens WHERE curado_em IS NOT NULL"
            ).fetchone()[0]
            descartados = self.con.execute(
                "SELECT COUNT(*) FROM itens WHERE estado=?",
                (DESCARTADO,)).fetchone()[0]
        return {"pendentes": pendentes, "pendentes_ativos": pendentes_ativos,
                "por_confianca": por_conf,
                "por_confianca_ativos": por_conf_ativos,
                "por_departamento": por_dep, "curados": curados,
                "descartados": descartados}

    def _aplicar_curadoria(self, cods, dep, sec, sub, por, nota,
                           acao) -> dict:
        """Grava o destino e libera para o agente. Só mexe em item pendente.

        Quem já foi curado por outra pessoa entre a tela ter sido carregada e o
        clique não é sobrescrito — volta no contador `ja_curados`. Dois
        curadores na mesma lista se atrapalham, mas nunca se apagam.
        """
        aplicados = ja = 0
        agora = time.time()
        with self.lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                for cod in cods:
                    r = self.con.execute(
                        "SELECT estado, pronto FROM itens WHERE cod=?",
                        (cod,)).fetchone()
                    if r is None:
                        continue
                    if r["pronto"] or r["estado"] not in (LIVRE, SEM_DESTINO):
                        ja += 1
                        continue
                    self.con.execute(
                        "UPDATE itens SET dep_novo=?, sec_novo=?, sub_novo=?,"
                        " estado=?, pronto=1, curado_em=?, curado_por=?,"
                        " curado_nota=? WHERE cod=?",
                        (dep, sec, sub, LIVRE, agora, por, nota or None, cod))
                    aplicados += 1
                if aplicados:
                    self._evento(acao, "", por, f"{aplicados} itens -> {dep}/{sec}/{sub}",
                                 depois=(dep, sec, sub))
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise
        return {"ok": True, "aplicados": aplicados, "ja_curados": ja}

    def curar(self, cods: list, dep: str, sec: str, sub: str,
              por: str = "", nota: str = "") -> dict:
        """Define o destino de um ou vários produtos e libera para execução.

        O trio é validado contra a estrutura merceológica AQUI, no servidor: os
        códigos se repetem entre níveis e um trio na ordem errada gera cadastro
        que o ERP aceita e que está semanticamente errado. O `<select>` da tela
        ajuda, mas não é ele quem garante.
        """
        from scripts.reclassificacao_estrutura import estrutura

        cods = [c for c in (cods or []) if c]
        if not cods:
            return {"ok": False, "erro": "nenhum produto selecionado"}
        dep, sec, sub = str(dep).strip(), str(sec).strip(), str(sub).strip()
        ok, motivo = estrutura().validar(dep, sec, sub)
        if not ok:
            return {"ok": False, "erro": motivo}
        return self._aplicar_curadoria(cods, dep, sec, sub, por, nota, "curou")

    def aceitar_sugestao(self, cods: list, por: str = "") -> dict:
        """Confirma o destino que a planilha já sugeriu, em lote.

        Ainda passa pela validação: uma planilha futura pode trazer trio que não
        existe na estrutura, e aceitar em lote é justamente onde isso passaria
        despercebido. O que não valida volta em `recusados`, com o motivo.
        """
        from scripts.reclassificacao_estrutura import estrutura

        cods = [c for c in (cods or []) if c]
        if not cods:
            return {"ok": False, "erro": "nenhum produto selecionado"}
        est = estrutura()
        grupos: dict[tuple, list] = {}
        recusados = []
        with self.lock:
            for cod in cods:
                r = self.con.execute(
                    "SELECT dep_novo, sec_novo, sub_novo FROM itens WHERE cod=?",
                    (cod,)).fetchone()
                if r is None:
                    continue
                trio = (r["dep_novo"] or "", r["sec_novo"] or "",
                        r["sub_novo"] or "")
                if not all(trio):
                    recusados.append({"cod": cod, "motivo": "sem destino sugerido"})
                    continue
                grupos.setdefault(trio, []).append(cod)

        aplicados = ja = 0
        for (dep, sec, sub), lista in grupos.items():
            ok, motivo = est.validar(dep, sec, sub)
            if not ok:
                recusados += [{"cod": c, "motivo": motivo} for c in lista]
                continue
            res = self._aplicar_curadoria(lista, dep, sec, sub, por, "", "aceitou")
            aplicados += res["aplicados"]
            ja += res["ja_curados"]
        return {"ok": True, "aplicados": aplicados, "ja_curados": ja,
                "recusados": recusados}

    def descurar(self, cods: list, por: str = "") -> dict:
        """Desfaz uma confirmação, enquanto ela ainda não virou trabalho.

        Existe porque a conferência é dirigida por uma tecla só: com 27 mil
        produtos, um Enter a mais é questão de tempo, e sem desfazer o curador
        aprenderia a hesitar — que é justamente o que torna o fluxo lento.

        Só desfaz o que ainda está `livre`. Assim que um agente reserva ou fecha
        o produto, a janela fecha: `concluido` é terminal e voltar atrás no
        banco não desfaz o que já foi escrito no ERP.
        """
        cods = [c for c in (cods or []) if c]
        if not cods:
            return {"ok": False, "erro": "nada para desfazer"}
        desfeitos, tarde = 0, []
        with self.lock:
            for cod in cods:
                r = self.con.execute(
                    "SELECT estado, pronto FROM itens WHERE cod=?", (cod,)
                ).fetchone()
                if r is None or not r["pronto"]:
                    continue
                if r["estado"] != LIVRE:
                    tarde.append({"cod": cod, "estado": r["estado"]})
                    continue
                self.con.execute(
                    "UPDATE itens SET pronto=0, curado_em=NULL, curado_por=NULL,"
                    " curado_nota=NULL WHERE cod=? AND estado=? AND pronto=1",
                    (cod, LIVRE))
                desfeitos += 1
            if desfeitos:
                self._evento("desfez", "", por, f"{desfeitos} confirmações desfeitas")
            self.con.commit()
        return {"ok": True, "desfeitos": desfeitos, "tarde_demais": tarde}

    def descartar(self, cods: list, por: str = "", motivo: str = "") -> dict:
        """Tira produtos do lote sem editá-los no ERP.

        Existe para os REVISAR que, olhados de perto, não devem mesmo ser
        reclassificados. Sai da fila de curadoria e da fila de execução, mas
        continua no banco com o motivo — some da vista, não da história.
        """
        cods = [c for c in (cods or []) if c]
        if not cods:
            return {"ok": False, "erro": "nenhum produto selecionado"}
        n = 0
        with self.lock:
            for cod in cods:
                cur = self.con.execute(
                    "UPDATE itens SET estado=?, pronto=0, curado_em=?,"
                    " curado_por=?, curado_nota=? WHERE cod=? AND estado IN (?,?)",
                    (DESCARTADO, time.time(), por, motivo or None, cod,
                     LIVRE, SEM_DESTINO))
                n += cur.rowcount
            if n:
                self._evento("descartou", "", por, f"{n} itens: {motivo}")
            self.con.commit()
        return {"ok": True, "descartados": n}

    def reverter_descarte(self, por: str = "") -> int:
        """Devolve todos os descartados à fila de curadoria."""
        with self.lock:
            cur = self.con.execute(
                "UPDATE itens SET estado=CASE WHEN dep_novo<>'' AND sec_novo<>''"
                " AND sub_novo<>'' THEN ? ELSE ? END, pronto=0, curado_em=NULL,"
                " curado_por=NULL, curado_nota=NULL WHERE estado=?",
                (LIVRE, SEM_DESTINO, DESCARTADO))
            if cur.rowcount:
                self._evento("devolveu", "", por or "painel",
                             f"{cur.rowcount} descartados voltaram à curadoria")
            self.con.commit()
            return cur.rowcount

    # ── controle remoto do agente ────────────────────────────────────────────
    # O programa de mesa não tem tela: ele pergunta o que fazer e obedece.
    # Ligar, pausar, parar e configurar acontecem no navegador.

    def definir_comando(self, operador: str, comando: str, por: str = "") -> dict:
        if comando not in COMANDOS:
            raise ValueError(f"comando inválido: {comando}")
        with self.lock:
            cur = self.con.execute(
                "UPDATE operadores SET comando=?, comando_em=?, comando_por=?"
                " WHERE nome=?", (comando, time.time(), por, operador))
            if not cur.rowcount:
                raise ValueError(f"operador {operador} não existe")
            self._evento("comando", "", operador, f"{comando} (por {por})")
            self.con.commit()
        return {"ok": True, "operador": operador, "comando": comando}

    def definir_parametros(self, operador: str, por: str = "", **p) -> dict:
        """Parâmetros da rodada daquela máquina. Só o painel escreve aqui."""
        campos = {
            "bloco": lambda v: max(1, min(int(v), 500)),
            "so_ativos": lambda v: int(bool(v)),
            "simular": lambda v: int(bool(v)),
            "pular_certos": lambda v: int(bool(v)),
            "limiar": lambda v: max(0.0, min(float(v), 1.0)),
        }
        sets, vals = [], []
        for chave, converte in campos.items():
            if chave in p and p[chave] is not None and p[chave] != "":
                sets.append(f"{chave}=?")
                vals.append(converte(p[chave]))
        if not sets:
            return {"ok": True, "alterados": 0}
        with self.lock:
            cur = self.con.execute(
                f"UPDATE operadores SET {', '.join(sets)} WHERE nome=?",
                [*vals, operador])
            if not cur.rowcount:
                raise ValueError(f"operador {operador} não existe")
            self._evento("parametros", "", operador,
                         ", ".join(f"{k}={v}" for k, v in zip(
                             [s[:-2] for s in sets], vals)) + f" (por {por})")
            self.con.commit()
        return {"ok": True, "alterados": len(sets)}

    def config_do_agente(self, operador: str) -> dict:
        """Tudo o que o agente precisa saber para a próxima volta do laço.

        Inclui o MAPA DO ERP: instalar uma máquina nova passa a ser colar o
        token, e recalibrar o RADGe depois de uma atualização é editar um campo
        no painel em vez de mexer no config.json de cada PC.
        """
        with self.lock:
            r = self.con.execute(
                "SELECT comando, bloco, so_ativos, simular, pular_certos, limiar"
                " FROM operadores WHERE nome=?", (operador,)).fetchone()
            mapa = self._config_bruta("mapa_erp")
        if r is None:
            return {"comando": PARAR, "erro": "operador não existe"}
        return {
            "comando": r["comando"] or PARAR,
            "bloco": r["bloco"], "so_ativos": bool(r["so_ativos"]),
            "simular": bool(r["simular"]),
            "pular_certos": bool(r["pular_certos"]),
            "limiar": r["limiar"],
            "lease_s": LEASE_PADRAO_S,
            "batimento_s": max(30, LEASE_PADRAO_S // 6),
            "mapa_erp": mapa,
        }

    def reportar_status(self, operador: str, estado: str = "", msg: str = "",
                        feitos: int = 0, atual: str = "", maquina: str = "",
                        log: list | None = None,
                        pedir_parada: bool = False) -> dict:
        """O agente conta o que está fazendo; o painel mostra.

        É a única janela que o coordenador tem para dentro da máquina do
        operador, já que lá não há mais nada para olhar.

        `pedir_parada` é a **única** ordem que anda no sentido contrário: o
        operador segurou ESC na máquina e abortou o lote. Sem isso o painel
        continuaria dizendo `rodar`, e o agente pegaria outro bloco na volta
        seguinte — a tecla de pânico não pararia nada por mais de um segundo.
        Quem está na frente do ERP vendo dar errado tem de poder parar.
        """
        agora = time.time()
        with self.lock:
            if pedir_parada:
                self.con.execute(
                    "UPDATE operadores SET comando=?, comando_em=?, comando_por=?"
                    " WHERE nome=?", (PARAR, agora, f"{operador} (ESC)", operador))
                self._evento("comando", "", operador,
                             "parar (ESC segurado na maquina)")
            self.con.execute(
                "UPDATE operadores SET agente_estado=?, agente_msg=?,"
                " agente_feitos=?, agente_atual=?, agente_em=?, visto_em=?"
                + (", maquina=?" if maquina else "") + " WHERE nome=?",
                [str(estado)[:40], str(msg)[:300], int(feitos or 0),
                 str(atual)[:40], agora, agora]
                + ([maquina] if maquina else []) + [operador])
            for linha in (log or [])[:50]:
                self.con.execute(
                    "INSERT INTO agente_log (quando, operador, texto)"
                    " VALUES (?,?,?)", (agora, operador, str(linha)[:400]))
            if log:
                self.con.execute(
                    "DELETE FROM agente_log WHERE operador=? AND id NOT IN"
                    " (SELECT id FROM agente_log WHERE operador=?"
                    "  ORDER BY id DESC LIMIT ?)",
                    (operador, operador, APARA_LOG))
            self.con.commit()
        # A resposta já traz o comando: um POST por volta do laço basta para o
        # agente reportar e receber ordem nova, sem uma segunda ida ao servidor.
        return {"ok": True, **self.config_do_agente(operador)}

    def log_do_agente(self, operador: str = "", limite: int = 80) -> list:
        limite = max(1, min(int(limite), 400))
        with self.lock:
            if operador:
                linhas = self.con.execute(
                    "SELECT quando, operador, texto FROM agente_log"
                    " WHERE operador=? ORDER BY id DESC LIMIT ?",
                    (operador, limite)).fetchall()
            else:
                linhas = self.con.execute(
                    "SELECT quando, operador, texto FROM agente_log"
                    " ORDER BY id DESC LIMIT ?", (limite,)).fetchall()
        return [dict(r) for r in linhas]

    # ── configuração global (inclui o mapa do ERP) ───────────────────────────
    def _config_bruta(self, chave: str):
        r = self.con.execute("SELECT valor FROM config WHERE chave=?",
                             (chave,)).fetchone()
        if not r or not r["valor"]:
            return None
        try:
            return json.loads(r["valor"])
        except (ValueError, TypeError):
            return None

    def ler_config(self, chave: str):
        with self.lock:
            return self._config_bruta(chave)

    def gravar_config(self, chave: str, valor, por: str = "") -> dict:
        """Valor é serializado em JSON. Texto vazio apaga a chave."""
        with self.lock:
            if valor in (None, "", {}, []):
                self.con.execute("DELETE FROM config WHERE chave=?", (chave,))
            else:
                self.con.execute(
                    "INSERT INTO config (chave, valor, em, por) VALUES (?,?,?,?)"
                    " ON CONFLICT(chave) DO UPDATE SET valor=excluded.valor,"
                    " em=excluded.em, por=excluded.por",
                    (chave, json.dumps(valor, ensure_ascii=False),
                     time.strftime("%Y-%m-%d %H:%M:%S"), por))
            self._evento("config", "", por, chave)
            self.con.commit()
        return {"ok": True, "chave": chave}

    # ── consultas ────────────────────────────────────────────────────────────
    def resumo(self) -> dict:
        with self.lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                self._expirar_vencidos()
                self.con.commit()
            except Exception:
                self.con.rollback()

            por_estado = dict(self.con.execute(
                "SELECT estado, COUNT(*) FROM itens GROUP BY estado"
            ).fetchall())
            por_situacao = dict(self.con.execute(
                "SELECT situacao, COUNT(*) FROM itens WHERE situacao IS NOT NULL"
                " GROUP BY situacao"
            ).fetchall())
            total = sum(por_estado.values())
            livres_ativos = self.con.execute(
                "SELECT COUNT(*) FROM itens WHERE estado='livre' AND ativo=1"
            ).fetchone()[0]
            # A fila que o agente enxerga agora é só a parte já decidida.
            prontos = self.con.execute(
                "SELECT COUNT(*) FROM itens WHERE estado=? AND pronto=1",
                (LIVRE,)).fetchone()[0]
            prontos_ativos = self.con.execute(
                "SELECT COUNT(*) FROM itens WHERE estado=? AND pronto=1"
                " AND ativo=1", (LIVRE,)).fetchone()[0]
            aguardando = self.con.execute(
                "SELECT COUNT(*) FROM itens WHERE pronto=0 AND estado IN (?,?)",
                (LIVRE, SEM_DESTINO)).fetchone()[0]
            no_lote = total - por_estado.get(SEM_DESTINO, 0) \
                - por_estado.get(DESCARTADO, 0)
            return {
                "total": total,
                "no_lote": no_lote,
                "por_estado": por_estado, "por_situacao": por_situacao,
                "operadores": self.listar_operadores(),
                "livres_ativos": livres_ativos,
                "prontos": prontos, "prontos_ativos": prontos_ativos,
                "aguardando_curadoria": aguardando,
                "agora": time.time(),
            }

    def eventos_recentes(self, limite: int = 40) -> list:
        with self.lock:
            return [dict(r) for r in self.con.execute(
                "SELECT quando, operador, cod, acao, detalhe FROM eventos"
                " ORDER BY id DESC LIMIT ?", (limite,)
            )]

    def todos(self) -> list:
        """Uma tupla por produto trabalhado, na ordem em que foram fechados."""
        with self.lock:
            saida = []
            for r in self.con.execute(
                "SELECT cod, situacao, dep_grav, sec_grav, sub_grav, detalhe,"
                " concluido_em, operador FROM itens WHERE situacao IS NOT NULL"
                " ORDER BY concluido_em"
            ):
                quando = time.strftime("%Y-%m-%d %H:%M:%S",
                                       time.localtime(r["concluido_em"] or 0))
                saida.append((r["cod"], r["situacao"], r["dep_grav"] or "",
                              r["sec_grav"] or "", r["sub_grav"] or "",
                              r["detalhe"] or "", quando, r["operador"] or ""))
            return saida

    def fechar(self):
        with self.lock:
            try:
                self.con.close()
            except Exception:
                pass


# ── instância única do processo ───────────────────────────────────────────────
# Único escritor: uma conexão só, criada na primeira chamada. Não abra o .db em
# outro lugar (nem por pasta de rede) — o travamento do SQLite sobre SMB é
# pouco confiável e o modo WAL nem funciona em rede.
_banco: FilaBanco | None = None
_criacao = threading.Lock()


def banco() -> FilaBanco:
    global _banco
    if _banco is None:
        with _criacao:
            if _banco is None:
                _banco = FilaBanco()
    return _banco


# ── exportação do resultado ───────────────────────────────────────────────────
def exportar_resultado(destino: str | Path) -> Path:
    """Grava um xlsx com o que aconteceu em cada produto do lote."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "Resultado"
    cab = ["Cód. Barras", "Situação", "CodGrp1", "CodGrp2", "CodGrp3",
           "Detalhe", "Quando", "Operador"]
    for i, h in enumerate(cab, 1):
        c = ws.cell(1, i, h)
        c.font = Font("Arial", bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="404040")
        c.alignment = Alignment(horizontal="center")

    cores = {"alterado": "C6EFCE", "ja_correto": "DDEBF7",
             "pulado": "FFEB9C", "erro": "F8CBAD", "simulado": "E4DFEC"}
    for r, linha in enumerate(banco().todos(), start=2):
        for i, v in enumerate(linha, 1):
            cel = ws.cell(r, i, v)
            cel.font = Font("Arial", size=9)
            if i in (1, 3, 4, 5):
                cel.number_format = "@"      # código de barras é texto
        cor = cores.get(linha[1])
        if cor:
            ws.cell(r, 2).fill = PatternFill("solid", fgColor=cor)

    for col, w in zip("ABCDEFGH", [16, 12, 10, 10, 10, 60, 20, 16]):
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:H{ws.max_row}"
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    wb.save(destino)
    return destino
