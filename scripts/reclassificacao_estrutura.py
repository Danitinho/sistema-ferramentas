# -*- coding: utf-8 -*-
"""
scripts/reclassificacao_estrutura.py
Estrutura merceológica válida: departamento > seção > subseção.

Por que isto precisa existir DO LADO DO SERVIDOR: os códigos se repetem entre
os três níveis com significados diferentes — o 22 é a seção "Cervejas" e também
a subseção "Bovinos"; o 28 é seção "Casa Geral" e subseção "Alimentos Naturais";
6, 7 e 9 colidem entre departamento e seção. Um trio na ordem errada produz um
cadastro que o ERP aceita sem reclamar e que está semanticamente errado.

Quem grava é a API, não o `<select>` do navegador. A tela ajuda o curador a
escolher certo; esta classe é quem **impede** o errado de entrar.

O arquivo `dados/estrutura_grupos.json` é o mesmo do programa de mesa. Se ele
sumir, o módulo continua de pé com a estrutura vazia e `validar` reprova tudo
com mensagem clara — melhor recusar a curadoria do que aceitar trio não
verificado.
"""
from __future__ import annotations

import json
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
ARQ_PADRAO = RAIZ / "dados" / "estrutura_grupos.json"


class Estrutura:
    def __init__(self, caminho: Path | str = ARQ_PADRAO):
        self.caminho = Path(caminho)
        self.erro = ""
        self.departamentos: dict[str, str] = {}
        self.secoes: dict[str, dict] = {}
        self.subsecoes: dict[str, dict] = {}
        try:
            dados = json.loads(self.caminho.read_text(encoding="utf-8"))
            self.departamentos = {str(k): v for k, v in dados["departamentos"].items()}
            self.secoes = {str(k): v for k, v in dados["secoes"].items()}
            self.subsecoes = {str(k): v for k, v in dados["subsecoes"].items()}
        except Exception as e:  # noqa: BLE001
            self.erro = f"não consegui ler {self.caminho.name}: {e}"

    @property
    def carregada(self) -> bool:
        return bool(self.subsecoes)

    # ── consultas ────────────────────────────────────────────────────────────
    def nome_dep(self, cod: str) -> str:
        return self.departamentos.get(str(cod), "")

    def nome_sec(self, cod: str) -> str:
        return self.secoes.get(str(cod), {}).get("nome", "")

    def nome_sub(self, cod: str) -> str:
        return self.subsecoes.get(str(cod), {}).get("nome", "")

    def lista_departamentos(self) -> list[dict]:
        return [{"cod": c, "nome": n}
                for c, n in sorted(self.departamentos.items(), key=lambda x: x[1])]

    def arvore(self) -> list[dict]:
        """Departamentos > seções > subseções, para montar os seletores da tela.

        Vai inteira para o navegador de uma vez (8 · 35 · 114 = alguns KB): sem
        isso cada troca de departamento seria uma ida ao servidor no meio da
        curadoria, que é justamente o trabalho que precisa ser rápido.
        """
        saida = []
        for dep in self.lista_departamentos():
            secoes = []
            for cs, vs in sorted(self.secoes.items(), key=lambda x: x[1]["nome"]):
                if str(vs["dep"]) != dep["cod"]:
                    continue
                subs = [{"cod": cu, "nome": vu["nome"]}
                        for cu, vu in sorted(self.subsecoes.items(),
                                             key=lambda x: x[1]["nome"])
                        if str(vu["sec"]) == cs]
                secoes.append({"cod": cs, "nome": vs["nome"], "subsecoes": subs})
            saida.append({**dep, "secoes": secoes})
        return saida

    # ── validação ────────────────────────────────────────────────────────────
    def validar(self, dep: str, sec: str, sub: str) -> tuple[bool, str]:
        """Confere se o trio existe E se a hierarquia bate. Retorna (ok, motivo)."""
        if not self.carregada:
            return False, (self.erro or "estrutura merceológica não carregada")
        dep, sec, sub = str(dep).strip(), str(sec).strip(), str(sub).strip()

        if dep not in self.departamentos:
            return False, f"departamento {dep} não existe na estrutura"
        if sec not in self.secoes:
            return False, f"seção {sec} não existe na estrutura"
        if sub not in self.subsecoes:
            return False, f"subseção {sub} não existe na estrutura"

        sec_esperada = str(self.subsecoes[sub]["sec"])
        if sec_esperada != sec:
            return False, (
                f"subseção {sub} ({self.nome_sub(sub)}) pertence à seção "
                f"{sec_esperada} ({self.nome_sec(sec_esperada)}), não à {sec}")
        dep_esperado = str(self.secoes[sec]["dep"])
        if dep_esperado != dep:
            return False, (
                f"seção {sec} ({self.nome_sec(sec)}) pertence ao departamento "
                f"{dep_esperado} ({self.nome_dep(dep_esperado)}), não ao {dep}")
        return True, ""

    def completar(self, sub: str) -> tuple[str, str, str] | None:
        """Dada a subseção, devolve o trio correto. A subseção determina tudo."""
        sub = str(sub).strip()
        if sub not in self.subsecoes:
            return None
        sec = str(self.subsecoes[sub]["sec"])
        dep = str(self.secoes[sec]["dep"])
        return dep, sec, sub

    def descrever(self, dep: str, sec: str, sub: str) -> str:
        return (f"{dep} {self.nome_dep(dep)} > {sec} {self.nome_sec(sec)} > "
                f"{sub} {self.nome_sub(sub)}")

    def rotulo_curto(self, dep: str, sec: str, sub: str) -> str:
        """Só os nomes, para caber na linha da lista de curadoria."""
        partes = [self.nome_dep(dep), self.nome_sec(sec), self.nome_sub(sub)]
        return " › ".join(p for p in partes if p)


# Uma instância por processo: o arquivo não muda em runtime e a árvore é lida a
# cada carregamento da tela de curadoria.
_estrutura: Estrutura | None = None


def estrutura() -> Estrutura:
    global _estrutura
    if _estrutura is None:
        _estrutura = Estrutura()
    return _estrutura


def recarregar() -> Estrutura:
    """Depois de substituir o JSON, sem reiniciar o serviço."""
    global _estrutura
    _estrutura = Estrutura()
    return _estrutura
