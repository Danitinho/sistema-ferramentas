# -*- coding: utf-8 -*-
"""
scripts/vencidos_notas.py
Inclusão dos vencidos na nota de vencido (tipo 3) de cada fornecedor, pelo
agente geral do ERP (seção 18 do CLAUDE.md). Sem Flask.

Fluxo de uma RODADA (pedido do usuário, 28/09/2026):
  1. todos os vencidos com baixa pendente e ainda fora de nota, AGRUPADOS por
     fornecedor — cada nota é aberta uma vez só;
  2. por fornecedor: TROCA (nota de vencido do fornecedor) ou PERDA (nota de
     perda — ainda não implementada, o grupo fica esperando). Quem decide é o
     fornecedor; a última escolha dele vira o padrão;
  3. fornecedor sem o nº do RADGe no cadastro não anda (a tela pede o número);
  4. `nf.abrir_ultima` tipo 3 -> a pessoa confere a nota no ERP e a lista ->
     aprova -> `nf.incluir_itens` com o nº da nota (o agente recusa se a nota
     na tela tiver mudado) -> cada item GRAVADO vira baixa "Devolução ao
     fornecedor" com o nº da nota; pulado/falhou volta a pendente com o motivo.

A regra que não pode afrouxar: **um vencido nunca vai duas vezes para uma
nota.** Incluir de novo duplicaria o item no ERP, e ninguém veria. Por isso:
  • o vencido é marcado `em_andamento` ANTES de o pedido existir, por UPDATE
    condicional (só pega o que está livre) — duas rodadas simultâneas não
    pegam o mesmo item;
  • pedido que terminou sem resultado depois de pego pela máquina vira
    `a_conferir`, não volta sozinho: a pessoa olha a nota e libera;
  • aplicar o resultado é idempotente (só mexe em quem ainda está
    `em_andamento` com aquele pedido).
"""
from __future__ import annotations

import json
import sqlite3

from scripts import vencidos as v

DESTINOS = ("troca", "perda")
DESTINO_PADRAO = "troca"
TIPO_NOTA_VENCIDO = "3"
LIVRES = ("", "pulado", "falhou")          # nota_situacao que pode entrar numa rodada


def _conn():
    return v._conn()


def _agente():
    from scripts import agente_erp
    return agente_erp


def _fornecedor(fid):
    try:
        from scripts import fornecedores
        return fornecedores.buscar(fid)
    except Exception:
        return None


def _no_catalogo(cb):
    try:
        return bool(_agente().esperado_do_catalogo(cb).get("codigo"))
    except Exception:
        return False


def _ref_nota(nota: dict) -> str:
    """Como a nota aparece na baixa: 'Nota 4001 (lanc. 4001)'."""
    num, lanc = (nota or {}).get("numero") or "", (nota or {}).get("lancamento") or ""
    if num and lanc and num != lanc:
        return f"Nota {num} (lanç. {lanc})"
    return f"Nota {num or lanc}" if (num or lanc) else "Nota de vencido"


# ── rodada ───────────────────────────────────────────────────────────────────
def destinos() -> dict:
    conn = _conn()
    try:
        return {r["fornecedor_id"]: r["destino"]
                for r in conn.execute("SELECT fornecedor_id, destino FROM fornecedor_destino")}
    finally:
        conn.close()


def definir_destino(fid: str, destino: str, usuario=None):
    if destino not in DESTINOS:
        return False, "Destino inválido (troca ou perda)."
    conn = _conn()
    try:
        conn.execute("INSERT INTO fornecedor_destino (fornecedor_id, destino, em, por) "
                     "VALUES (?,?,?,?) ON CONFLICT(fornecedor_id) DO UPDATE SET "
                     "destino=excluded.destino, em=excluded.em, por=excluded.por",
                     (fid, destino, v._agora(), usuario))
        v._auditar(conn, "fornecedor", fid, "destino_vencidos", destino, usuario)
        conn.commit()
        return True, "Destino gravado."
    finally:
        conn.close()


def _pendentes(conn, fid=None):
    sql = ("SELECT * FROM vencidos WHERE excluido_em IS NULL AND baixa_status='pendente' "
           "AND COALESCE(nota_situacao, '') IN (?,?,?)")
    args = list(LIVRES)
    if fid is not None:
        sql += " AND fornecedor_id = ?"
        args.append(fid)
    return [dict(r) for r in conn.execute(sql + " ORDER BY criado_em", args)]


def _item(r):
    cb = (r["codigo_barras"] or "").strip()
    return {"id": r["id"], "produto": r["produto"], "codigo_barras": cb,
            "quantidade": r["quantidade"], "fornecedor": r["fornecedor"] or "",
            "situacao": r.get("nota_situacao") or "", "detalhe": r.get("nota_detalhe") or "",
            "no_catalogo": _no_catalogo(cb) if cb else False,
            "sem_codigo": not cb}


def montar_rodada() -> dict:
    """Grupos por fornecedor + o que está travado e por quê."""
    conn = _conn()
    try:
        pend = _pendentes(conn)
        andamento = [dict(r) for r in conn.execute(
            "SELECT * FROM vencidos WHERE excluido_em IS NULL AND nota_situacao IN "
            "('em_andamento','a_conferir') ORDER BY nota_em")]
    finally:
        conn.close()
    dest = destinos()
    grupos, sem_fornecedor = {}, []
    for r in pend:
        fid = r.get("fornecedor_id")
        if not fid:
            sem_fornecedor.append(_item(r))
            continue
        grupos.setdefault(fid, []).append(r)
    saida = []
    for fid, rs in grupos.items():
        f = _fornecedor(fid) or {}
        itens = [_item(r) for r in rs]
        saida.append({
            "fornecedor_id": fid,
            "nome": f.get("nome") or rs[0]["fornecedor"] or "(fornecedor)",
            "numero": f.get("numero") or "",
            "destino": dest.get(fid, DESTINO_PADRAO),
            "itens": itens,
            "incluiveis": sum(1 for i in itens if not i["sem_codigo"]),
        })
    saida.sort(key=lambda g: g["nome"].upper())
    return {"grupos": saida, "sem_fornecedor": sem_fornecedor,
            "andamento": [_item(r) | {"pedido": r.get("nota_pedido") or ""} for r in andamento],
            "maquinas": _maquinas()}


def _maquinas():
    try:
        return [{"nome": m["nome"], "apelido": m["apelido"] or m["nome"], "vivo": m["vivo"]}
                for m in _agente().banco().listar_maquinas()
                if m["tem_token"] and "nf.incluir_itens" in (m["capacidades"] or [])]
    except Exception:
        return []


# ── passo 1: abrir a nota ────────────────────────────────────────────────────
def abrir_nota(fid: str, usuario: str, maquina: str = ""):
    f = _fornecedor(fid)
    if not f:
        return False, "Fornecedor não encontrado no cadastro.", None
    if not f.get("numero"):
        return False, (f"{f['nome']} não tem o nº de fornecedor do RADGe no cadastro. "
                       "Informe o número antes."), None
    try:
        p = _agente().banco().criar_pedido(
            "nf.abrir_ultima", {"fornecedor": f["numero"], "tipo": TIPO_NOTA_VENCIDO},
            usuario, modulo="vencidos", maquina_alvo=maquina)
    except ValueError as e:
        return False, str(e), None
    return True, "Pedido enviado ao agente.", p


# ── passo 2: incluir (depois da conferência da pessoa) ───────────────────────
def incluir(fid: str, pedido_abrir: int, usuario: str, maquina: str = "",
            simular: bool = False):
    """Confere o pedido de abrir, reserva os vencidos e cria o pedido de
    inclusão. Devolve (ok, msg, pedido)."""
    ag = _agente()
    f = _fornecedor(fid)
    if not f or not f.get("numero"):
        return False, "Fornecedor sem nº do RADGe no cadastro.", None
    if destinos().get(fid, DESTINO_PADRAO) != "troca":
        return False, ("Este fornecedor está marcado como PERDA (sem troca): os vencidos dele "
                       "vão para a nota de perda, que ainda não é feita pelo agente."), None
    pa = ag.banco().pedido(int(pedido_abrir))
    if (not pa or pa["tipo"] != "nf.abrir_ultima" or pa["estado"] != "feito"
            or str((pa["params"] or {}).get("fornecedor")) != str(f["numero"])):
        return False, "A nota deste fornecedor ainda não foi aberta pelo agente.", None
    res = pa.get("resultado") or {}
    if not res.get("encontrada"):
        return False, res.get("msg") or "Nenhuma nota aberta encontrada.", None
    nota = res.get("nota") or {}
    conferir = nota.get("lancamento") or nota.get("numero") or ""
    if not conferir:
        return False, "A nota aberta não tem número nem lançamento para conferir.", None

    # reserva: UPDATE condicional -> só entra quem ainda está livre
    marca = f"reserva-{v._uid()}"
    conn = _conn()
    try:
        ids = [r["id"] for r in _pendentes(conn, fid) if (r["codigo_barras"] or "").strip()]
        if not ids:
            return False, "Nenhum vencido deste fornecedor para incluir.", None
        marcas = ",".join("?" * len(ids))
        conn.execute(
            f"UPDATE vencidos SET nota_situacao='em_andamento', nota_pedido=?, nota_em=?, "
            f"nota_detalhe=NULL WHERE id IN ({marcas}) AND baixa_status='pendente' "
            f"AND excluido_em IS NULL AND COALESCE(nota_situacao,'') IN (?,?,?)",
            [marca, v._agora(), *ids, *LIVRES])
        conn.commit()
        itens = [dict(r) for r in conn.execute(
            "SELECT * FROM vencidos WHERE nota_pedido=? ORDER BY criado_em", (marca,))]
    finally:
        conn.close()
    if not itens:
        return False, "Os vencidos deste fornecedor já estão em outra inclusão.", None

    params = {"itens": [{"codigo": r["codigo_barras"].strip(), "qtd": r["quantidade"]}
                        for r in itens],
              "nota": conferir, "simular": bool(simular)}
    try:
        p = ag.banco().criar_pedido("nf.incluir_itens", params, usuario, modulo="vencidos",
                                    maquina_alvo=maquina, autorizado_por=usuario)
    except Exception as e:
        _soltar(marca)
        return False, f"O agente recusou o pedido: {e}", None
    pid = str(p["id"])
    conn = _conn()
    try:
        conn.execute("UPDATE vencidos SET nota_pedido=?, nota_numero=? WHERE nota_pedido=?",
                     (pid, json.dumps(nota, ensure_ascii=False), marca))
        conn.executemany("INSERT OR REPLACE INTO nota_pedido_itens (pedido_id, idx, vencido_id) "
                         "VALUES (?,?,?)", [(pid, i, r["id"]) for i, r in enumerate(itens)])
        for r in itens:
            v._auditar(conn, "vencido", r["id"], "incluir_nota",
                       f"pedido #{pid} ao agente · {_ref_nota(nota)}"
                       + (" · SIMULACAO" if simular else ""), usuario)
        conn.commit()
    finally:
        conn.close()
    return True, f"{len(itens)} item(ns) enviados ao agente.", p


def _soltar(marca):
    conn = _conn()
    try:
        conn.execute("UPDATE vencidos SET nota_situacao=NULL, nota_pedido=NULL, nota_em=NULL "
                     "WHERE nota_pedido=? AND nota_situacao='em_andamento'", (marca,))
        conn.commit()
    finally:
        conn.close()


# ── passo 3: resultado ───────────────────────────────────────────────────────
def aplicar_resultado(pedido_id) -> dict:
    """Lê o pedido do agente e, se ele terminou, aplica o resultado aos
    vencidos (uma vez só). Devolve o pedido + o que foi aplicado."""
    pid = str(pedido_id)
    p = _agente().banco().pedido(int(pid))
    if not p:
        return {"ok": False, "msg": "Pedido não existe."}
    if p["estado"] in ("pendente", "executando"):
        return {"ok": True, "pedido": p, "aplicado": None}
    res = p.get("resultado") or {}
    autor = p.get("autorizado_por") or p.get("criado_por")
    conn = _conn()
    contagem = {"incluido": 0, "livre": 0, "pulado": 0, "falhou": 0, "a_conferir": 0}
    try:
        mapa = {r["idx"]: r["vencido_id"] for r in conn.execute(
            "SELECT idx, vencido_id FROM nota_pedido_itens WHERE pedido_id=?", (pid,))}
        vivos = {r["id"] for r in conn.execute(
            "SELECT id FROM vencidos WHERE nota_pedido=? AND nota_situacao='em_andamento'",
            (pid,))}
        if not vivos:
            return {"ok": True, "pedido": p, "aplicado": None}   # já aplicado antes
        try:
            nota = json.loads(next(iter(conn.execute(
                "SELECT nota_numero FROM vencidos WHERE nota_pedido=? LIMIT 1", (pid,))))[0] or "{}")
        except (StopIteration, ValueError, TypeError):
            nota = {}
        nota = (res.get("nota") or nota) or {}
        agora = v._agora()

        def marcar(vid, situacao, detalhe=""):
            if vid not in vivos:
                return
            vivos.discard(vid)
            if situacao == "incluido":
                conn.execute(
                    "UPDATE vencidos SET nota_situacao='incluido', nota_detalhe=?, nota_em=?, "
                    "baixa_status='baixado', baixa_tipo='devolucao', baixa_ref=?, baixa_em=?, "
                    "baixa_por=?, atualizado_em=? WHERE id=?",
                    (detalhe or None, agora, _ref_nota(nota), agora, autor, agora, vid))
                v._auditar(conn, "vencido", vid, "baixa",
                           f"{v.TIPOS_BAIXA['devolucao']} · {_ref_nota(nota)} (pedido #{pid})", autor)
                contagem["incluido"] += 1
            elif situacao == "livre":
                conn.execute("UPDATE vencidos SET nota_situacao=NULL, nota_pedido=NULL, "
                             "nota_detalhe=?, nota_em=? WHERE id=?", (detalhe or None, agora, vid))
                contagem["livre"] += 1
            else:
                conn.execute("UPDATE vencidos SET nota_situacao=?, nota_detalhe=?, nota_em=? "
                             "WHERE id=?", (situacao, detalhe or None, agora, vid))
                contagem[situacao] += 1
            if situacao != "incluido":
                v._auditar(conn, "vencido", vid, "nota_" + situacao,
                           f"pedido #{pid}: {detalhe}"[:300], autor)

        if p["estado"] == "feito" and res.get("itens") is not None:
            for i, it in enumerate(res.get("itens") or []):
                vid, sit = mapa.get(i), it.get("situacao")
                det = it.get("detalhe") or ""
                if sit == "gravado":
                    marcar(vid, "incluido", det)
                elif sit == "simulado":
                    marcar(vid, "livre", "simulado (nada gravado)")
                elif sit == "pulado":
                    marcar(vid, "pulado", det)
                elif det.startswith("parei aqui"):
                    marcar(vid, "a_conferir", det)          # estado do ERP incerto
                else:
                    marcar(vid, "falhou", det)
            # itens que o agente nem chegou a tocar (parou antes): livres
            for vid in list(vivos):
                marcar(vid, "livre", "não chegou a ser processado"
                       + (f" ({res.get('interrompido')})" if res.get("interrompido") else ""))
        elif p["estado"] in ("expirado", "cancelado"):
            # nenhuma máquina pegou: o ERP não foi tocado
            for vid in list(vivos):
                marcar(vid, "livre", f"pedido {p['estado']}: {p.get('erro') or ''}")
        else:
            # erro depois de pego: não se sabe o que entrou -> alguém confere
            for vid in list(vivos):
                marcar(vid, "a_conferir", p.get("erro") or "pedido terminou em erro")
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "pedido": p, "aplicado": contagem}


def liberar_conferido(id_vencido: str, usuario=None):
    """A pessoa conferiu a nota no ERP e o item NÃO entrou: volta a pendente.
    (Se entrou, é a baixa manual de sempre.)"""
    conn = _conn()
    try:
        r = conn.execute("UPDATE vencidos SET nota_situacao=NULL, nota_pedido=NULL, "
                         "nota_detalhe=NULL, nota_em=? WHERE id=? AND nota_situacao='a_conferir'",
                         (v._agora(), id_vencido))
        if not r.rowcount:
            return False, "Este vencido não está aguardando conferência."
        v._auditar(conn, "vencido", id_vencido, "nota_liberar",
                   "conferido no ERP: nao entrou na nota", usuario)
        conn.commit()
        return True, "Vencido liberado para a próxima rodada."
    finally:
        conn.close()
