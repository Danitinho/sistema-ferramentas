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

# ── Painel de andamento ──────────────────────────────────────────────────────
# "Dia" é o dia de São Paulo. O Python do Windows não traz a base de fusos
# (falta o pacote tzdata), e o ZoneInfo falha; cai para UTC-3 fixo, que é o
# mesmo desde que o Brasil aboliu o horário de verão (2019).
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    FUSO = _ZoneInfo("America/Sao_Paulo")
except Exception:  # noqa: BLE001
    from datetime import timedelta as _td, timezone as _tz
    FUSO = _tz(_td(hours=-3), "America/Sao_Paulo")

TEMPO_DECISAO_S = 13        # ~tempo de uma decisão na conferência (estimativa)
INTERVALO_MAX_S = 60        # intervalo entre gravações acima disto = pausa, fora da mediana
DIAS_RITMO = 10             # dias úteis do gráfico de ritmo
DIAS_MEDIA = 7              # dias úteis da média da curadoria

# Produto ATIVO = VENDEU (qtd > 0) deste mês em diante, pelo banco de vendas do
# módulo de relatórios (Curva ABC). Decisão do usuário em 30/10/2026, depois de
# medir que a "Últ. mov." da planilha não é a última venda: 2.048 itens com
# "2025" venderam entre jan e jul/2026, e 437 com "2026" não venderam nada.
# A regra é reaplicada sozinha quando muda o critério ou o banco de vendas
# (mês novo processado) — ver `_aplicar_ativo`.
VENDA_DESDE = "2026-01"

# Caminho do banco de vendas montado aqui, sem importar `relatorios_vendas`:
# aquele módulo puxa o leitor de PDF (pdfplumber), e a reclassificação não pode
# cair por falta dele. Mesmo arquivo de `relatorios_vendas.BANCO`.
VENDAS_DB = RAIZ / "dados" / "relatorios" / "vendas.db"


def _chave_cb(cb) -> str:
    """Código sem zeros à esquerda. O mesmo produto aparece como `42360407` no
    relatório de vendas e `0000042360407` na planilha (EAN-8 completado). Aqui
    o erro barato é o falso positivo — um item a mais na curadoria —, por isso
    não se exige casamento exato como no catálogo, onde o zero separa preços."""
    return str(cb or "").strip().lstrip("0") or "0"


def codigos_com_venda(desde: str = VENDA_DESDE):
    """(conjunto de códigos que venderam desde `desde`, assinatura do banco).

    Devolve (None, None) se o banco de vendas não existe ou não tem nenhum mês
    no período — e aí ninguém mexe em `ativo`: zerar todos os ativos por falta
    de dado esconderia a curadoria inteira."""
    import sqlite3 as _sq
    if not VENDAS_DB.exists():
        return None, None
    con = _sq.connect(f"file:{VENDAS_DB.as_posix()}?mode=ro", uri=True)
    try:
        ultimo = con.execute(
            "SELECT MAX(mes) FROM vendas WHERE mes >= ?", (desde,)).fetchone()[0]
        if not ultimo:
            return None, None
        cods = {_chave_cb(r[0]) for r in con.execute(
            "SELECT DISTINCT codigo_barras FROM vendas WHERE mes >= ? AND qtd > 0",
            (desde,)) if str(r[0] or "").strip()}
    finally:
        con.close()
    return cods, f"venda:{desde}:{ultimo}:{len(cods)}"

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
            ativo=False, ano=g("ano"),   # `ativo` vem das vendas (semear recalcula)
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
                             ("curado_nota", "TEXT"),
                             ("sug_dep", "TEXT"), ("sug_sec", "TEXT"),
                             ("sug_sub", "TEXT"), ("sug_conf", "TEXT"),
                             ("sug_base", "TEXT"),
                             ("rev_em", "REAL"), ("rev_por", "TEXT"),
                             # 'aceitou' = ficou com a sugestão; 'corrigiu' =
                             # escolheu outro destino. Só a partir de 30/10/2026:
                             # a sugestão de antes é sobrescrita ao curar, então
                             # não há como saber isso para as decisões antigas.
                             ("curado_acao", "TEXT")):
            if coluna not in tem_i:
                self.con.execute(f"ALTER TABLE itens ADD COLUMN {coluna} {tipo}")

        if "sug_dep" not in tem_i:
            # A sugestão ORIGINAL da planilha passa a ser guardada à parte:
            # `dep_novo` é sobrescrito quando alguém escolhe outro destino ou
            # quando a revisão troca o palpite, e assim se perdeu qual era a
            # sugestão dos 467 itens corrigidos à mão até aqui. Só dá para
            # recuperar a dos que ninguém decidiu ainda.
            self.con.execute(
                "UPDATE itens SET sug_dep=dep_novo, sug_sec=sec_novo,"
                " sug_sub=sub_novo, sug_conf=confianca, sug_base=base"
                " WHERE curado_em IS NULL")

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

        self._aplicar_ativo()

    def _aplicar_ativo(self, forcar: bool = False) -> dict | None:
        """Recalcula `ativo` = vendeu desde `VENDA_DESDE` (banco de vendas).

        Só roda quando a assinatura (critério + último mês + quantos códigos
        venderam) difere da gravada em `config.ativo_criterio` — ao subir o
        sistema, ao importar a planilha e depois de processar PDFs de venda.
        Só mexe na coluna `ativo`: estado, curadoria e reservas ficam como
        estão. Sem banco de vendas, não mexe em nada.
        """
        cods, assinatura = codigos_com_venda()
        if cods is None:
            return None
        marca = self.con.execute(
            "SELECT valor FROM config WHERE chave='ativo_criterio'").fetchone()
        if not forcar and marca and json.loads(marca[0]) == assinatura:
            return None
        mudar = [(1 if _chave_cb(cod) in cods else 0, cod, 1 if _chave_cb(cod) in cods else 0)
                 for (cod,) in self.con.execute("SELECT cod FROM itens")]
        self.con.executemany(
            "UPDATE itens SET ativo=? WHERE cod=? AND COALESCE(ativo,-1)<>?", mudar)
        self.con.execute(
            "INSERT OR REPLACE INTO config (chave, valor, em, por)"
            " VALUES (?,?,?,?)",
            ("ativo_criterio", json.dumps(assinatura),
             time.strftime("%Y-%m-%d %H:%M:%S"), "migracao"))
        ativos = self.con.execute(
            "SELECT COUNT(*) FROM itens WHERE ativo=1").fetchone()[0]
        total = len(mudar)
        self._evento("migrou", "", "",
                     f"ativo = vendeu desde {VENDA_DESDE} ({assinatura}):"
                     f" {ativos} ativos de {total}")
        return {"ativos": ativos, "total": total, "assinatura": assinatura}

    def recalcular_ativos(self) -> dict | None:
        """Porta pública: o módulo de relatórios chama depois de processar PDFs,
        para um mês novo de vendas valer na curadoria sem reiniciar o sistema."""
        with self.lock:
            r = self._aplicar_ativo()
            self.con.commit()
        return r

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
            "revisado": bool(r["rev_em"]) if "rev_em" in r.keys() else False,
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

    def garantir_operador(self, nome: str) -> None:
        """Operador de uma MÁQUINA pareada (token do /agente). Nasce com o
        comando `parar` e os parâmetros padrão: aprovar a máquina dá acesso,
        não põe ninguém para rodar."""
        with self.lock:
            cur = self.con.execute(
                "INSERT OR IGNORE INTO operadores (nome, maquina, visto_em, criado_em,"
                " criado_por) VALUES (?,?,?,?,?)",
                (nome, nome, None, time.strftime("%Y-%m-%d %H:%M:%S"), "pareamento"))
            if cur.rowcount:
                self._evento("token", "", nome, "maquina pareada pelo /agente")
            self.con.commit()

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
                    " sub_novo, confianca, ordem_conf, base, estado, pronto,"
                    " sug_dep, sug_sec, sug_sub, sug_conf, sug_base)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (it.cod, it.linha, it.produto, int(it.ativo), it.ano,
                     it.dep_atual, it.sec_atual, it.sub_atual,
                     it.dep_novo, it.sec_novo, it.sub_novo,
                     it.confianca, ORDEM_CONF.get(it.confianca, 9), it.base,
                     LIVRE if tem_destino else SEM_DESTINO, 0,
                     it.dep_novo, it.sec_novo, it.sub_novo,
                     it.confianca, it.base),
                )
                existentes.add(it.cod)
                novos += 1
            self._evento("importou", "", "", f"{novos} novos, {ignorados} já existiam")
            self._aplicar_ativo(forcar=True)
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

    def itens_do_destino(self, dep: str, sec: str, sub: str,
                         confianca: str = "", so_ativos: bool = False,
                         limite: int = 2000) -> list:
        """Os pendentes daquele destino, para a pessoa ver antes de aprovar.

        A aprovação em lote é a única ação da curadoria que decide por centenas
        de produtos de uma vez, e era a única feita às cegas: o texto dizia
        quantos, nunca quais. Um item que caiu no grupo por engano passava
        junto, e ninguém saberia.

        A contagem vem daqui, e não de aritmética na tela. `no_destino` é medido
        no momento em que a fila é carregada; entre aquilo e o clique o curador
        já aprovou alguns, e outra pessoa pode ter mexido no grupo. Ler a lista
        agora é a única forma de o número bater com o que a ação faz.
        """
        dep, sec, sub = str(dep).strip(), str(sec).strip(), str(sub).strip()
        if not (dep and sec and sub):
            return []
        sql = ("SELECT cod, produto, ativo, confianca, base,"
               " dep_atual, sec_atual, sub_atual"
               " FROM itens WHERE pronto=0 AND estado=?"
               " AND dep_novo=? AND sec_novo=? AND sub_novo=?"
               + (" AND ativo=1" if so_ativos else "")
               + (" AND confianca=?" if confianca else "")
               + " ORDER BY produto LIMIT ?")
        p = [LIVRE, dep, sec, sub] + ([confianca] if confianca else []) + [limite]
        with self.lock:
            linhas = self.con.execute(sql, p).fetchall()
        return [{"cod": r["cod"], "produto": r["produto"] or "",
                 "ativo": bool(r["ativo"]), "confianca": r["confianca"] or "",
                 "base": r["base"] or "",
                 "atual": "/".join(x or "" for x in (r["dep_atual"],
                                                     r["sec_atual"],
                                                     r["sub_atual"]))}
                for r in linhas]

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
                        "SELECT estado, pronto, dep_novo, sec_novo, sub_novo"
                        " FROM itens WHERE cod=?", (cod,)).fetchone()
                    if r is None:
                        continue
                    if r["pronto"] or r["estado"] not in (LIVRE, SEM_DESTINO):
                        ja += 1
                        continue
                    sugerido = (r["dep_novo"] or "", r["sec_novo"] or "", r["sub_novo"] or "")
                    acao_item = "aceitou" if sugerido == (dep, sec, sub) else "corrigiu"
                    self.con.execute(
                        "UPDATE itens SET dep_novo=?, sec_novo=?, sub_novo=?,"
                        " estado=?, pronto=1, curado_em=?, curado_por=?,"
                        " curado_nota=?, curado_acao=? WHERE cod=?",
                        (dep, sec, sub, LIVRE, agora, por, nota or None,
                         acao_item, cod))
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

    def aceitar_sugestao(self, cods: list, por: str = "",
                         esperado: dict | None = None) -> dict:
        """Confirma o destino que a planilha já sugeriu, em lote.

        Ainda passa pela validação: uma planilha futura pode trazer trio que não
        existe na estrutura, e aceitar em lote é justamente onde isso passaria
        despercebido. O que não valida volta em `recusados`, com o motivo.

        `esperado` ({cod: "dep/sec/sub"}) é o destino que a pessoa VIU. A
        conferência guarda centenas de itens no navegador e sobe as aprovações
        depois; se a revisão trocar a sugestão nesse meio-tempo, aprovar o que
        está no banco seria aprovar um destino que ninguém olhou. Divergiu,
        não aprova: volta em `mudaram`, e o item continua pendente.
        """
        from scripts.reclassificacao_estrutura import estrutura

        cods = [c for c in (cods or []) if c]
        if not cods:
            return {"ok": False, "erro": "nenhum produto selecionado"}
        est = estrutura()
        grupos: dict[tuple, list] = {}
        recusados = []
        mudaram = []
        esperado = esperado or {}
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
                visto = esperado.get(cod)
                if visto is not None and str(visto) != "/".join(trio):
                    mudaram.append({"cod": cod, "agora": "/".join(trio)})
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
                "recusados": recusados, "mudaram": mudaram}

    # ── painel de andamento (só leitura) ─────────────────────────────────────
    def andamento(self, usuario: str = "", agora: float | None = None) -> dict:
        """Tudo o que o painel de andamento mostra. Só LÊ: nada aqui aprova,
        reserva ou grava.

        Conta só os ATIVOS (coluna `ativo`, regra `VENDA_DESDE`); o total geral
        vai só como texto auxiliar. "Dia" = dia de São Paulo (`FUSO`); dia útil =
        segunda a sexta. Atividade de sábado/domingo entra no acumulado, nos
        "hoje" e nos dias seguidos não-úteis, mas não vira barra no ritmo.
        """
        from datetime import datetime, timedelta
        import math
        import statistics
        from scripts.reclassificacao_estrutura import estrutura

        est = estrutura()
        agora = time.time() if agora is None else agora
        hoje = datetime.fromtimestamp(agora, FUSO).date()
        meia_noite = datetime(hoje.year, hoje.month, hoje.day, tzinfo=FUSO).timestamp()
        dia = lambda t: datetime.fromtimestamp(t, FUSO).date()  # noqa: E731

        with self.lock:
            itens = [dict(r) for r in self.con.execute(
                "SELECT cod, produto, estado, pronto, curado_em, curado_por,"
                " concluido_em, operador, expira_em, dep_novo, sec_novo, sub_novo"
                " FROM itens WHERE ativo=1")]
            total_geral = self.con.execute("SELECT COUNT(*) FROM itens").fetchone()[0]
            hora_atras = self.con.execute(
                "SELECT operador, concluido_em FROM itens WHERE estado=?"
                " AND concluido_em >= ? ORDER BY operador, concluido_em",
                (CONCLUIDO, agora - 3600)).fetchall()
            ultimos = [dict(r) for r in self.con.execute(
                "SELECT cod, produto, concluido_em, operador,"
                " COALESCE(NULLIF(dep_grav,''), dep_novo) AS dep,"
                " COALESCE(NULLIF(sec_grav,''), sec_novo) AS sec,"
                " COALESCE(NULLIF(sub_grav,''), sub_novo) AS sub"
                " FROM itens WHERE estado=? AND concluido_em IS NOT NULL"
                " ORDER BY concluido_em DESC LIMIT 5", (CONCLUIDO,))]
        total = len(itens)

        # ── as sete etapas (somam o total de ativos) ───────────────────────────
        # `concluido` inclui ja_correto e pulado: o agente fechou o item, nada
        # mais a fazer. `simulado` conta como "confirmado na fila": a pessoa já
        # decidiu, só falta a rodada de verdade gravar.
        etapas = {"gravado": 0, "gravando": 0, "confirmado": 0, "esperando": 0,
                  "sem_destino": 0, "falhou": 0, "descartado": 0}
        for it in itens:
            e = it["estado"]
            if e == CONCLUIDO:
                etapas["gravado"] += 1
            elif e == RESERVADO:
                etapas["gravando"] += 1
            elif e == SIMULADO or (e == LIVRE and it["pronto"]):
                etapas["confirmado"] += 1
            elif e == LIVRE:
                etapas["esperando"] += 1
            elif e == SEM_DESTINO:
                etapas["sem_destino"] += 1
            elif e == FALHOU:
                etapas["falhou"] += 1
            elif e == DESCARTADO:
                etapas["descartado"] += 1
        pct = lambda n: round(n * 100 / total, 1) if total else 0.0  # noqa: E731

        gravados = etapas["gravado"]
        grav_antes_hoje = sum(1 for it in itens if it["estado"] == CONCLUIDO
                              and it["concluido_em"] and it["concluido_em"] < meia_noite)
        pct_gravado = pct(gravados)
        marco = min(100, (math.floor(pct_gravado / 10) + 1) * 10)
        faltam_marco = max(0, math.ceil(total * marco / 100) - gravados)

        # ── por dia (só ativos) ────────────────────────────────────────────────
        conf_dia, grav_dia, meu_dia = {}, {}, {}
        for it in itens:
            if it["curado_em"]:
                d = dia(it["curado_em"])
                conf_dia[d] = conf_dia.get(d, 0) + 1
                if usuario and it["curado_por"] == usuario:
                    meu_dia[d] = meu_dia.get(d, 0) + 1
            if it["estado"] == CONCLUIDO and it["concluido_em"]:
                d = dia(it["concluido_em"])
                grav_dia[d] = grav_dia.get(d, 0) + 1

        def uteis_ate(d, n):
            """Os `n` últimos dias úteis até `d` (inclusive se for útil), do mais
            antigo para o mais novo."""
            out = []
            while len(out) < n:
                if d.weekday() < 5:
                    out.append(d)
                d -= timedelta(days=1)
            return out[::-1]

        ritmo_dias = uteis_ate(hoje, DIAS_RITMO)
        ritmo = [{"dia": d.isoformat(), "rotulo": f"{d.day:02d}/{d.month:02d}",
                  "semana": "smtqqss"[d.weekday()],
                  "confirmados": conf_dia.get(d, 0), "gravados": grav_dia.get(d, 0),
                  "hoje": d == hoje} for d in ritmo_dias]
        soma = [r["confirmados"] + r["gravados"] for r in ritmo]
        melhor_i = max(range(len(ritmo)), key=lambda i: soma[i]) if any(soma) else None
        for i, r in enumerate(ritmo):
            r["melhor"] = i == melhor_i
        # "o melhor dia do LOTE": contra todos os dias, não só os 10 do gráfico
        por_dia = {x: conf_dia.get(x, 0) + grav_dia.get(x, 0) for x in set(conf_dia) | set(grav_dia)}
        hoje_melhor_do_lote = bool(por_dia.get(hoje)) and por_dia[hoje] >= max(por_dia.values())

        # dias úteis seguidos com avanço. Hoje sem avanço AINDA não quebra a
        # sequência (o dia não acabou): conta-se a partir do último dia útil.
        avancou = lambda d: conf_dia.get(d, 0) + grav_dia.get(d, 0) > 0  # noqa: E731
        d = hoje
        if d.weekday() >= 5 or not avancou(d):
            d -= timedelta(days=1)
        seguidos = 0
        while True:
            if d.weekday() < 5:
                if not avancou(d):
                    break
                seguidos += 1
            d -= timedelta(days=1)

        # ── curadoria (pessoas) ────────────────────────────────────────────────
        # Os 7 últimos dias úteis INCLUINDO hoje: hoje ainda pela metade puxa a
        # média para baixo, então a estimativa erra para o lado seguro (diz mais
        # dias do que vai levar), e aparece já no primeiro dia de trabalho.
        media_dias = uteis_ate(hoje, DIAS_MEDIA)
        media = sum(conf_dia.get(x, 0) for x in media_dias) / DIAS_MEDIA
        faltam_decidir = etapas["esperando"] + etapas["sem_destino"]
        est_cur = round(faltam_decidir / media, 1) if media > 0 and faltam_decidir else None
        melhor_meu = max(meu_dia.items(), key=lambda x: x[1]) if meu_dia else None
        curadoria = {
            "hoje_equipe": conf_dia.get(hoje, 0),
            "hoje_usuario": meu_dia.get(hoje, 0),
            "melhor_dia_usuario": ({"dia": melhor_meu[0].isoformat(), "n": melhor_meu[1]}
                                   if melhor_meu else None),
            "media_dia_util": round(media, 1),
            "faltam_decidir": faltam_decidir,
            "estimativa_dias_uteis": est_cur,
        }

        # ── gravação (máquinas) ────────────────────────────────────────────────
        seg = self._seg_por_produto(hora_atras)
        operadores = self.listar_operadores()
        vivas = [o for o in operadores if o["vivo"]]
        fila = etapas["confirmado"] + etapas["gravando"]
        est_fila = (round(fila * seg / len(vivas) / 60, 1)
                    if seg and vivas and fila else (0.0 if not fila else None))
        gravacao = {
            "hoje": grav_dia.get(hoje, 0), "seg_por_produto": seg,
            "fila": fila, "maquinas_vivas": len(vivas),
            "estimativa_min": est_fila, "falharam": etapas["falhou"],
        }

        # ── por departamento de DESTINO ────────────────────────────────────────
        deps = {}
        for it in itens:
            dep = it["dep_novo"] or ""
            if not dep or it["estado"] in (DESCARTADO, SEM_DESTINO):
                continue    # sem destino não tem departamento
            g = deps.setdefault(dep, {"dep": dep, "nome": est.nome_dep(dep) or f"Departamento {dep}",
                                      "total": 0, "gravados": 0, "confirmados": 0, "faltam": 0})
            g["total"] += 1
            if it["estado"] == CONCLUIDO:
                g["gravados"] += 1
            elif it["estado"] == LIVRE and not it["pronto"]:
                g["faltam"] += 1
            elif it["estado"] in (LIVRE, RESERVADO, SIMULADO):
                g["confirmados"] += 1
        concluidos_dep = sorted(g["nome"] for g in deps.values() if g["gravados"] == g["total"])
        departamentos = sorted((g for g in deps.values() if g["gravados"] < g["total"]),
                               key=lambda g: (g["faltam"], g["nome"]))
        for g in departamentos:
            g["pct_gravado"] = round(g["gravados"] * 100 / g["total"], 1)
            g["pct_confirmado"] = round(g["confirmados"] * 100 / g["total"], 1)
        perto = next((g for g in departamentos if g["faltam"] > 0), None)
        mais_perto = (dict(perto, estimativa_min=max(1, round(perto["faltam"] * TEMPO_DECISAO_S / 60)))
                      if perto else None)

        maquinas = self._maquinas_painel(operadores, agora)

        # ── últimos gravados ───────────────────────────────────────────────────
        for u in ultimos:
            quando = datetime.fromtimestamp(u["concluido_em"], FUSO)
            u["hora"] = quando.strftime("%H:%M")
            u["e_hoje"] = quando.date() == hoje
            # de outro dia, a hora sozinha enganaria ("18:21" parece de hoje)
            u["quando"] = u["hora"] if u["e_hoje"] else quando.strftime("%d/%m %H:%M")
            u["destino"] = " › ".join(x for x in (est.nome_dep(u["dep"]), est.nome_sub(u["sub"])) if x) \
                or "/".join(str(x or "") for x in (u["dep"], u["sec"], u["sub"]))

        fim = None
        if total and gravados == total:
            fim = datetime.fromtimestamp(max(it["concluido_em"] or 0 for it in itens),
                                         FUSO).date().isoformat()
        return {
            "ok": True, "agora": agora, "hoje": hoje.isoformat(),
            "total": total, "total_geral": total_geral,
            "etapas": etapas, "gravados": gravados,
            "pct_gravado": pct_gravado, "pct_inicio_do_dia": pct(grav_antes_hoje),
            "proximo_marco": {"pct": marco, "faltam": faltam_marco} if pct_gravado < 100 else None,
            "curadoria": curadoria, "gravacao": gravacao,
            "ritmo": ritmo, "dias_seguidos_com_avanco": seguidos,
            "hoje_melhor_do_lote": hoje_melhor_do_lote,
            "departamentos": departamentos, "departamentos_concluidos": concluidos_dep,
            "mais_perto": mais_perto, "maquinas": maquinas,
            "ultimos_gravados": ultimos, "lote_concluido_em": fim,
        }

    @staticmethod
    def _seg_por_produto(conclusoes):
        """Mediana dos intervalos entre conclusões SEGUIDAS da mesma máquina
        (`conclusoes` ordenadas por operador e hora). Intervalo acima de
        `INTERVALO_MAX_S` é pausa, não ritmo, e fica de fora."""
        import statistics
        intervalos, anterior = [], {}
        for op, t in conclusoes:
            if op in anterior and 0 < t - anterior[op] <= INTERVALO_MAX_S:
                intervalos.append(t - anterior[op])
            anterior[op] = t
        return round(statistics.median(intervalos), 1) if intervalos else None

    def _maquinas_painel(self, operadores, agora):
        with self.lock:
            vencidos = dict(self.con.execute(
                "SELECT operador, COUNT(*) FROM itens WHERE estado=?"
                " AND expira_em < ? GROUP BY operador", (RESERVADO, agora)).fetchall())
        maquinas = []
        for o in operadores:
            if o.get("revogado_em"):
                continue
            bloco = o["bloco"] or 0
            res = o["reservados"] or 0
            maquinas.append({
                "nome": o["nome"], "maquina": o["maquina"], "estado": o["agente_estado"] or "",
                "vivo": o["vivo"], "silencio_s": o["silencio_s"], "atual": o["agente_atual"] or "",
                "comando": o["comando"], "reservados": res, "bloco": bloco,
                # feitos dentro do bloco = o que já saiu da reserva dele
                "feitos_bloco": max(0, bloco - res) if res else 0,
                # sem contato e com TODA a reserva vencida: os itens voltam à
                # fila na próxima reserva de qualquer máquina. Sem reserva não
                # há item nenhum para "voltar" — a frase seria falsa.
                "itens_voltaram": (not o["vivo"]) and res > 0 and vencidos.get(o["nome"], 0) == res,
            })
        return maquinas

    def andamento_leve(self, agora: float | None = None) -> dict:
        """A parte do painel que muda a cada segundo — etapas, percentual, fila e
        máquinas —, com contagens agrupadas no SQL. O painel chama a cada 4 s;
        o `andamento` completo (ritmo, departamentos) vai em cache de 60 s."""
        import math
        agora = time.time() if agora is None else agora
        with self.lock:
            linhas = self.con.execute(
                "SELECT estado, pronto, COUNT(*) FROM itens WHERE ativo=1"
                " GROUP BY estado, pronto").fetchall()
            hora_atras = self.con.execute(
                "SELECT operador, concluido_em FROM itens WHERE estado=?"
                " AND concluido_em >= ? ORDER BY operador, concluido_em",
                (CONCLUIDO, agora - 3600)).fetchall()
        etapas = {"gravado": 0, "gravando": 0, "confirmado": 0, "esperando": 0,
                  "sem_destino": 0, "falhou": 0, "descartado": 0}
        chave = {CONCLUIDO: "gravado", RESERVADO: "gravando", SIMULADO: "confirmado",
                 SEM_DESTINO: "sem_destino", FALHOU: "falhou", DESCARTADO: "descartado"}
        for estado, pronto, n in linhas:
            k = ("confirmado" if pronto else "esperando") if estado == LIVRE else chave.get(estado)
            if k:
                etapas[k] += n
        total = sum(etapas.values())
        gravados = etapas["gravado"]
        pct_gravado = round(gravados * 100 / total, 1) if total else 0.0
        marco = min(100, (math.floor(pct_gravado / 10) + 1) * 10)
        operadores = self.listar_operadores()
        vivas = [o for o in operadores if o["vivo"]]
        seg = self._seg_por_produto(hora_atras)
        fila = etapas["confirmado"] + etapas["gravando"]
        return {
            "ok": True, "agora": agora, "total": total, "etapas": etapas,
            "gravados": gravados, "pct_gravado": pct_gravado,
            "proximo_marco": ({"pct": marco, "faltam": max(0, math.ceil(total * marco / 100) - gravados)}
                              if pct_gravado < 100 else None),
            "gravacao_leve": {
                "fila": fila, "maquinas_vivas": len(vivas), "seg_por_produto": seg,
                "estimativa_min": (round(fila * seg / len(vivas) / 60, 1)
                                   if seg and vivas and fila else (0.0 if not fila else None)),
                "falharam": etapas["falhou"]},
            "maquinas": self._maquinas_painel(operadores, agora),
        }

    def resumo_sessao(self, usuario: str, desde: float) -> dict:
        """O que esta pessoa decidiu desde `desde` (início da sessão na tela).

        Conta pelo estado ATUAL do banco: aprovação que voltou em `ja_curados`
        (era de outra pessoa) ou que foi desfeita não está como dela, então o
        número bate com o que de fato ficou aprovado."""
        with self.lock:
            linhas = self.con.execute(
                "SELECT estado, curado_acao FROM itens WHERE curado_por=?"
                " AND curado_em >= ? AND pronto=1", (usuario, float(desde))).fetchall()
        a = self.andamento(usuario)
        return {
            "ok": True, "desde": float(desde),
            "confirmados": len(linhas),
            "ja_gravados": sum(1 for e, _ in linhas if e == CONCLUIDO),
            "na_fila": sum(1 for e, _ in linhas if e in (LIVRE, RESERVADO, SIMULADO)),
            "corrigidos": sum(1 for _, ac in linhas if ac == "corrigiu"),
            "pct_inicio_do_dia": a["pct_inicio_do_dia"], "pct_agora": a["pct_gravado"],
            "equipe_hoje": a["curadoria"]["hoje_equipe"],
            "mais_perto": a["mais_perto"],
            "algum_departamento_concluido": bool(a["departamentos_concluidos"]),
        }

    # ── inclusão a partir do catálogo ────────────────────────────────────────
    def incluir_do_catalogo(self, so_com_venda: bool = True, aplicar: bool = False,
                            por: str = "") -> dict:
        """Põe no lote o produto cuja classificação de HOJE no ERP (catálogo)
        está fora da estrutura nova e que a planilha não trouxe.

        Achado em 30/10/2026: 7.410 produtos assim estavam fora do lote — o
        departamento 3 inteiro (seção 35, um balaio de 7 mil itens) e 306 trios
        inconsistentes dentro dos departamentos novos (a armadilha dos códigos:
        `6/51/28`, subseção 28 é da seção 34). A curadoria só mostra o que está
        no lote, então ninguém os veria.

        `so_com_venda` (padrão, pedido do usuário): só quem vendeu desde
        `VENDA_DESDE` — os ativos, prioritários. Entram como os outros: ativos,
        **sem nada confirmado** (`pronto=0`), sem destino e confiança REVISAR; o
        destino sugerido vem do motor de revisão rodado SÓ para eles. Sem
        `aplicar`, é a prévia. Repetir é seguro: quem já está no lote é pulado.
        """
        from scripts import catalogo
        from scripts.reclassificacao_estrutura import estrutura

        est = estrutura()
        if not est.carregada:
            return {"ok": False, "erro": "estrutura merceológica não carregada"}
        vendidos, _ = codigos_com_venda()
        if so_com_venda and vendidos is None:
            return {"ok": False, "erro": "sem banco de vendas: não há como saber quem vendeu"}
        with self.lock:
            no_lote = {_chave_cb(c) for (c,) in self.con.execute("SELECT cod FROM itens")}
        hoje = time.strftime("%d/%m/%Y")
        novos, vistos = [], set()
        for cb, desc, dep, sec, sub in catalogo.produtos_com_grupo():
            k = _chave_cb(cb)
            if k in no_lote or k in vistos:
                continue          # já no lote (inclusive variante só de zeros)
            if so_com_venda and k not in vendidos:
                continue
            ok, motivo = est.validar(dep or "", sec or "", sub or "")
            if ok:
                continue
            vistos.add(k)
            trio = f"{dep or '?'}/{sec or '?'}/{sub or '?'}"
            novos.append({"cod": cb, "produto": desc or "", "dep": dep or "",
                          "sec": sec or "", "sub": sub or "",
                          "base": f"incluído do catálogo em {hoje}: no ERP está em "
                                  f"{trio}, fora da estrutura nova ({motivo})"})
        resumo = {"ok": True, "encontrados": len(novos), "so_com_venda": so_com_venda,
                  "amostra": [{"cod": n["cod"], "produto": n["produto"],
                               "atual": f'{n["dep"]}/{n["sec"]}/{n["sub"]}',
                               "base": n["base"]} for n in novos[:50]]}
        if not aplicar or not novos:
            return resumo

        with self.lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                for n in novos:
                    self.con.execute(
                        "INSERT INTO itens (cod, produto, ativo, ano, dep_atual,"
                        " sec_atual, sub_atual, confianca, ordem_conf, base, estado,"
                        " pronto, sug_conf, sug_base)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (n["cod"], n["produto"], 1, "", n["dep"], n["sec"], n["sub"],
                         "REVISAR", ORDEM_CONF["REVISAR"], n["base"], SEM_DESTINO, 0,
                         "REVISAR", n["base"]))
                self._evento("incluiu", "", por,
                             f"{len(novos)} produtos do catálogo com classificação fora"
                             f" da estrutura nova{' e venda em 2026' if so_com_venda else ''}")
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise
        rev = self.revisar(so_ativos=False, aplicar=True, por=por,
                           somente={n["cod"] for n in novos})
        resumo.update({"incluidos": len(novos), "com_sugestao": rev.get("revisados", 0)})
        return resumo

    # ── revisão das sugestões ────────────────────────────────────────────────
    def revisar(self, so_ativos: bool = True, aplicar: bool = False,
                por: str = "", somente: set | None = None) -> dict:
        """Revê o destino sugerido dos pendentes à luz do que já foi confirmado.

        O motor mora em `reclassificacao_revisao` (lá estão a regra e a
        medição). Aqui só se lê, decide o que gravar e grava. Sem `aplicar`, é
        a prévia: diz quantos mudariam e mostra quais.

        Só mexe em item PENDENTE (`pronto=0`, livre ou sem destino), e só na
        sugestão. Nada sai daqui aprovado — quem aprova continua sendo a pessoa
        na curadoria. Rodar de novo recalcula tudo: o que a revisão trocou e
        perdeu o apoio volta para a sugestão da planilha.
        """
        from scripts import catalogo
        from scripts import reclassificacao_revisao as rv
        from scripts.reclassificacao_estrutura import estrutura

        est = estrutura()
        with self.lock:
            linhas = self.con.execute(
                "SELECT cod, produto, ativo, estado, pronto, curado_em,"
                " dep_novo, sec_novo, sub_novo, confianca, base,"
                " sug_dep, sug_sec, sug_sub, sug_conf, sug_base, rev_em"
                " FROM itens WHERE estado<>?", (DESCARTADO,)).fetchall()
        try:
            ncm = catalogo.ncm_por_codigos()
        except Exception:
            ncm = {}                   # sem catálogo, revisa só pela descrição

        base, alvo = [], []
        for r in linhas:
            confirmado = r["curado_em"] is not None
            # O voto de quem ninguém decidiu é o palpite ORIGINAL da planilha,
            # nunca o já revisado: senão a revisão viraria prova de si mesma.
            destino = ((r["dep_novo"], r["sec_novo"], r["sub_novo"]) if confirmado
                       else (r["sug_dep"], r["sug_sec"], r["sug_sub"]))
            if all(destino):
                base.append({"cod": r["cod"], "produto": r["produto"] or "",
                             "destino": tuple(str(x) for x in destino),
                             "confirmado": confirmado})
            if (not r["pronto"] and r["estado"] in (LIVRE, SEM_DESTINO)
                    and (r["ativo"] or not so_ativos)
                    and (somente is None or r["cod"] in somente)):
                alvo.append({
                    "cod": r["cod"], "produto": r["produto"] or "",
                    "planilha": (r["sug_dep"] or "", r["sug_sec"] or "",
                                 r["sug_sub"] or ""),
                    "atual": (r["dep_novo"] or "", r["sec_novo"] or "",
                              r["sub_novo"] or ""),
                    "planilha_conf": r["sug_conf"] or "",
                    "planilha_base": r["sug_base"] or "",
                    "rev_em": r["rev_em"]})

        motor = rv.Motor(base, ncm)
        propostas = rv.propor(motor, alvo, rotulo=est.rotulo_curto,
                              validar=est.validar)
        atual = {a["cod"]: a for a in alvo}
        mudar = [p for p in propostas
                 if tuple(p["destino"]) != atual[p["cod"]]["atual"]
                 or bool(p["revisado"]) != bool(atual[p["cod"]]["rev_em"])]
        revisados = sum(1 for p in propostas if p["revisado"])
        voltam = sum(1 for p in mudar if not p["revisado"])
        com_ncm = sum(1 for a in alvo if a["cod"] in ncm)

        def amostra(p):
            a = atual[p["cod"]]
            return {"cod": p["cod"], "produto": p["produto"],
                    "de": est.rotulo_curto(*a["atual"]) if all(a["atual"])
                    else "sem sugestão",
                    "para": est.rotulo_curto(*p["destino"]) if all(p["destino"])
                    else "sem sugestão",
                    "confianca": p["confianca"], "base": p["base"],
                    "revisado": p["revisado"]}

        resumo = {"ok": True, "alvo": len(alvo), "com_ncm": com_ncm,
                  "catalogo_com_ncm": bool(ncm), "revisados": revisados,
                  "mudam": len(mudar), "voltam_planilha": voltam,
                  "so_ativos": so_ativos,
                  "amostra": [amostra(p) for p in mudar[:400]]}
        if not aplicar:
            return resumo

        agora = time.time()
        gravados = 0
        with self.lock:
            self.con.execute("BEGIN IMMEDIATE")
            try:
                for p in mudar:
                    dep, sec, sub = p["destino"]
                    tem = bool(dep and sec and sub)
                    conf = p["confianca"] or ""
                    cur = self.con.execute(
                        "UPDATE itens SET dep_novo=?, sec_novo=?, sub_novo=?,"
                        " estado=?, confianca=?, ordem_conf=?, base=?,"
                        " rev_em=?, rev_por=?"
                        " WHERE cod=? AND pronto=0 AND estado IN (?,?)",
                        (dep or None, sec or None, sub or None,
                         LIVRE if tem else SEM_DESTINO, conf,
                         ORDEM_CONF.get(conf, 9), p["base"],
                         agora if p["revisado"] else None,
                         por if p["revisado"] else None,
                         p["cod"], LIVRE, SEM_DESTINO))
                    gravados += cur.rowcount
                self._evento("revisou", "", por,
                             f"{gravados} sugestões alteradas"
                             f" ({revisados} revisadas pelo histórico,"
                             f" {voltam} de volta à planilha;"
                             f" {'só ativos' if so_ativos else 'todos'})")
                self.con.commit()
            except Exception:
                self.con.rollback()
                raise
        resumo["gravados"] = gravados
        return resumo

    def historico_curadoria(self, limite: int = 50, por: str = "",
                            so_meus: bool = False) -> list:
        """Últimos itens confirmados, do mais recente para o mais antigo.

        Serve para consertar o erro que só se percebe dois produtos depois. O
        `Z` da conferência alcança apenas o último; daqui se enxerga a trilha
        inteira.

        Cada linha carrega o **estado atual**, e não só o que foi decidido:
        saber que um item já está com um agente é o que diz se ainda dá para
        mexer nele. Sem isso a tela ofereceria um botão que vai falhar.
        """
        limite = max(1, min(int(limite), 200))
        onde = "curado_em IS NOT NULL"
        params: list = []
        if so_meus and por:
            onde += " AND curado_por=?"
            params.append(por)
        with self.lock:
            linhas = self.con.execute(
                f"SELECT cod, produto, ativo, confianca,"
                f" dep_atual, sec_atual, sub_atual,"
                f" dep_novo, sec_novo, sub_novo, estado, pronto,"
                f" curado_em, curado_por, curado_nota, operador, situacao"
                f" FROM itens WHERE {onde}"
                f" ORDER BY curado_em DESC LIMIT ?", [*params, limite]
            ).fetchall()
        saida = []
        for r in linhas:
            d = dict(r)
            d["ativo"] = bool(r["ativo"])
            # Editável enquanto ninguém trabalhou nele. Assim que um agente
            # reserva ou fecha, a janela se fecha: desfazer no banco não desfaz
            # o que já foi escrito no ERP.
            d["editavel"] = bool(r["pronto"]) and r["estado"] == LIVRE
            d["meu"] = (r["curado_por"] or "") == (por or "")
            saida.append(d)
        return saida

    def recurar(self, cod: str, dep: str, sec: str, sub: str,
                por: str = "", nota: str = "") -> dict:
        """Corrige o destino de um item JÁ confirmado, que ninguém trabalhou.

        Existe porque `curar` recusa item confirmado de propósito (`pronto=1`
        cai em `ja_curados`), e essa recusa é a proteção contra dois curadores
        se sobrescreverem. Corrigir é outra intenção: veio da tela de histórico,
        onde a pessoa vê o produto, o destino e **de quem foi a decisão** antes
        de clicar. Uma ação deliberada e informada, não uma colisão cega.
        """
        from scripts.reclassificacao_estrutura import estrutura

        dep, sec, sub = str(dep).strip(), str(sec).strip(), str(sub).strip()
        ok, motivo = estrutura().validar(dep, sec, sub)
        if not ok:
            return {"ok": False, "erro": motivo}

        with self.lock:
            r = self.con.execute(
                "SELECT estado, pronto, curado_por, dep_novo, sec_novo, sub_novo"
                " FROM itens WHERE cod=?", (cod,)).fetchone()
            if r is None:
                return {"ok": False, "erro": "produto não está no lote"}
            if not r["pronto"] or r["estado"] != LIVRE:
                return {"ok": False, "erro": (
                    f"tarde demais: o produto está em '{r['estado']}'. "
                    "Depois que um agente pega, o que vale é o que foi escrito "
                    "no ERP."), "estado": r["estado"]}

            antes = (r["dep_novo"] or "", r["sec_novo"] or "", r["sub_novo"] or "")
            self.con.execute(
                "UPDATE itens SET dep_novo=?, sec_novo=?, sub_novo=?,"
                " curado_em=?, curado_por=?, curado_nota=?,"
                " curado_acao=CASE WHEN ? THEN 'corrigiu' ELSE curado_acao END"
                " WHERE cod=?",
                (dep, sec, sub, time.time(), por, nota or None,
                 antes != (dep, sec, sub), cod))
            self._evento("corrigiu", cod, por,
                         f"destino corrigido (era de {r['curado_por'] or '—'})",
                         antes=antes, depois=(dep, sec, sub))
            self.con.commit()
        return {"ok": True, "cod": cod, "antes": antes,
                "depois": (dep, sec, sub), "era_de": r["curado_por"] or ""}

    def descurar(self, cods: list, por: str = "",
                 deliberado: bool = False) -> dict:
        """Desfaz uma confirmação, enquanto ela ainda não virou trabalho.

        `deliberado` separa dois gestos que só parecem o mesmo. O `Z` da
        conferência é **cego**: desfaz "o último", sem a pessoa ver de quem era
        — e como as duas telas entregam a mesma fila, esse último pode ser de
        outro curador. Já o botão do histórico mostra o produto, o destino e o
        autor antes do clique. Um exige dono; o outro, não.

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
        desfeitos, tarde, alheios = 0, [], []
        with self.lock:
            for cod in cods:
                r = self.con.execute(
                    "SELECT estado, pronto, curado_por FROM itens WHERE cod=?",
                    (cod,)).fetchone()
                if r is None or not r["pronto"]:
                    continue
                if r["estado"] != LIVRE:
                    tarde.append({"cod": cod, "estado": r["estado"]})
                    continue
                # Só se desfaz o que é seu. Dois curadores na mesma fila veem a
                # MESMA lista e começam pelo mesmo produto: quando o segundo
                # aperta Enter, a aprovação vira no-op (`ja_curados`) — e o `Z`
                # seguinte estaria desfazendo o trabalho do primeiro, que não
                # saberia de nada. O desfazer é do autor, não de quem passou por
                # último.
                if not deliberado and (r["curado_por"] or "") != (por or ""):
                    alheios.append({"cod": cod, "de": r["curado_por"] or "outro"})
                    continue
                self.con.execute(
                    "UPDATE itens SET pronto=0, curado_em=NULL, curado_por=NULL,"
                    " curado_nota=NULL WHERE cod=? AND estado=? AND pronto=1",
                    (cod, LIVRE))
                desfeitos += 1
            if desfeitos:
                self._evento("desfez", "", por,
                             f"{desfeitos} confirmações desfeitas"
                             + (" (pelo histórico)" if deliberado else ""))
            self.con.commit()
        return {"ok": True, "desfeitos": desfeitos, "tarde_demais": tarde,
                "de_outro": alheios}

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


def andamento(usuario: str = "") -> dict:
    """Atalho de módulo: `banco().andamento(usuario)`."""
    return banco().andamento(usuario)
