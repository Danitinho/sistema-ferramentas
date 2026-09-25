# -*- coding: utf-8 -*-
"""
agente/erp.py
Base comum para controlar a janela do RADGe (Delphi) por Win32, via pywinauto.
Serve ao agente da reclassificação e ao agente geral do ERP.

O MAPA dos campos vem do servidor (painel). Cada controle é descrito por uma
"spec", com uma ou mais destas chaves:

  class_name   classe Win32 do controle (TDBEdit, TcxButton...). Obrigatória,
               exceto quando só há `texto`.
  ordinal      posição entre TODOS os controles daquela classe, na ordem de
               enumeração do Windows, contando os invisíveis. É o que a
               inspeção do painel mostra.
  x, y         um ponto DENTRO do controle, medido do canto superior esquerdo
               da janela principal. Vale o controle cujo retângulo contém o
               ponto (o menor, se houver vários).
  ancora       texto que identifica o campo: o controle da classe pedida que
               está DENTRO de um contêiner com esse texto (group box, painel),
               ou, sem isso, o mais próximo à direita/abaixo de um controle com
               esse texto. Comparação sem acento e sem caixa ("Secao" = "Seção").
  texto        o texto do próprio controle (botões: "Salvar", "Limpar").
  maior        entre os candidatos, o de maior área (grades).

Nada aqui usa o mouse de verdade quando dá para evitar: clique é por mensagem
(`click()`), que funciona com a tela bloqueada. Digitação precisa de foco,
porque o TDBEdit só põe o registro em edição quando recebe tecla — mandar
WM_SETTEXT mudaria o texto sem o ERP gravar nada.
"""
import re
import time
import unicodedata

try:
    from pywinauto import Desktop, handleprops
    from pywinauto.application import Application
    DISPONIVEL = True
except ImportError:                     # máquina sem pywinauto: só os testes rodam
    DISPONIVEL = False


class ErroERP(Exception):
    """Falha ao falar com a janela do ERP (não achou, controle sumiu...)."""


def normalizar(s):
    s = unicodedata.normalize("NFKD", str(s or ""))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s.replace("&", "")).strip().lower()


def _rect(ctrl):
    r = ctrl.rectangle()
    return r.left, r.top, r.right, r.bottom


class Janela:
    """A janela principal do RADGe e a busca de controles dentro dela."""

    def __init__(self, titulo_regex, classes_dialogo=None, formulario=None):
        if not DISPONIVEL:
            raise ErroERP("pywinauto não está instalado nesta máquina "
                          "(pip install pywinauto)")
        self.titulo_regex = titulo_regex
        self.classes_dialogo = classes_dialogo or ["#32770", "TMessageForm"]
        self.app = None
        self.win = None
        self._cache = {}
        # classe do formulário MDI onde procurar (ex.: TfrmProdutos). Com
        # várias telas abertas no RADGe, "Salvar" e "Limpar" existem em cada
        # uma: sem escopo, o clique poderia ir para a tela errada.
        self.formulario = formulario
        self._form = None

    # ── conexão ───────────────────────────────────────────────────────────────
    def conectar(self):
        try:
            self.app = Application(backend="win32").connect(
                title_re=self.titulo_regex, found_index=0, timeout=3)
            self.win = self.app.window(title_re=self.titulo_regex, found_index=0)
            self.win.wait("exists", timeout=3)
        except Exception:
            self.app = self.win = None
            raise ErroERP(f"não encontrei a janela do ERP com o padrão "
                          f"'{self.titulo_regex}'")
        self._cache.clear()
        self._form = None
        self._restaurar_aplicacao()
        return self.titulo()

    def viva(self):
        try:
            return bool(self.win and self.win.exists(timeout=0))
        except Exception:
            return False

    def titulo(self):
        return self.win.window_text() if self.win else ""

    def _restaurar_aplicacao(self):
        """Delphi minimizado: quem fica minimizada é a janela oculta
        `TApplication`, e o formulário principal some (IsWindowVisible=0, e
        todo controle parece invisível). Restaurar o formulário não adianta;
        tem de ser a TApplication do mesmo processo."""
        try:
            pid = self.win.process_id()
            for w in Desktop(backend="win32").windows(process=pid, class_name="TApplication",
                                                     visible_only=False):
                if w.is_minimized():
                    w.restore()
                    time.sleep(1.0)
        except Exception:
            pass

    def trazer_para_frente(self):
        try:
            self._restaurar_aplicacao()
            if self.win.is_minimized():
                self.win.restore()
            self.win.set_focus()
        except Exception as e:
            raise ErroERP(f"não consegui trazer o ERP para a frente ({e}); "
                          "a tela do Windows está bloqueada?")

    def esquecer(self):
        """Limpa os controles guardados — depois de erro de handle inválido."""
        self._cache.clear()
        self._form = None

    # ── localizar ────────────────────────────────────────────────────────────
    def _raiz(self):
        w = self.win.wrapper_object()
        if not self.formulario:
            return w
        try:
            if self._form is not None and handleprops.iswindow(self._form.handle):
                return self._form
        except Exception:
            pass
        forms = w.descendants(class_name=self.formulario)
        if not forms:
            raise ErroERP(f"a tela '{self.formulario}' não está aberta no ERP")
        self._form = forms[0]
        return self._form

    def _todos(self, class_name=None):
        w = self._raiz()
        return w.descendants(class_name=class_name) if class_name else w.descendants()

    def achar(self, nome, spec, visivel=False):
        """Resolve uma spec do mapa num controle. `nome` só serve de chave do
        cache e para a mensagem de erro."""
        c = self._cache.get(nome)
        if c is not None:
            try:
                if handleprops.iswindow(c.handle) and (not visivel or c.is_visible()):
                    return c
            except Exception:
                pass
        c = self._resolver(spec, visivel)
        if c is None:
            raise ErroERP(f"não achei o campo '{nome}' na tela do ERP "
                          f"({ {k: v for k, v in spec.items()} })")
        self._cache[nome] = c
        return c

    def _resolver(self, spec, visivel):
        classe = spec.get("class_name")
        cands = self._todos(classe)
        if "ordinal" in spec and not any(k in spec for k in ("x", "ancora", "texto")):
            i = int(spec["ordinal"])
            return cands[i] if 0 <= i < len(cands) else None
        if visivel or "x" in spec or spec.get("maior"):
            cands = [c for c in cands if c.is_visible()]
        if "min_larg" in spec or "min_alt" in spec:
            def cabe(c):
                l, t, r, b = _rect(c)
                return r - l >= int(spec.get("min_larg", 0)) and b - t >= int(spec.get("min_alt", 0))
            cands = [c for c in cands if cabe(c)]
        if "texto" in spec:
            alvo = normalizar(spec["texto"])
            cands = [c for c in cands if normalizar(c.window_text()) == alvo]
        if "x" in spec and "y" in spec:
            wl, wt, _, _ = _rect(self.win.wrapper_object())
            px, py = wl + int(spec["x"]), wt + int(spec["y"])
            dentro = []
            for c in cands:
                l, t, r, b = _rect(c)
                if l <= px < r and t <= py < b:
                    dentro.append(((r - l) * (b - t), c))
            cands = [c for _, c in sorted(dentro, key=lambda p: p[0])]
        if "ancora" in spec:
            cands = self._por_ancora(cands, spec["ancora"])
        if spec.get("maior"):
            cands = sorted(cands, key=lambda c: -((lambda r: (r[2]-r[0])*(r[3]-r[1]))(_rect(c))))
        if "ordinal" in spec and len(cands) > 1:
            i = int(spec["ordinal"])
            return cands[i] if 0 <= i < len(cands) else None
        return cands[0] if cands else None

    def _por_ancora(self, cands, ancora):
        alvo = normalizar(ancora)
        ancoras = [a for a in self._todos() if normalizar(a.window_text()) == alvo]
        if not ancoras:
            return []
        # 1) dentro de um contêiner com o texto (group box "Departamento")
        dentro = []
        for c in cands:
            p = c
            for _ in range(6):
                try:
                    p = p.parent()
                except Exception:
                    p = None
                if p is None:
                    break
                if any(p.handle == a.handle for a in ancoras):
                    dentro.append(c)
                    break
        if dentro:
            return dentro
        # 2) o mais perto à direita ou abaixo de um rótulo com o texto
        pontuados = []
        for c in cands:
            cl, ct, cr, cb = _rect(c)
            for a in ancoras:
                al, at, ar, ab = _rect(a)
                mesma_linha = ct < ab and cb > at and cl >= al
                abaixo = ct >= at and cl < ar and cr > al
                if mesma_linha:
                    pontuados.append((cl - ar, c))
                elif abaixo:
                    pontuados.append((1000 + ct - ab, c))
        return [c for _, c in sorted(pontuados, key=lambda p: p[0])]

    # ── ler / escrever / clicar ─────────────────────────────────────────────
    def ler(self, nome, spec):
        try:
            return (self.achar(nome, spec).window_text() or "").strip()
        except ErroERP:
            raise
        except Exception as e:
            self.esquecer()
            raise ErroERP(f"não consegui ler '{nome}': {e}")

    def escrever(self, nome, spec, valor, depois="{TAB}", pausa=0.08):
        """Digita `valor` no campo como uma pessoa faria: foco, apaga, digita,
        sai do campo (o TAB é o que dispara a validação/lookup do ERP)."""
        c = self.achar(nome, spec, visivel=True)
        try:
            self.trazer_para_frente()
            c.set_focus()
            c.type_keys("{HOME}+{END}{DEL}", set_foreground=False)
            texto = re.sub(r"([{}()+^%~\[\]])", r"{\1}", str(valor))
            # devagar: o RADGe perdeu o 2o "1" de "11" digitado de uma vez
            c.type_keys(texto, with_spaces=True, set_foreground=False, pause=pausa)
            if depois:
                time.sleep(pausa)
                c.type_keys(depois, set_foreground=False)
        except ErroERP:
            raise
        except Exception as e:
            self.esquecer()
            raise ErroERP(f"não consegui digitar em '{nome}': {e}")

    def habilitado(self, nome, spec):
        try:
            return self.achar(nome, spec, visivel=True).is_enabled()
        except Exception:
            return False

    def clicar(self, nome, spec):
        c = self.achar(nome, spec, visivel=True)
        try:
            c.click()
        except Exception:
            try:
                c.click_input()
            except Exception as e:
                self.esquecer()
                raise ErroERP(f"não consegui clicar em '{nome}': {e}")

    def teclas(self, teclas):
        self.trazer_para_frente()
        self.win.type_keys(teclas, set_foreground=False)

    # ── caixas de diálogo ─────────────────────────────────────────────────────
    def dialogos(self):
        """Caixas abertas pelo ERP agora: [{texto, botoes, janela}]."""
        saida = []
        try:
            pid = self.win.process_id()
            for w in Desktop(backend="win32").windows(process=pid, visible_only=True):
                if w.class_name() not in self.classes_dialogo:
                    continue
                if w.handle == self.win.handle:
                    continue
                partes, botoes = [], []
                for c in w.descendants():
                    t = (c.window_text() or "").strip()
                    if not t:
                        continue
                    if "button" in c.class_name().lower() or c.class_name() in ("TButton", "TcxButton", "TBitBtn"):
                        botoes.append(t)
                    elif t not in partes:       # o Delphi repete o texto em dois controles
                        partes.append(t)
                texto = " ".join(partes) or w.window_text()
                saida.append({"texto": re.sub(r"\s+", " ", texto).strip(),
                              "botoes": botoes, "janela": w})
        except Exception:
            pass
        return saida

    def responder(self, dialogo, preferidos):
        """Aperta o primeiro botão de `preferidos` que existir na caixa.
        Devolve o texto do botão apertado, ou None se nenhum servir."""
        disponiveis = {normalizar(b): b for b in dialogo["botoes"]}
        for p in preferidos:
            b = disponiveis.get(normalizar(p))
            if b is None:
                continue
            try:
                for c in dialogo["janela"].descendants():
                    if (c.window_text() or "").strip() == b:
                        c.click()
                        return b
            except Exception:
                return None
        return None
