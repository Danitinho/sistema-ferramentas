"""
scripts/catalogo.py
===================
Catálogo de produtos — espelho do cadastro do ERP, alimentado por um relatório
exportado em TXT ("todos os produtos").

Para que serve: quando alguém bipa um código de barras no controle de vencidos,
o sistema já sabe o nome do produto, o custo e o preço de venda, em vez de
obrigar a digitar tudo à mão.

O que ele NÃO é: fonte da verdade. É uma FOTO do ERP, com a idade da última
importação. Por isso toda consulta devolve `atualizado_em` — um preço de três
meses atrás não pode ser apresentado como se fosse de hoje.

Importar SUBSTITUI o catálogo inteiro (o arquivo é a foto completa do cadastro):
produto que saiu do ERP some daqui também, e nunca sobra preço fantasma de uma
exportação antiga. A troca é feita numa transação só — se o arquivo falhar no
meio, o catálogo anterior continua inteiro.

Armadilhas do arquivo real (medidas no relatório de 08/09/2026, 95.750 linhas):
  • É UTF-16 LE com BOM, não UTF-8.
  • Os campos vêm entre aspas e separados por '|', mas as aspas NÃO têm escape:
    descrições com polegadas (FACAO M0727N 18") fazem o csv padrão juntar
    colunas e ler o preço errado. Por isso o parse é split('|') + remoção das
    aspas externas, nunca csv.reader.
  • Uma linha traz quebra de linha DENTRO do campo — daí o buffer em
    `_registros`, que acumula até fechar os campos do registro.
  • Preço "R$0,00" é produto sem preço no ERP (11.376 sem venda, 346 sem custo).
    Guardamos NULL: gravar 0,00 num vencido subestimaria a perda no relatório.
  • Códigos com zero à esquerda são cadastros DIFERENTES, com preços diferentes
    ('78924345' R$11,99 × '0000078924345' R$9,39). Por isso a busca é exata —
    ver `consultar`.

Camada de lógica: NÃO importa Flask.
"""
import io
import os
import re
import codecs
import sqlite3
import unicodedata
from datetime import datetime

BASE_DIR  = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DADOS_DIR = os.path.join(BASE_DIR, "dados")
BANCO     = os.path.join(DADOS_DIR, "catalogo.db")

SEP = "|"
LOTE = 5000            # tamanho do bloco de INSERT
MAX_BUFFER = 4000      # trava do acumulador de linha quebrada (caracteres)

# Colunas localizadas PELO NOME no cabeçalho, nunca pela posição: se o layout do
# relatório mudar, a importação falha com mensagem clara em vez de ler a coluna
# errada calada (mesma regra do módulo de reclassificação).
COLUNAS = {
    "codigo_barras":  ("barra1",),
    "descricao":      ("descricao",),
    "venda":          ("precokit",),
    "custo":          ("precocustoaquisicao",),
    "codigo_interno": ("codigo",),          # opcional
}
OBRIGATORIAS = ("codigo_barras", "descricao", "venda", "custo")
ROTULO_COLUNA = {"codigo_barras": "Barra1", "descricao": "Descrição",
                 "venda": "PrecoKit", "custo": "PrecoCustoAquisição"}


class ErroPlanilha(Exception):
    """Arquivo ilegível ou com o cabeçalho fora do contrato."""


# ── Banco ─────────────────────────────────────────────────────────────────────
def _conn():
    os.makedirs(DADOS_DIR, exist_ok=True)
    conn = sqlite3.connect(BANCO)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    _init_schema(conn)
    return conn


def _init_schema(conn):
    conn.executescript("""
        -- Espelho do ERP. Sem soft-delete de propósito: a linha não é um fato
        -- histórico, é a foto de agora — quem guarda a história é `importacoes`.
        CREATE TABLE IF NOT EXISTS produtos (
            codigo_barras  TEXT PRIMARY KEY,
            codigo_interno TEXT,
            descricao      TEXT NOT NULL,
            venda          REAL,          -- NULL = sem preço no ERP
            custo          REAL,
            cb_chave       TEXT           -- código sem zeros à esquerda; SÓ para
                                          -- avisar de ambiguidade, nunca para
                                          -- preencher preço (ver `consultar`)
        );
        CREATE INDEX IF NOT EXISTS idx_cat_chave ON produtos(cb_chave);

        -- Trilha de auditoria da importação: quem trocou o catálogo e quando.
        CREATE TABLE IF NOT EXISTS importacoes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            quando     TEXT NOT NULL,
            usuario    TEXT,
            arquivo    TEXT,
            linhas     INTEGER,
            gravados   INTEGER,
            ignorados  INTEGER,
            conflitos  INTEGER,
            sem_venda  INTEGER,
            sem_custo  INTEGER
        );
    """)
    conn.commit()


def _agora():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _fmt_dt(s):
    """'2026-09-08 14:30:00' → '08/09/2026 14:30'."""
    m = re.match(r'^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})', s or "")
    if not m:
        return s or ""
    y, mo, d, h, mi = m.groups()
    return f"{d}/{mo}/{y} {h}:{mi}"


# ── Leitura do arquivo ────────────────────────────────────────────────────────
def _detectar_encoding(cabeca):
    """A exportação de hoje é UTF-16 LE com BOM; as outras formas são rede de
    segurança para o dia em que o ERP mudar de humor."""
    if cabeca.startswith(codecs.BOM_UTF16_LE) or cabeca.startswith(codecs.BOM_UTF16_BE):
        return "utf-16"
    if cabeca.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if len(cabeca) >= 4 and cabeca[1] == 0 and cabeca[3] == 0:
        return "utf-16-le"          # UTF-16 sem BOM
    try:
        cabeca.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1252"


def _abrir(caminho):
    with open(caminho, "rb") as fb:
        enc = _detectar_encoding(fb.read(4))
    return io.open(caminho, encoding=enc, errors="replace", newline="")


def _limpar(campo):
    """Tira espaços e UMA aspa de cada ponta. Aspas internas (as polegadas da
    descrição) ficam onde estão."""
    s = (campo or "").strip()
    if s.startswith('"'):
        s = s[1:]
    if s.endswith('"'):
        s = s[:-1]
    return s.strip()


def _registros(f, ncol):
    """Gera listas de campos já limpos. Acumula linhas até fechar `ncol` campos,
    porque há registro com quebra de linha no meio de um campo."""
    buf = ""
    for bruta in f:
        buf += bruta.rstrip("\r\n")
        if not buf.strip():
            buf = ""
            continue
        partes = buf.split(SEP)
        if len(partes) >= ncol:
            yield [_limpar(p) for p in partes]
            buf = ""
        elif len(buf) > MAX_BUFFER:
            buf = ""    # linha corrompida: descarta em vez de engolir o resto


def _chave_cab(nome):
    """'PrecoCustoAquisição' → 'precocustoaquisicao'."""
    s = unicodedata.normalize("NFKD", _limpar(nome))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _mapear_cabecalho(cab):
    """Posição de cada coluna, pelo nome. Devolve (posicoes, erro)."""
    achadas = {_chave_cab(nome): i for i, nome in enumerate(cab)}
    pos = {}
    for campo, apelidos in COLUNAS.items():
        for a in apelidos:
            if a in achadas:
                pos[campo] = achadas[a]
                break
    faltando = [c for c in OBRIGATORIAS if c not in pos]
    if faltando:
        return None, ("Cabeçalho fora do esperado: falta a coluna "
                      + ", ".join(ROTULO_COLUNA[c] for c in faltando)
                      + ". Colunas encontradas: "
                      + ", ".join(_limpar(c) for c in cab) + ".")
    return pos, None


_SO_DIGITOS = re.compile(r"\D")


def _parse_dinheiro(txt):
    """'R$1.234,56' → 1234.56. Vazio, zero ou negativo → None (sem preço)."""
    s = (txt or "").replace("R$", "").replace(" ", "").strip()
    if not s:
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")   # vírgula decimal, ponto = milhar
    try:
        v = round(float(s), 2)
    except ValueError:
        return None
    return v if v > 0 else None


# ── Importação ────────────────────────────────────────────────────────────────
_SQL_INSERT = ("INSERT OR REPLACE INTO produtos "
               "(codigo_barras, codigo_interno, descricao, venda, custo, cb_chave) "
               "VALUES (?,?,?,?,?,?)")


def importar(caminho, arquivo=None, usuario=None):
    """Substitui o catálogo inteiro pelo conteúdo do arquivo.

    Devolve um resumo com o que entrou e o que foi descartado — a importação
    conta em voz alta, senão um arquivo pela metade passaria por completo."""
    arquivo = arquivo or os.path.basename(caminho)
    f = _abrir(caminho)
    try:
        cabeca = None
        for bruta in f:
            if bruta.strip():
                cabeca = bruta.rstrip("\r\n").split(SEP)
                break
        if not cabeca:
            raise ErroPlanilha("Arquivo vazio.")
        pos, erro = _mapear_cabecalho(cabeca)
        if erro:
            raise ErroPlanilha(erro)
        ncol = max(pos.values()) + 1

        i_cb, i_desc = pos["codigo_barras"], pos["descricao"]
        i_venda, i_custo = pos["venda"], pos["custo"]
        i_cod = pos.get("codigo_interno")

        conn = _conn()
        conn.isolation_level = None            # a transação é conduzida na mão
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("DELETE FROM produtos")

            linhas = gravados = ignorados = conflitos = sem_venda = sem_custo = 0
            vistos = {}
            exemplos = []
            lote = []
            for campos in _registros(f, ncol):
                linhas += 1
                cb = _SO_DIGITOS.sub("", campos[i_cb]) if i_cb < len(campos) else ""
                desc = campos[i_desc] if i_desc < len(campos) else ""
                if not cb or not desc:
                    ignorados += 1          # sem código ou sem nome não serve
                    continue
                venda = _parse_dinheiro(campos[i_venda]) if i_venda < len(campos) else None
                custo = _parse_dinheiro(campos[i_custo]) if i_custo < len(campos) else None
                if venda is None:
                    sem_venda += 1
                if custo is None:
                    sem_custo += 1
                if cb in vistos:
                    # Código repetido no ERP (9 no arquivo de 08/09/2026, com
                    # descrições e preços diferentes). Vence quem TEM preço de
                    # venda — em 3 dos 9 a segunda linha vem com R$0,00, e ficar
                    # com ela deixaria o produto sem preço à toa. Empate: a
                    # última. O número aparece no resumo da importação.
                    conflitos += 1
                    antes_desc, antes_venda = vistos[cb]
                    if antes_desc != desc and len(exemplos) < 5:
                        exemplos.append(f"{cb}: {antes_desc} / {desc}")
                    if venda is None and antes_venda is not None:
                        continue            # a linha que já está no lote é melhor
                else:
                    gravados += 1
                vistos[cb] = (desc, venda)
                cod_int = campos[i_cod] if (i_cod is not None and i_cod < len(campos)) else None
                lote.append((cb, cod_int, desc, venda, custo, cb.lstrip("0") or "0"))
                if len(lote) >= LOTE:
                    conn.executemany(_SQL_INSERT, lote)
                    lote.clear()
            if lote:
                conn.executemany(_SQL_INSERT, lote)

            if gravados == 0:
                conn.execute("ROLLBACK")
                raise ErroPlanilha(
                    "Nenhum produto válido no arquivo — o catálogo anterior foi mantido.")

            quando = _agora()
            conn.execute(
                "INSERT INTO importacoes (quando, usuario, arquivo, linhas, gravados, "
                "ignorados, conflitos, sem_venda, sem_custo) VALUES (?,?,?,?,?,?,?,?,?)",
                (quando, usuario, arquivo, linhas, gravados, ignorados, conflitos,
                 sem_venda, sem_custo))
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
    finally:
        f.close()

    return {
        "ok": True, "arquivo": arquivo, "quando": quando, "quando_fmt": _fmt_dt(quando),
        "linhas": linhas, "gravados": gravados, "ignorados": ignorados,
        "conflitos": conflitos, "conflitos_exemplo": exemplos,
        "sem_venda": sem_venda, "sem_custo": sem_custo,
        "msg": (f"{gravados} produtos no catálogo — {linhas} linhas lidas, "
                f"{ignorados} sem código de barras ou nome, "
                f"{conflitos} códigos repetidos."),
    }


# ── Consulta ──────────────────────────────────────────────────────────────────
def _linha(r, exato):
    return {
        "encontrado": True, "exato": exato, "ambiguo": False,
        "codigo_barras": r["codigo_barras"], "descricao": r["descricao"],
        "venda": r["venda"], "custo": r["custo"],
    }


def consultar(codigo_barras):
    """Produto do catálogo pelo código de barras.

    A busca é EXATA. Zeros à esquerda distinguem cadastros de verdade
    ('78924345' e '0000078924345' são produtos com preços diferentes), então
    normalizar o código encheria o formulário com o preço do cadastro errado.
    Quando o código exato não existe mas há variantes só de zeros:
      • uma única variante → devolve marcada como `exato=False`, e a tela diz
        com qual cadastro casou (a pessoa confere antes de salvar);
      • mais de uma → `ambiguo`, sem preencher nada. Melhor não preencher do
        que preencher o preço de outro produto.
    Devolve sempre um dict, com `atualizado_em` (a idade da foto) junto."""
    cb = _SO_DIGITOS.sub("", str(codigo_barras or ""))
    base = {"encontrado": False, "exato": False, "ambiguo": False, "parecidos": [],
            "atualizado_em": None, "atualizado_fmt": ""}
    if not cb:
        return base
    conn = _conn()
    try:
        ult = conn.execute(
            "SELECT quando FROM importacoes ORDER BY id DESC LIMIT 1").fetchone()
        if ult:
            base["atualizado_em"] = ult["quando"]
            base["atualizado_fmt"] = _fmt_dt(ult["quando"])
        r = conn.execute("SELECT * FROM produtos WHERE codigo_barras = ?", (cb,)).fetchone()
        if r:
            base.update(_linha(r, True))
            return base
        rs = conn.execute("SELECT * FROM produtos WHERE cb_chave = ? LIMIT 5",
                          (cb.lstrip("0") or "0",)).fetchall()
    finally:
        conn.close()
    if len(rs) == 1:
        base.update(_linha(rs[0], False))
    elif len(rs) > 1:
        base["ambiguo"] = True
        base["parecidos"] = [{"codigo_barras": x["codigo_barras"],
                              "descricao": x["descricao"]} for x in rs]
    return base


def status():
    """Estado do catálogo, para a tela de atualização."""
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM produtos").fetchone()[0]
        r = conn.execute("SELECT * FROM importacoes ORDER BY id DESC LIMIT 1").fetchone()
    finally:
        conn.close()
    ultima = None
    if r:
        ultima = dict(r)
        ultima["quando_fmt"] = _fmt_dt(r["quando"])
    return {"total": total, "vazio": total == 0, "ultima": ultima}
