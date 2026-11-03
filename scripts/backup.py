"""
scripts/backup.py
=================
Backup automático dos bancos SQLite do sistema.

Por que existe:
  Os dados importantes (débitos, pagamentos, produtos vencidos, relatórios)
  moram em arquivos .db num PC só. Sem backup, uma falha de disco apaga o
  histórico de pagamento das empresas. Este módulo tira cópias periódicas e
  seguras dos bancos.

Estratégia:
  • Usa a API de backup ONLINE do SQLite (Connection.backup) — copia de forma
    consistente mesmo com o banco em uso. Nada de copiar o arquivo cru e correr
    o risco de pegar um estado parcial/corrompido.
  • Gera cópias datadas em  backups/<banco>/<banco>_AAAA-MM-DD_HHMM.db.
  • Retenção: mantém as últimas N cópias de cada banco (default 30).
  • A pasta backups/ é o ponto que o cliente do Google Drive para desktop
    sincroniza para a nuvem — assim a cópia sai do PC sem acoplar credencial de
    API do Google dentro do Flask.

Camada de lógica: NÃO importa Flask.
"""
import os
import re
import time
import shutil
import sqlite3
import json
import threading
from datetime import datetime

from scripts import db_util

BASE_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DADOS_DIR  = os.path.join(BASE_DIR, "dados")

# Destino do backup. Por padrão fica em backups/ na raiz do projeto; aponte a
# variável de ambiente BACKUP_DIR para a pasta do Google Drive se quiser gravar
# direto lá.
BACKUP_DIR = os.environ.get("BACKUP_DIR") or os.path.join(BASE_DIR, "backups")

# Quantas cópias manter por banco e de quanto em quanto tempo rodar.
RETENCAO         = int(os.environ.get("BACKUP_RETENCAO", "30"))
INTERVALO_HORAS  = float(os.environ.get("BACKUP_INTERVALO_HORAS", "24"))

# Retenção específica de um banco. `catalogo.db` é um espelho REIMPORTÁVEL do
# cadastro do ERP (~14 MB): 30 cópias seriam ~420 MB de backup para um dado que
# se refaz em segundos reenviando o arquivo. Duas bastam para não ficar sem a
# foto vigente enquanto ninguém reexporta.
RETENCAO_POR_BANCO = {"catalogo.db": 2}

_TS_FMT = "%Y-%m-%d_%H%M"

# Arquivos que não são banco mas sem os quais restaurar a máquina dói: a
# SECRET_KEY (sem ela todo mundo é deslogado) e a CA do HTTPS (sem ela cada
# aparelho teria de instalar um certificado novo). Vão para backups/_config/.
ARQUIVOS_CONFIG = ("secret.key", os.path.join("tls", "ca.crt"), os.path.join("tls", "ca.key"))
RETENCAO_CONFIG = 10
PASTA_CONFIG = "_config"

# O backup é considerado ATRASADO quando a cópia mais nova passou do intervalo
# com esta folga (o agendador conta a partir da subida do serviço).
FOLGA_ATRASO_H = 2
ARQ_ULTIMO = "_ultimo.json"


# ── Descoberta dos bancos ─────────────────────────────────────────────────────
def _bancos():
    """Lista (rel, abspath) de todos os .db dentro de dados/ (recursivo)."""
    encontrados = []
    for raiz, _dirs, arquivos in os.walk(DADOS_DIR):
        for nome in arquivos:
            if nome.lower().endswith(".db"):
                abs_ = os.path.join(raiz, nome)
                rel  = os.path.relpath(abs_, DADOS_DIR)
                encontrados.append((rel, abs_))
    return sorted(encontrados)


def _retencao(rel):
    """Quantas cópias manter deste banco (ver RETENCAO_POR_BANCO)."""
    return RETENCAO_POR_BANCO.get(os.path.basename(rel).lower(), RETENCAO)


def _slug(rel):
    """Nome de pasta seguro a partir do caminho relativo do banco.
    'relatorios\\vendas.db' → 'relatorios__vendas.db'."""
    return re.sub(r"[\\/]+", "__", rel)


# ── Cópia de um banco ─────────────────────────────────────────────────────────
def _verificar(caminho):
    """`PRAGMA quick_check` na CÓPIA: um backup que não abre não é backup, e
    só se descobre isso no dia de restaurar. Devolve None se está bom."""
    conn = sqlite3.connect(f"file:{caminho}?mode=ro", uri=True)
    try:
        r = conn.execute("PRAGMA quick_check").fetchone()
    finally:
        conn.close()
    return None if r and r[0] == "ok" else f"cópia com defeito: {r[0] if r else '?'}"


def _copiar_config(carimbo):
    """Copia os ARQUIVOS_CONFIG que existirem para _config/<carimbo>/."""
    existentes = [rel for rel in ARQUIVOS_CONFIG if os.path.isfile(os.path.join(DADOS_DIR, rel))]
    if not existentes:
        return 0
    base = os.path.join(BACKUP_DIR, PASTA_CONFIG)
    destino = os.path.join(base, carimbo)
    for rel in existentes:
        alvo = os.path.join(destino, rel)
        os.makedirs(os.path.dirname(alvo), exist_ok=True)
        shutil.copy2(os.path.join(DADOS_DIR, rel), alvo)
    pastas = sorted(p for p in os.listdir(base) if os.path.isdir(os.path.join(base, p)))
    for velha in pastas[:-RETENCAO_CONFIG]:
        shutil.rmtree(os.path.join(base, velha), ignore_errors=True)
    return len(existentes)


def _gravar_ultimo(resumo):
    try:
        with open(os.path.join(BACKUP_DIR, ARQ_ULTIMO), "w", encoding="utf-8") as f:
            json.dump({"quando": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                       "copiados": resumo["copiados"], "falhas": resumo["falhas"],
                       "erros": [f"{i['banco']}: {i.get('msg', '')}" for i in resumo["itens"]
                                 if not i["ok"]]}, f, ensure_ascii=False)
    except OSError:
        pass


def _ler_ultimo():
    try:
        with open(os.path.join(BACKUP_DIR, ARQ_ULTIMO), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _copiar_online(origem, destino):
    """Copia um SQLite usando a API de backup online (consistente com o banco
    em uso). Cria o destino."""
    os.makedirs(os.path.dirname(destino), exist_ok=True)
    src = sqlite3.connect(origem)
    try:
        src.execute("PRAGMA busy_timeout = 5000")
        dst = sqlite3.connect(destino)
        try:
            with dst:
                src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def _aplicar_retencao(pasta, prefixo, manter):
    """Mantém apenas as `manter` cópias mais recentes de um banco."""
    if manter <= 0:
        return
    copias = sorted(
        f for f in os.listdir(pasta)
        if f.startswith(prefixo + "_") and f.endswith(".db")
    )
    for velho in copias[:-manter]:
        try:
            os.remove(os.path.join(pasta, velho))
        except OSError:
            pass


def fazer_backup():
    """Faz o backup de todos os bancos. Retorna um resumo do que aconteceu.
    Nunca lança: uma falha num banco não impede os demais."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    carimbo = datetime.now().strftime(_TS_FMT)
    itens = []
    for rel, abs_ in _bancos():
        slug = _slug(rel)
        pasta = os.path.join(BACKUP_DIR, slug)
        destino = os.path.join(pasta, f"{slug}_{carimbo}.db")
        try:
            _copiar_online(abs_, destino)
            defeito = _verificar(destino)
            if defeito:
                # Não entra na retenção: uma cópia ruim não pode empurrar uma
                # boa para fora. Renomeada, para a restauração não oferecê-la.
                os.replace(destino, destino + ".defeito")
                raise RuntimeError(defeito)
            _aplicar_retencao(pasta, slug, _retencao(rel))
            itens.append({
                "banco": rel, "ok": True,
                "arquivo": os.path.basename(destino),
                "tamanho": os.path.getsize(destino),
            })
        except Exception as e:  # noqa: BLE001 — continuar nos demais bancos
            itens.append({"banco": rel, "ok": False, "msg": str(e)})
    try:
        config = _copiar_config(carimbo)
    except Exception as e:  # noqa: BLE001
        config = 0
        itens.append({"banco": PASTA_CONFIG, "ok": False, "msg": str(e)})
    ok = [i for i in itens if i["ok"]]
    resumo = {
        "quando": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "total": len(itens),
        "copiados": len(ok),
        "falhas": len(itens) - len(ok),
        "config": config,
        "destino": BACKUP_DIR,
        "itens": itens,
    }
    _gravar_ultimo(resumo)
    return resumo


# ── Consulta / status ─────────────────────────────────────────────────────────
def _ts_do_arquivo(slug, nome):
    """Extrai o datetime do nome do arquivo de backup, se possível."""
    m = re.search(r"_(\d{4}-\d{2}-\d{2}_\d{4})\.db$", nome)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), _TS_FMT)
    except ValueError:
        return None


def listar_backups(rel=None):
    """Lista as cópias existentes, agrupadas por banco.
    Retorna [{banco, slug, existe_ao_vivo, copias:[{arquivo, quando, tamanho}]}].
    """
    ao_vivo = dict(_bancos())  # rel -> abspath
    grupos = []
    alvos = [rel] if rel else list(ao_vivo.keys())

    # inclui também backups de bancos que não existem mais ao vivo
    if not rel and os.path.isdir(BACKUP_DIR):
        for slug in os.listdir(BACKUP_DIR):
            if slug == PASTA_CONFIG or not os.path.isdir(os.path.join(BACKUP_DIR, slug)):
                continue
            rel_do_slug = slug.replace("__", os.sep)
            if rel_do_slug not in ao_vivo and rel_do_slug not in alvos:
                alvos.append(rel_do_slug)

    for r in alvos:
        slug = _slug(r)
        pasta = os.path.join(BACKUP_DIR, slug)
        copias = []
        if os.path.isdir(pasta):
            for nome in sorted(os.listdir(pasta), reverse=True):
                if not nome.endswith(".db"):
                    continue
                dt = _ts_do_arquivo(slug, nome)
                copias.append({
                    "arquivo": nome,
                    "quando": dt.strftime("%d/%m/%Y %H:%M") if dt else nome,
                    "quando_dt": dt,
                    "tamanho": os.path.getsize(os.path.join(pasta, nome)),
                })
        grupos.append({
            "banco": r,
            "slug": slug,
            "existe_ao_vivo": r in ao_vivo,
            "copias": copias,
        })
    return grupos


def status():
    """Resumo para exibir na tela de administração."""
    grupos = listar_backups()
    total_copias = sum(len(g["copias"]) for g in grupos)
    # A mais nova pela DATA (antes comparava o texto "dd/mm/aaaa", e 31/10
    # ganhava de 03/11).
    datas = [c["quando_dt"] for g in grupos for c in g["copias"] if c.get("quando_dt")]
    ultima_dt = max(datas) if datas else None
    for g in grupos:
        for c in g["copias"]:
            c.pop("quando_dt", None)
    return {
        **saude(ultima_dt),
        "destino": BACKUP_DIR,
        "retencao": RETENCAO,
        "intervalo_horas": INTERVALO_HORAS,
        "total_bancos": len(grupos),
        "total_copias": total_copias,
        "ultima_copia": ultima_dt.strftime("%d/%m/%Y %H:%M") if ultima_dt else None,
        "grupos": grupos,
    }


def saude(ultima_dt=None, agora=None):
    """O backup está em dia? Para o aviso do menu e da tela de backup.
    Atrasado = a cópia mais nova passou do intervalo + FOLGA_ATRASO_H, ou a
    última rodada teve falha."""
    agora = agora or datetime.now()
    if ultima_dt is None:
        datas = []
        if os.path.isdir(BACKUP_DIR):
            for slug in os.listdir(BACKUP_DIR):
                pasta = os.path.join(BACKUP_DIR, slug)
                if slug == PASTA_CONFIG or not os.path.isdir(pasta):
                    continue
                datas += [d for d in (_ts_do_arquivo(slug, n) for n in os.listdir(pasta)
                                      if n.endswith(".db")) if d]
        ultima_dt = max(datas) if datas else None
    ultimo = _ler_ultimo() or {}
    horas = round((agora - ultima_dt).total_seconds() / 3600, 1) if ultima_dt else None
    limite = INTERVALO_HORAS + FOLGA_ATRASO_H
    motivo = None
    if ultima_dt is None:
        motivo = "nenhuma cópia encontrada"
    elif horas > limite:
        motivo = f"última cópia há {horas:.0f} h"
    elif ultimo.get("falhas"):
        motivo = f"{ultimo['falhas']} falha(s) na última rodada"
    return {"atrasado": motivo is not None, "motivo_atraso": motivo,
            "horas_desde_ultima": horas,
            "ultima_iso": ultima_dt.strftime("%Y-%m-%d %H:%M") if ultima_dt else None,
            "erros_ultima_rodada": ultimo.get("erros", [])}


# ── Restauração ───────────────────────────────────────────────────────────────
def restaurar(rel, nome_arquivo):
    """Restaura um banco a partir de uma cópia.

    Segurança:
      • valida que o arquivo pertence à pasta de backup daquele banco (sem
        path traversal);
      • antes de sobrescrever, salva o estado atual em backups/<slug>/_pre_restauracao/
        para que a restauração também seja reversível.

    `rel` é o caminho relativo do banco dentro de dados/ (ex.: 'debitos.db').
    """
    if os.path.basename(nome_arquivo) != nome_arquivo or not nome_arquivo.endswith(".db"):
        return False, "Nome de arquivo inválido."

    slug = _slug(rel)
    pasta = os.path.join(BACKUP_DIR, slug)
    origem = os.path.join(pasta, nome_arquivo)
    if not os.path.isfile(origem):
        return False, "Cópia de backup não encontrada."

    destino = os.path.join(DADOS_DIR, rel)
    os.makedirs(os.path.dirname(destino), exist_ok=True)

    # snapshot do estado atual antes de sobrescrever
    if os.path.exists(destino):
        pre_dir = os.path.join(pasta, "_pre_restauracao")
        os.makedirs(pre_dir, exist_ok=True)
        carimbo = datetime.now().strftime(_TS_FMT)
        try:
            _copiar_online(destino, os.path.join(pre_dir, f"{slug}_{carimbo}.db"))
        except Exception:
            # se o banco atual estiver ilegível, copia o arquivo cru como pôde
            try:
                shutil.copy2(destino, os.path.join(pre_dir, f"{slug}_{carimbo}.db"))
            except OSError:
                pass

    # restaura a cópia por cima do banco ao vivo (via API de backup)
    try:
        _copiar_online(origem, destino)
    except Exception as e:  # noqa: BLE001
        return False, f"Falha ao restaurar: {e}"
    # A cópia pode ter um esquema mais antigo: o próximo acesso migra de novo.
    db_util.esquecer(destino)
    return True, f"Banco '{rel}' restaurado a partir de {nome_arquivo}."


# ── Agendador (thread daemon) ─────────────────────────────────────────────────
_agendador_iniciado = False
_lock = threading.Lock()


def iniciar_agendador(intervalo_horas=None, delay_inicial=15):
    """Sobe uma thread daemon que faz backup logo após iniciar e depois de
    tempos em tempos. Idempotente: chamar duas vezes não cria dois agendadores.
    """
    global _agendador_iniciado
    with _lock:
        if _agendador_iniciado:
            return
        _agendador_iniciado = True

    intervalo = (intervalo_horas or INTERVALO_HORAS) * 3600

    def _loop():
        time.sleep(delay_inicial)
        while True:
            try:
                r = fazer_backup()
                print(f"[backup] {r['copiados']}/{r['total']} bancos copiados "
                      f"em {r['quando']} -> {r['destino']}")
            except Exception as e:  # noqa: BLE001
                print(f"[backup] falha no agendador: {e}")
            time.sleep(intervalo)

    threading.Thread(target=_loop, name="backup-agendador", daemon=True).start()
