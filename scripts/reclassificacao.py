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

A atomicidade da reserva é a garantia inteira do sistema: selecionar e marcar
acontecem na MESMA transação. Separar em "buscar livres" e depois "marcar"
reabre a corrida que a fila existe para fechar. Não refatore isso.

ATENÇÃO AO DEPLOY: o lock abaixo serializa dentro de UM processo. O waitress
roda em processo único com threads, então funciona. Se um dia o sistema passar a
vários processos worker, a atomicidade cai e a fila precisa de outra trava.

Estados do item:

    livre -> reservado -> concluido    (terminal, nunca volta)
                       -> falhou       (pilha à parte, não volta sozinho)
                       -> simulado     (não contou como feito)
    reservado -> livre                 (quando o prazo vence)
"""
from __future__ import annotations

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
    concluido_em REAL
);
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
    revogado_em TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_token ON operadores(token);
"""


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
        for coluna, tipo in (("token", "TEXT"), ("criado_em", "TEXT"),
                             ("criado_por", "TEXT"), ("revogado_em", "TEXT")):
            if coluna not in tem:
                self.con.execute(
                    f"ALTER TABLE operadores ADD COLUMN {coluna} {tipo}")

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
            return [dict(r) for r in self.con.execute(
                "SELECT nome, maquina, visto_em, criado_em, criado_por,"
                " revogado_em, (token IS NOT NULL) AS tem_token,"
                " (SELECT COUNT(*) FROM itens i WHERE i.operador=o.nome"
                "   AND i.estado='reservado') AS reservados,"
                " (SELECT COUNT(*) FROM itens i WHERE i.operador=o.nome"
                "   AND i.estado='concluido') AS concluidos"
                " FROM operadores o ORDER BY o.nome"
            )]

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
            novos = ignorados = sem_destino = 0
            for it in itens:
                if it.cod in existentes:
                    ignorados += 1
                    continue
                tem_destino = bool(it.dep_novo and it.sec_novo and it.sub_novo)
                if not tem_destino:
                    sem_destino += 1
                self.con.execute(
                    "INSERT INTO itens (cod, linha, produto, ativo, ano,"
                    " dep_atual, sec_atual, sub_atual, dep_novo, sec_novo,"
                    " sub_novo, confianca, ordem_conf, base, estado)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it.cod, it.linha, it.produto, int(it.ativo), it.ano,
                     it.dep_atual, it.sec_atual, it.sub_atual,
                     it.dep_novo, it.sec_novo, it.sub_novo,
                     it.confianca, ORDEM_CONF.get(it.confianca, 9), it.base,
                     LIVRE if tem_destino else SEM_DESTINO),
                )
                existentes.add(it.cod)
                novos += 1
            self._evento("importou", "", "", f"{novos} novos, {ignorados} já existiam")
            self.con.commit()
            return {"novos": novos, "ja_existiam": ignorados,
                    "sem_destino": sem_destino}

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
                confs = [c for c in (confiancas or []) if c in ORDEM_CONF]
                if faltam > 0 and confs:
                    marcadores = ",".join("?" * len(confs))
                    sql = (
                        "SELECT * FROM itens WHERE estado=?"
                        f" AND confianca IN ({marcadores})"
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
            return {
                "total": total,
                "no_lote": total - por_estado.get(SEM_DESTINO, 0),
                "por_estado": por_estado, "por_situacao": por_situacao,
                "operadores": self.listar_operadores(),
                "livres_ativos": livres_ativos,
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
