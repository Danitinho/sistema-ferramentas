"""
scripts/vencidos_rebaixa.py
===========================
Rebaixa de preço dos produtos que vão sobrar no vencimento.

O aviso de vencimento já estima a SOBRA (quanto do lote ainda estará na gôndola
no dia de vencer, pelo ritmo de venda — `vencidos._riscos_para`). Este módulo
fecha o ciclo: sugere um preço de rebaixa para os que têm risco, registra a
rebaixa decidida e acompanha até ela ser lançada no ERP.

O sistema NÃO muda preço no ERP. A rebaixa registrada aqui é uma tarefa do
escritório — "lançar R$ X de tal a tal data" — e só vira `lancada` quando
alguém marca que lançou (com a referência). Mesmo modelo de "registrar aqui,
lançar lá" do resto do sistema.

A SUGESTÃO É UMA REGRA, NÃO UMA PREVISÃO. Não há dado de elasticidade (quanto a
venda sobe com X% de desconto) — inventar um seria pior que dizer a regra.
Então a regra fica escrita na tela:
  • risco alto (≥50% do lote sobra): 30% de desconto; médio (20–50%): 15%;
  • faltando 7 dias ou menos: +10 pontos;
  • preço terminado em ,99 PARA BAIXO, nunca abaixo do custo (piso). Quando o
    piso trava o desconto, a sugestão diz isso — abaixo do custo ainda pode
    valer a pena (perder parte é melhor que perder tudo), mas é decisão da
    pessoa, não da regra.

Estados (`status` gravado): a_lancar → lancada; cancelada a qualquer momento
antes do fim. "encerrada" é DERIVADO: o aviso foi resolvido (virou vencido) ou
a data final passou.

Persiste em `vencidos.db` (tabela `rebaixas`), pela conexão do próprio módulo
de vencidos. Auditoria na tabela `auditoria` dele.

Camada de lógica: NÃO importa Flask.
"""
import math
from datetime import date

from scripts import db_util
from scripts import vencidos as v

DESCONTO_POR_RISCO = {"alto": 30, "medio": 15}
EXTRA_URGENTE = 10            # pontos a mais quando falta pouco
DIAS_URGENTE = 7
STATUS = ("a_lancar", "lancada", "cancelada")
STATUS_LABEL = {"a_lancar": "A lançar no ERP", "lancada": "Lançada no ERP",
                "cancelada": "Cancelada", "encerrada": "Encerrada"}


def _init(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS rebaixas (
            id            TEXT PRIMARY KEY,
            aviso_id      TEXT NOT NULL,
            codigo_barras TEXT,
            produto       TEXT NOT NULL,
            preco_de      REAL,               -- venda antes da rebaixa
            preco_por     REAL NOT NULL,      -- preço da rebaixa
            custo         REAL,
            quantidade    REAL,               -- sobra estimada no registro
            inicio        TEXT NOT NULL,      -- AAAA-MM-DD
            fim           TEXT NOT NULL,      -- AAAA-MM-DD (até a véspera do vencimento)
            status        TEXT NOT NULL DEFAULT 'a_lancar',
            obs           TEXT,
            criado_em     TEXT NOT NULL,
            criado_por    TEXT,
            atualizado_em TEXT,
            lancada_em    TEXT,
            lancada_por   TEXT,
            lancada_ref   TEXT,
            cancelada_em  TEXT,
            cancelada_por TEXT,
            cancelada_motivo TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_rebaixa_aviso ON rebaixas(aviso_id);
        CREATE INDEX IF NOT EXISTS idx_rebaixa_status ON rebaixas(status);
    """)
    conn.commit()


def _conn():
    conn = v._conn()
    # chave própria: o esquema de vencidos é preparado pelo módulo de vencidos
    db_util.preparar(conn, v.BANCO + "#rebaixas", _init)
    return conn


# ── Regra da sugestão ─────────────────────────────────────────────────────────
def _arredondar_99_baixo(preco):
    """Maior preço terminado em ,99 que não passa de `preco` (em centavos)."""
    c = math.floor(round(preco * 100, 6))
    alvo = (c // 100) * 100 + 99
    if alvo > c:
        alvo -= 100
    return max(alvo, 0) / 100


def sugerir_preco(venda, custo, nivel, dias_para_vencer):
    """{desconto_pct, preco, travado_no_custo, regra} ou None se não há o que
    sugerir (sem preço de venda, risco baixo)."""
    if not venda or venda <= 0 or nivel not in DESCONTO_POR_RISCO:
        return None
    desc = DESCONTO_POR_RISCO[nivel]
    partes = [f"risco {'alto' if nivel == 'alto' else 'médio'}: {desc}%"]
    if dias_para_vencer is not None and dias_para_vencer <= DIAS_URGENTE:
        desc += EXTRA_URGENTE
        partes.append(f"faltam {dias_para_vencer} dia(s): +{EXTRA_URGENTE}")
    bruto = venda * (1 - desc / 100)
    preco = _arredondar_99_baixo(bruto)
    if preco <= 0:
        preco = round(bruto, 2)
    travado = False
    if custo and custo > 0 and preco < custo:
        preco = math.ceil(round(custo * 100, 6)) / 100
        travado = True
        partes.append("parou no custo")
    if preco >= venda:
        return None        # o piso não deixa rebaixar nada
    return {"desconto_pct": desc,
            "desconto_real_pct": round((1 - preco / venda) * 100, 1),
            "preco": preco, "travado_no_custo": travado,
            "regra": " · ".join(partes)}


# ── Leitura ───────────────────────────────────────────────────────────────────
def _ativas_por_aviso(conn):
    return {r["aviso_id"]: dict(r) for r in conn.execute(
        "SELECT * FROM rebaixas WHERE status IN ('a_lancar','lancada')")}


def sugestoes():
    """Avisos ativos com risco médio/alto de sobra e SEM rebaixa em andamento,
    do mais urgente ao menos. Cada item traz o aviso, o risco e a sugestão."""
    avisos = v.listar_avisos(incluir_resolvidos=False, limite=None, ordem="vencimento")
    conn = _conn()
    try:
        ativas = _ativas_por_aviso(conn)
    finally:
        conn.close()
    saida = []
    for a in avisos:
        risco = a.get("risco")
        if not risco or risco["nivel"] not in DESCONTO_POR_RISCO:
            continue
        dias = a.get("dias_para_vencer")
        # vence hoje (0) ou já venceu: não há dia de rebaixa antes do vencimento
        if a["id"] in ativas or dias is None or dias < 1:
            continue
        venda = a.get("venda")
        sug = sugerir_preco(venda, a.get("custo"), risco["nivel"], a["dias_para_vencer"])
        saida.append({
            "aviso_id": a["id"], "produto": a["produto"],
            "codigo_barras": a.get("codigo_barras") or "",
            "fornecedor": a.get("fornecedor") or "",
            "quantidade": a["quantidade"], "data_vencimento": a["data_vencimento"],
            "data_venc_fmt": a["data_venc_fmt"], "dias_para_vencer": a["dias_para_vencer"],
            "venda": venda, "custo": a.get("custo"),
            "valor_promocional": a.get("valor_promocional"),
            "risco": risco, "sugestao": sug,
            "sem_preco": not venda,
            "fim_sugerido": _vespera(a["data_vencimento"]),
        })
    # o que vence antes e o que mais sobra (em R$) primeiro
    saida.sort(key=lambda s: (s["dias_para_vencer"], -(s["risco"]["valor_sobra"] or 0)))
    return saida


def _vespera(data_iso):
    try:
        return date.fromordinal(date.fromisoformat(data_iso).toordinal() - 1).isoformat()
    except (TypeError, ValueError):
        return data_iso


def listar(incluir_encerradas=True, limite=300):
    """Rebaixas registradas, com o status efetivo (encerrada é derivado) e o
    resultado quando o aviso já foi resolvido."""
    conn = _conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT r.*, a.resolvido_em AS aviso_resolvido_em, a.quantidade AS aviso_qtd,"
            " a.data_vencimento, a.resolvido_vencido_id,"
            " (SELECT ve.quantidade FROM vencidos ve WHERE ve.id = a.resolvido_vencido_id"
            "   AND ve.excluido_em IS NULL) AS qtd_vencida"
            " FROM rebaixas r LEFT JOIN avisos a ON a.id = r.aviso_id"
            " ORDER BY (r.status = 'a_lancar') DESC, r.inicio DESC, r.criado_em DESC LIMIT ?",
            (int(limite),))]
    finally:
        conn.close()
    hoje = v._hoje()
    saida = []
    for r in rows:
        efetivo = r["status"]
        if efetivo in ("a_lancar", "lancada") and (r["aviso_resolvido_em"] or r["fim"] < hoje):
            efetivo = "encerrada"
        r["status_efetivo"] = efetivo
        r["status_label"] = STATUS_LABEL[efetivo]
        r["inicio_fmt"] = v._fmt_dia(r["inicio"])
        r["fim_fmt"] = v._fmt_dia(r["fim"])
        r["venc_fmt"] = v._fmt_dia(r["data_vencimento"] or "")
        r["criado_fmt"] = v._fmt_dt(r["criado_em"])
        r["lancada_fmt"] = v._fmt_dt(r["lancada_em"] or "")
        r["desconto_pct"] = (round((1 - r["preco_por"] / r["preco_de"]) * 100, 1)
                             if r["preco_de"] else None)
        r["abaixo_custo"] = bool(r["custo"] and r["preco_por"] < r["custo"])
        if r["aviso_resolvido_em"] and r["qtd_vencida"] is not None:
            r["resultado"] = {"avisadas": r["aviso_qtd"], "vencidas": r["qtd_vencida"]}
        if efetivo == "encerrada" and not incluir_encerradas:
            continue
        saida.append(r)
    return saida


# ── Escrita ───────────────────────────────────────────────────────────────────
def registrar(aviso_id, preco_por, inicio=None, fim=None, obs=None, usuario=None):
    """Registra a rebaixa decidida para um aviso ativo. O preço vai também para
    o `valor_promocional` do aviso (é o campo que a tela e o relatório já
    mostram). Devolve (ok, msg, id)."""
    preco = v._parse_valor_opcional(preco_por)
    if not preco:
        return False, "Informe o preço da rebaixa.", None
    ini = v._norm_dia(inicio) or v._hoje()
    conn = _conn()
    try:
        a = conn.execute("SELECT * FROM avisos WHERE id=? AND excluido_em IS NULL",
                         (aviso_id,)).fetchone()
        if not a:
            return False, "Aviso não encontrado.", None
        if a["resolvido_em"]:
            return False, "Este aviso já virou vencido — não cabe mais rebaixa.", None
        fim_ = v._norm_dia(fim) or _vespera(a["data_vencimento"])
        if fim_ < ini:
            return False, "A data final é antes do início.", None
        if fim_ >= a["data_vencimento"]:
            return False, "A rebaixa tem de terminar antes do vencimento.", None
        if a["venda"] and preco >= a["venda"]:
            return False, (f"R$ {preco:.2f} não é rebaixa: a venda é "
                           f"R$ {a['venda']:.2f}.").replace(".", ","), None
        if conn.execute("SELECT 1 FROM rebaixas WHERE aviso_id=? AND status IN"
                        " ('a_lancar','lancada')", (aviso_id,)).fetchone():
            return False, "Este aviso já tem uma rebaixa em andamento.", None
        # sobra estimada no momento do registro (só para referência)
        sobra = None
        try:
            item = v._enriquecer_aviso(dict(a))
            v._riscos_para([item])
            sobra = (item.get("risco") or {}).get("sobra")
        except Exception:  # noqa: BLE001 — sem vendas, segue sem a referência
            pass
        rid = v._uid()
        agora = v._agora()
        conn.execute(
            "INSERT INTO rebaixas (id, aviso_id, codigo_barras, produto, preco_de, preco_por,"
            " custo, quantidade, inicio, fim, status, obs, criado_em, criado_por, atualizado_em)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,'a_lancar',?,?,?,?)",
            (rid, aviso_id, a["codigo_barras"], a["produto"], a["venda"], preco, a["custo"],
             sobra, ini, fim_, (obs or "").strip(), agora, usuario, agora))
        conn.execute("UPDATE avisos SET valor_promocional=?, atualizado_em=? WHERE id=?",
                     (preco, agora, aviso_id))
        v._auditar(conn, "rebaixa", rid, "registrar",
                   f"{a['produto']} · R$ {preco:.2f} de {v._fmt_dia(ini)} a {v._fmt_dia(fim_)}",
                   usuario)
        conn.commit()
        return True, "Rebaixa registrada. Falta lançar no ERP.", rid
    finally:
        conn.close()


def marcar_lancada(rebaixa_id, referencia="", usuario=None):
    conn = _conn()
    try:
        r = conn.execute("UPDATE rebaixas SET status='lancada', lancada_em=?, lancada_por=?,"
                         " lancada_ref=?, atualizado_em=? WHERE id=? AND status='a_lancar'",
                         (v._agora(), usuario, (referencia or "").strip(), v._agora(),
                          rebaixa_id))
        if not r.rowcount:
            return False, "Rebaixa não encontrada ou já lançada."
        v._auditar(conn, "rebaixa", rebaixa_id, "lancada", referencia or "", usuario)
        conn.commit()
        return True, "Marcada como lançada no ERP."
    finally:
        conn.close()


def desfazer_lancada(rebaixa_id, usuario=None):
    """Marcou por engano: volta para "a lançar"."""
    conn = _conn()
    try:
        r = conn.execute("UPDATE rebaixas SET status='a_lancar', lancada_em=NULL,"
                         " lancada_por=NULL, lancada_ref=NULL, atualizado_em=?"
                         " WHERE id=? AND status='lancada'", (v._agora(), rebaixa_id))
        if not r.rowcount:
            return False, "Rebaixa não está marcada como lançada."
        v._auditar(conn, "rebaixa", rebaixa_id, "desfazer_lancada", "", usuario)
        conn.commit()
        return True, "Voltou para a lançar."
    finally:
        conn.close()


def cancelar(rebaixa_id, motivo="", usuario=None):
    """Cancela e tira o preço do `valor_promocional` do aviso (se ainda for o
    desta rebaixa). Se já tinha sido lançada no ERP, o preço LÁ continua: a
    mensagem lembra de desfazer no ERP."""
    conn = _conn()
    try:
        r = conn.execute("SELECT * FROM rebaixas WHERE id=?", (rebaixa_id,)).fetchone()
        if not r or r["status"] == "cancelada":
            return False, "Rebaixa não encontrada ou já cancelada."
        agora = v._agora()
        conn.execute("UPDATE rebaixas SET status='cancelada', cancelada_em=?, cancelada_por=?,"
                     " cancelada_motivo=?, atualizado_em=? WHERE id=?",
                     (agora, usuario, (motivo or "").strip(), agora, rebaixa_id))
        conn.execute("UPDATE avisos SET valor_promocional=NULL, atualizado_em=?"
                     " WHERE id=? AND valor_promocional=? AND resolvido_em IS NULL",
                     (agora, r["aviso_id"], r["preco_por"]))
        v._auditar(conn, "rebaixa", rebaixa_id, "cancelar", motivo or "", usuario)
        conn.commit()
        if r["status"] == "lancada":
            return True, "Cancelada aqui. Ela já estava lançada: volte o preço no ERP."
        return True, "Rebaixa cancelada."
    finally:
        conn.close()
