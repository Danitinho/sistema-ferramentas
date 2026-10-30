"""
scripts/reclassificacao_revisao.py
Revisão das sugestões de destino da reclassificação. Sem Flask.

A planilha sugeriu o destino de cada produto por PALAVRA-CHAVE na descrição
("termo HAV", "termo IP", "termo CART"...). Acerta muito, mas erra de um jeito
que se repete: Stella Artois caiu em "Cadernos", feijão em "Perfumaria", pá de
brinquedo e pá de ferramenta no mesmo lugar.

Este módulo propõe um destino olhando os VIZINHOS de cada produto:

  * produtos com descrição parecida (palavras em comum, pesadas pela raridade);
  * a semelhança é MULTIPLICADA quando o NCM bate (8 dígitos > 6 > 4). O NCM
    separa o que o nome confunde, mas sozinho não puxa ninguém: NCMs como
    3924 (utensílio plástico) e 6402 (calçado) são largos demais para decidir;
  * cada vizinho vota no destino dele. Confirmado por uma pessoa vale
    `PESO_CONFIRMADO` votos; palpite da planilha vale 1.

O voto da planilha é o ORIGINAL (`sug_*`), nunca o destino já revisado — senão
uma revisão errada viraria evidência para a próxima, e o erro se reforçaria.

O motor só troca a sugestão quando tem certeza (`CONF_MIN` dos votos e
`APOIO_MIN` de evidência). Fora disso, vale a planilha.

Medido em 30/10/2026, prevendo cada um dos 4.186 confirmados sem deixar ele
votar em si mesmo: a planilha acertava 88,8%; com a revisão, 94,8%. Dos 467
que as pessoas tiveram de corrigir à mão, 291 viriam certos; dos 3.719 em que
a planilha estava certa, 42 seriam trocados para pior. Nos 1.838 ativos de 2026
pendentes, troca 167. A confiança continua sendo só indicativa: nada disto
aprova produto nenhum (ver seção 10-B do CLAUDE.md).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict

PESO_CONFIRMADO = 8.0     # um confirmado vale 8 palpites da planilha
SIM_MIN = 0.45            # vizinho abaixo disto não vota (evita "BIG" + "AZUL")
VIZINHOS = 25
BONUS_NCM = ((8, 1.0), (6, 0.6), (4, 0.3))   # semelhança × (1 + bônus)
DF_MAX = 4000             # palavra em mais itens que isto não serve de índice
CONF_MIN = 0.6            # fatia mínima dos votos para trocar
APOIO_MIN = 2.0           # evidência mínima (soma ponderada) para trocar

# Palavras que não dizem o que o produto é.
_VAZIAS = {
    "KG", "G", "GR", "ML", "L", "LT", "UN", "UND", "UNID", "UNI", "CX", "PCT",
    "PC", "PCS", "CM", "MM", "M", "X", "E", "DE", "DA", "DO", "COM", "NO", "NA",
}
_MEDIDA = re.compile(r"\d+[A-Z]{1,3}")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", (s or "").upper())
    return "".join(c for c in s if not unicodedata.combining(c))


def palavras(descricao: str) -> set:
    return {t for t in re.split(r"[^A-Z0-9]+", _norm(descricao))
            if len(t) >= 2 and not t.isdigit() and t not in _VAZIAS
            and not _MEDIDA.fullmatch(t)}


def rotulo_confianca(fatia: float, confirmados: int) -> str:
    """A revisão também precisa de um rótulo, porque é ele que ordena a fila."""
    if fatia >= 0.85 and confirmados >= 2:
        return "ALTA"
    if fatia >= 0.7:
        return "MÉDIA"
    return "BAIXA"


class Motor:
    """Vizinhos mais parecidos votam no destino.

    `base`: lista de dicts com `cod`, `produto`, `destino` (trio) e
    `confirmado` (bool). `ncm`: {cod: ncm}.
    """

    def __init__(self, base: list, ncm: dict):
        self.ncm = ncm
        df = Counter()
        toks = []
        for it in base:
            t = palavras(it["produto"])
            toks.append(t)
            df.update(t)
        n = max(1, len(base))
        self.idf = {t: math.log(n / (1 + d)) for t, d in df.items()}
        self.base = []
        self.indice = defaultdict(list)
        for it, t in zip(base, toks):
            i = len(self.base)
            self.base.append((it["cod"], self._massa(t), ncm.get(it["cod"], ""),
                              tuple(it["destino"]),
                              PESO_CONFIRMADO if it["confirmado"] else 1.0))
            for p in t:
                if df[p] <= DF_MAX:
                    self.indice[p].append(i)

    def _massa(self, t: set) -> float:
        return sum(self.idf.get(p, 0.0) for p in t) or 1.0

    def prever(self, cod: str, produto: str) -> dict | None:
        """Destino mais votado, ou None se nenhum vizinho passou do mínimo."""
        t = palavras(produto)
        massa = self._massa(t)
        meu_ncm = self.ncm.get(cod, "")
        soma = defaultdict(float)
        for p in t:
            peso = self.idf.get(p, 0.0)
            for i in self.indice.get(p, ()):
                soma[i] += peso

        cand = []
        for i, v in soma.items():
            outro_cod, outra_massa, outro_ncm, _, _ = self.base[i]
            if outro_cod == cod:
                continue                      # ninguém vota em si mesmo
            sim = v / math.sqrt(massa * outra_massa)
            if len(meu_ncm) == 8 and len(outro_ncm) == 8:
                for digitos, bonus in BONUS_NCM:
                    if meu_ncm[:digitos] == outro_ncm[:digitos]:
                        sim *= 1 + bonus
                        break
            if sim >= SIM_MIN:
                cand.append((sim, i))
        if not cand:
            return None
        cand.sort(reverse=True)
        votos = defaultdict(float)
        vizinhos = defaultdict(int)
        confirmados = defaultdict(int)
        for sim, i in cand[:VIZINHOS]:
            _, _, _, destino, peso = self.base[i]
            votos[destino] += sim * peso
            vizinhos[destino] += 1
            if peso > 1:
                confirmados[destino] += 1
        total = sum(votos.values())
        destino, apoio = max(votos.items(), key=lambda x: x[1])
        return {"destino": destino, "fatia": apoio / total, "apoio": apoio,
                "vizinhos": vizinhos[destino],
                "de_vizinhos": min(len(cand), VIZINHOS),
                "confirmados": confirmados[destino], "ncm": meu_ncm}


def propor(motor: Motor, alvo: list, rotulo=None, validar=None) -> list:
    """O que a revisão faria com cada item do alvo.

    `alvo`: dicts com `cod`, `produto`, `planilha` (trio original, pode ser
    vazio), `atual` (trio sugerido hoje), `planilha_conf`, `planilha_base`.
    Devolve, para cada item, o destino que DEVE estar sugerido: o do motor
    quando ele tem certeza e discorda da planilha, senão o da planilha. Assim
    rodar de novo também desfaz uma revisão que perdeu o apoio.
    """
    rotulo = rotulo or (lambda d, s, u: f"{d}/{s}/{u}")
    saida = []
    for it in alvo:
        planilha = tuple(it["planilha"]) if all(it["planilha"]) else ("", "", "")
        p = motor.prever(it["cod"], it["produto"])
        troca = (p is not None and p["fatia"] >= CONF_MIN
                 and p["apoio"] >= APOIO_MIN and p["destino"] != planilha)
        if troca and validar is not None:
            ok, _ = validar(*p["destino"])
            troca = ok
        if troca:
            ncm = p["ncm"]
            ncm_txt = (f"; NCM {ncm[:4]}.{ncm[4:6]}.{ncm[6:]}"
                       if len(ncm) == 8 else "")
            conf_txt = (f" ({p['confirmados']} confirmados)"
                        if p["confirmados"] else "")
            antes = (f"A planilha dizia {rotulo(*planilha).split(' › ')[-1]}"
                     if all(planilha) else "Não havia sugestão")
            base = (f"revisada: {p['vizinhos']} de {p['de_vizinhos']} parecidos"
                    f"{conf_txt} estão em "
                    f"{rotulo(*p['destino']).split(' › ')[-1]}{ncm_txt}."
                    f" {antes}")
            saida.append({
                "cod": it["cod"], "produto": it["produto"], "revisado": True,
                "destino": p["destino"], "planilha": planilha,
                "confianca": rotulo_confianca(p["fatia"], p["confirmados"]),
                "base": base, "fatia": round(p["fatia"], 3),
                "confirmados": p["confirmados"]})
        else:
            saida.append({
                "cod": it["cod"], "produto": it["produto"], "revisado": False,
                "destino": planilha, "planilha": planilha,
                "confianca": it["planilha_conf"], "base": it["planilha_base"]})
    return saida
