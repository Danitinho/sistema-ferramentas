# -*- coding: utf-8 -*-
"""
agente/reclassificacao.py
Agente SEM TELA da reclassificação merceológica. Roda na máquina do operador,
com o RADGe aberto na tela de Produtos, e obedece ao painel do navegador
(/reclassificacao): quem liga, pausa e configura é o painel.

Contrato com o servidor: CLAUDE.md, seção 10-B ("Contrato da API"). Mudar
campo lá ou aqui quebra o outro lado em silêncio — mude os dois juntos.

Uso (na máquina do operador):
    python -m agente.reclassificacao            (lê agente/config.json)
    python -m agente.reclassificacao outro.json

config.json: {"servidor_url": "http://192.168.0.52", "token": "..."}.
O resto (bloco, simulação, limiar, mapa do ERP) vem do servidor.

Regras que não podem afrouxar:
  • Antes de escrever no ERP, reconfirma que o item ainda é MEU (/api/item):
    o prazo da reserva pode ter vencido e outra pessoa ter pegado o produto.
  • Descrição da tela diferente da planilha (abaixo do limiar) = não grava.
  • Caixa do ERP durante a digitação NUNCA é respondida com "Sim": "Seção 326
    não está na lista, quer cadastrar?" com Sim criaria uma seção nova.
  • Formulário que não esvazia = para tudo e pede parada no painel: seguir
    com o ERP num estado desconhecido é pior do que parar.
"""
import difflib
import json
import os
import re
import socket
import sys
import threading
import time

from agente.servidor import Cliente, ErroServidor, TokenInvalido
from agente.tecla_esc import esc_segurado
from agente import erp as erp_base

VERSAO = "2.0"
PASTA = os.path.dirname(os.path.abspath(__file__))


class ErroItem(Exception):
    """Este produto não deu certo; o próximo pode dar."""


class ErroGrave(Exception):
    """O ERP ficou num estado em que não dá para continuar sem uma pessoa."""


class Interrompido(Exception):
    """ESC segurado ou ordem do painel no meio de um produto."""


def _norm(s):
    return erp_base.normalizar(s).upper()


def similaridade(a, b):
    return difflib.SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# ── driver da tela de Produtos ───────────────────────────────────────────────
NEGATIVOS = ["Não", "&Não", "Nao", "&Nao", "No", "&No"]
FECHAR = ["OK", "&OK", "Ok", "Fechar", "&Fechar"]
RECUSA = re.compile(r"cancelad|nao pode|nao foi informad|obrigatori|invalid|"
                    r"nao permitid|impossivel|erro|nao esta na lista")


class DriverProdutos:
    """Operações primitivas no cadastro de produtos do RADGe. Não decide nada:
    quem decide se grava é o Agente."""

    def __init__(self, mapa, log):
        self.m = mapa
        self.log = log
        self.c = mapa.get("controles", {})
        self.j = erp_base.Janela(mapa.get("janela_titulo_regex", "RADGe"),
                                 mapa.get("classes_dialogo"))
        self._offset_aba = None

    def _espera(self, chave, padrao):
        time.sleep(float(self.m.get(chave, padrao)))

    def conectar(self):
        titulo = self.j.conectar()
        if "produto" not in erp_base.normalizar(titulo):
            self.log(f"Atenção: a janela do ERP está em '{titulo}'. "
                     "Abra a tela de Produtos.")
        return titulo

    def viva(self):
        return self.j.viva()

    def _botao(self, nome):
        atalho = (self.m.get("atalhos") or {}).get(nome)
        if atalho:
            self.j.teclas(atalho)
        else:
            spec = dict((self.m.get("botoes") or {}).get(nome) or {})
            spec.pop("tipo", None)
            if not spec:
                raise erp_base.ErroERP(f"o mapa do ERP não diz onde fica o botão '{nome}'")
            self.j.clicar(f"botao_{nome}", spec)

    def _fechar_dialogos(self, preferidos):
        """Responde toda caixa aberta com o primeiro botão de `preferidos`.
        Devolve os textos das caixas encontradas."""
        textos = []
        for _ in range(5):
            dlg = self.j.dialogos()
            if not dlg:
                break
            for d in dlg:
                textos.append(d["texto"])
                if not self.j.responder(d, preferidos + FECHAR):
                    raise ErroGrave(f"caixa do ERP sem botão que eu possa apertar: "
                                    f"'{d['texto']}' ({d['botoes']})")
            time.sleep(0.4)
        return textos

    def formulario_vazio(self):
        return not self.j.ler("barra", self.c["barra"]) and \
               not self.j.ler("descricao", self.c["descricao"])

    def limpar(self):
        for tentativa in range(1, 4):
            self._botao("limpar")
            self._espera("espera_limpar_s", 0.8)
            # "Deseja salvar as alterações?" -> Não. Nunca salvar ao limpar.
            for t in self._fechar_dialogos(NEGATIVOS):
                self.log(f"Dialogo do ERP ao limpar: {t}")
            if self.formulario_vazio():
                return
            self.log(f"Limpar não pegou (tentativa {tentativa}); repetindo.")
        raise ErroGrave("O formulário do ERP não esvaziou depois de três tentativas. "
                        "Confira a tela antes de continuar.")

    def buscar(self, cod):
        """Carrega o produto pelo código de barras. None = não achou."""
        self.j.escrever("barra", self.c["barra"], cod,
                        depois=self.m.get("buscar", "{ENTER}"))
        self._espera("espera_busca_s", 1.2)
        # O RADGe pergunta "Quer Acessar o Produto?" ao achar o código: é
        # navegação, e o Sim só abre o cadastro para ver. SÓ essa pergunta leva
        # Sim — "não cadastrado, deseja incluir?" com Sim criaria um produto.
        navegar = [erp_base.normalizar(p) for p in
                   self.m.get("busca_responder_sim", ["acessar o produto"])]
        for _ in range(3):
            dlg = self.j.dialogos()
            if not dlg:
                break
            for d in dlg:
                if any(p in erp_base.normalizar(d["texto"]) for p in navegar):
                    if not self.j.responder(d, ["&Sim", "Sim", "Yes", "&Yes"]):
                        raise ErroGrave(f"não consegui responder '{d['texto']}'")
                else:
                    self.j.responder(d, NEGATIVOS + FECHAR)
                    raise ErroItem(f"o ERP respondeu à busca: {d['texto']}")
            self._espera("espera_busca_s", 1.2)
        desc = self.j.ler("descricao", self.c["descricao"])
        if not desc:
            return None
        return {"descricao": desc, **self._codigos()}

    def _codigos(self):
        return {k: self.j.ler(f"{k}_cod", self.c[f"{k}_cod"]) for k in ("dep", "sec", "sub")}

    def trazer_aba_grupos(self):
        """A aba 'Grupos' é desenhada pelo componente (não é janela própria),
        então o clique é por coordenada na faixa de abas. O deslocamento que
        funcionou fica guardado para os próximos produtos."""
        nome = self.m.get("aba_grupos_nome", "Grupos")
        alvo = erp_base.normalizar(nome)
        folhas = [c for c in self.j._todos() if "tabsheet" in c.class_name().lower()
                  and erp_base.normalizar(c.window_text()) == alvo]
        if not folhas:
            raise ErroItem(f"Nao achei a aba '{nome}' no cadastro. Abra o ERP na tela "
                           "de Produtos; se a aba tiver outro nome, ajuste "
                           "'aba_grupos_nome' no mapa do ERP (painel).")
        folha = folhas[0]
        if folha.is_visible():
            return
        pc = folha.parent()
        pl, pt, pr, pb = erp_base._rect(pc)
        fl, ft, fr, fb = erp_base._rect(folha)
        # a faixa de abas é o lado em que a folha deixa mais espaço no controle
        lados = {"topo": ft - pt, "base": pb - fb, "esq": fl - pl, "dir": pr - fr}
        lado = max(lados, key=lados.get)
        horizontal = lado in ("topo", "base")
        comprimento = (pr - pl) if horizontal else (pb - pt)
        tentativas = [o for o in (self._offset_aba, self.m.get("aba_grupos_offset")) if o]
        tentativas += list(range(12, comprimento - 4, 16))
        for off in tentativas:
            off = int(off)
            if horizontal:
                x = off
                y = (ft - pt) // 2 if lado == "topo" else (fb - pt) + (pb - fb) // 2
            else:
                y = off
                x = (fl - pl) // 2 if lado == "esq" else (fr - pl) + (pr - fr) // 2
            try:
                pc.click(coords=(x, y))
            except Exception:
                continue
            time.sleep(0.25)
            if folha.is_visible():
                if off != self._offset_aba:
                    self.log(f"Aba '{nome}' trazida para a frente (offset {off}).")
                self._offset_aba = off
                return
        raise ErroItem(f"Nao consegui trazer a aba '{nome}' para a frente. Clique nela "
                       "uma vez no ERP e tente de novo.")

    def gravar(self, dep, sec, sub, antes_de_salvar=None):
        """Digita o trio, confere o que o ERP aceitou e salva. Devolve os nomes
        (departamento, seção, subseção) que o ERP mostrou."""
        self.trazer_aba_grupos()
        for k, v in (("dep", dep), ("sec", sec), ("sub", sub)):
            self.j.escrever(f"{k}_cod", self.c[f"{k}_cod"], v)
            time.sleep(0.3)
            caixas = self._fechar_dialogos(NEGATIVOS)
            if caixas:
                raise ErroItem(f"o ERP reclamou do campo {k} ({v}): {' / '.join(caixas)}")
        lido = self._codigos()
        if (lido["dep"], lido["sec"], lido["sub"]) != (str(dep), str(sec), str(sub)):
            raise ErroItem(f"campos nao aceitaram os valores: gravado "
                           f"{lido['dep']}/{lido['sec']}/{lido['sub']}, "
                           f"esperado {dep}/{sec}/{sub}")
        nomes = []
        for k in ("dep", "sec", "sub"):
            try:
                nomes.append(self.j.ler(f"{k}_nome", self.c[f"{k}_nome"]))
            except Exception:
                nomes.append("")
        if antes_de_salvar:
            antes_de_salvar()           # última chance de o ESC impedir a gravação
        self._botao("salvar")
        self._espera("espera_salvar_s", 1.5)
        confirmar = self.m.get("dialogos_confirmar") or ["Sim", "&Sim", "OK", "&OK"]
        for _ in range(5):
            dlg = self.j.dialogos()
            if not dlg:
                break
            for d in dlg:
                if RECUSA.search(erp_base.normalizar(d["texto"])):
                    self.j.responder(d, FECHAR + NEGATIVOS)
                    raise ErroItem(f"o ERP recusou a gravacao: {d['texto']}")
                b = self.j.responder(d, confirmar)
                if not b:
                    raise ErroGrave(f"caixa do ERP ao salvar que não sei responder: "
                                    f"'{d['texto']}' ({d['botoes']})")
                self.log(f"Dialogo do ERP: {d['texto']} (respondi {b})")
            time.sleep(0.5)
        return nomes


# ── o agente ─────────────────────────────────────────────────────────────────
class Agente:
    def __init__(self, cfg, fabrica_driver=None, esc=esc_segurado):
        self.cli = Cliente(cfg["servidor_url"], cfg["token"])
        self.mapa_local = cfg.get("mapa_erp")
        # trava da máquina: com ela, nada é salvo, diga o painel o que disser
        # (máquina de teste, ou a primeira rodada numa instalação nova)
        self.forcar_simulacao = bool(cfg.get("forcar_simulacao"))
        self.maquina = socket.gethostname()
        self.fabrica_driver = fabrica_driver or (lambda mapa, log: DriverProdutos(mapa, log))
        self.esc = esc
        self.drv = None
        self._mapa_drv = None
        self.cfg = {}
        self.estado, self.msg, self.atual = "iniciando", "", ""
        self.feitos = 0
        self.encerrar = False
        self._pedir_parada = False
        self._esc_visto = False
        self._log = []
        self._lock = threading.Lock()

    # ── conversa com o painel ────────────────────────────────────────────────
    def log(self, texto):
        texto = str(texto)
        try:
            print(time.strftime("%H:%M:%S"), texto, flush=True)
        except Exception:
            pass
        with self._lock:
            self._log.append(texto)

    def status(self, estado=None, msg=None):
        """Reporta e recebe a ordem seguinte (uma ida só). None = sem contato."""
        if estado is not None:
            self.estado = estado
        if msg is not None:
            self.msg = msg
        with self._lock:
            lote, self._log = self._log[:50], self._log[50:]
        try:
            cfg = self.cli.post("/reclassificacao/api/status", {
                "estado": self.estado, "msg": self.msg, "feitos": self.feitos,
                "atual": self.atual, "maquina": self.maquina, "log": lote,
                "pedir_parada": self._pedir_parada})
        except TokenInvalido:
            raise
        except ErroServidor as e:
            with self._lock:
                self._log[:0] = lote        # não perde o log: vai na próxima
            print("sem contato com o servidor:", e, flush=True)
            return None
        self._pedir_parada = False
        self.cfg = cfg
        return cfg

    def _mapa(self):
        m = self.cfg.get("mapa_erp") or self.mapa_local
        if isinstance(m, str):
            m = json.loads(m) if m.strip() else None
        return m

    def garantir_erp(self):
        mapa = self._mapa()
        if not mapa:
            self.status("erro", "sem mapa do ERP: cole o mapa no painel")
            return False
        try:
            if self.drv is None or mapa != self._mapa_drv:
                if self._mapa_drv is not None:
                    self.log("Mapa do ERP atualizado pelo painel.")
                self.drv = self.fabrica_driver(mapa, self.log)
                self._mapa_drv = mapa
            if not self.drv.viva():
                self.log(f"Conectado ao ERP: {self.drv.conectar()}")
            return True
        except erp_base.ErroERP as e:
            self.log(f"ERP: {e}. Abra o ERP na tela de Produtos.")
            self.status("erro", str(e))
            return False

    # ── ESC em segundo plano ─────────────────────────────────────────────────
    def _vigiar_esc(self):
        while not self.encerrar:
            if self.esc():
                self._esc_visto = True
                time.sleep(1.5)
            time.sleep(0.05)

    def _checar_interrupcao(self):
        if self._esc_visto:
            raise Interrompido("ESC segurado na maquina")

    # ── laço principal ───────────────────────────────────────────────────────
    def rodar(self):
        self.log(f"Agente da reclassificação iniciado (v{VERSAO}). Quem manda é o painel.")
        if self.forcar_simulacao:
            self.log("SIMULACAO FORCADA nesta maquina (config.json): nada sera salvo no ERP.")
        threading.Thread(target=self._vigiar_esc, daemon=True).start()
        ocioso_msg = None
        while not self.encerrar:
            try:
                cfg = self.status()
            except TokenInvalido as e:
                self.log(str(e))
                return
            if cfg is None:
                time.sleep(10)
                continue
            if self._esc_visto:          # ESC entre blocos: também para
                self._esc_visto = False
                self._pedir_parada = True
                self.log("ESC segurado: pedindo parada ao painel.")
                continue
            comando = cfg.get("comando")
            if comando != "rodar":
                estado = "pausado" if comando == "pausar" else "parado"
                if self.estado != estado:
                    self.status(estado, "aguardando comando")
                time.sleep(3)
                continue
            if not self.garantir_erp():
                time.sleep(10)
                continue
            try:
                r = self.cli.post("/reclassificacao/api/reservar", {
                    "quantidade": int(cfg.get("bloco") or 100),
                    "so_ativos": bool(cfg.get("so_ativos", True)),
                    "confiancas": [], "maquina": self.maquina})
            except ErroServidor as e:
                self.log(f"Não consegui reservar: {e}")
                time.sleep(10)
                continue
            itens = r.get("itens") or []
            if not itens:
                m = "Fila vazia: nada confirmado para mim agora."
                if ocioso_msg != m:
                    self.log(m)
                ocioso_msg = m
                self.status("ocioso", m)
                time.sleep(15)
                continue
            ocioso_msg = None
            self.log(f"Bloco de {len(itens)} produtos"
                     + (f" ({r.get('novos', 0)} novos, o resto retomado)." if r.get("retomado") else "."))
            self.rodar_bloco(itens)

    def rodar_bloco(self, itens):
        ultimo_renovar = time.time()
        feitos_aqui = []
        try:
            for i, item in enumerate(itens):
                self._checar_interrupcao()
                cfg = self.status("rodando", f"produto {i + 1} de {len(itens)}")
                if cfg is None:
                    raise Interrompido("sem contato com o servidor")
                if cfg.get("comando") != "rodar":
                    raise Interrompido(f"painel mandou {cfg.get('comando')}")
                if time.time() - ultimo_renovar > float(cfg.get("batimento_s") or 180):
                    self.cli.post("/reclassificacao/api/renovar")
                    ultimo_renovar = time.time()
                self.processar(item, cfg)
                feitos_aqui.append(item["cod"])
        except Interrompido as e:
            self._parar_por(e, itens, feitos_aqui)
        except ErroGrave as e:
            # O ESC segurado também chega ao ERP, e com a tecla presa o Limpar
            # não pega: a falha é efeito do ESC, não um ERP perdido. O vigia
            # leva ~0,6 s para confirmar a tecla, daí a espera.
            time.sleep(1.0)
            if self._esc_visto:
                self._parar_por(Interrompido("ESC segurado na maquina"), itens, feitos_aqui)
                return
            self.log(f"ERRO: {e}")
            self.log("Nao consegui recuperar o ERP. Soltando o bloco e pedindo parada.")
            self._pedir_parada = True
            self._soltar(itens, feitos_aqui)
            self.status("erro", str(e))
        except ErroServidor as e:
            self.log(f"Servidor: {e}. Soltando o bloco.")
            self._soltar(itens, feitos_aqui)
        finally:
            self.atual = ""

    def _parar_por(self, motivo, itens, feitos):
        self.log(f"Parei: {motivo}.")
        if "ESC" in str(motivo):
            self._pedir_parada = True
            # limpar com a tecla ainda presa falha: espera soltar (até 10 s)
            fim = time.time() + 10
            while self.esc() and time.time() < fim:
                time.sleep(0.2)
            self._esc_visto = False
        if self.drv is not None:
            try:
                self.drv.limpar()
            except Exception as e:
                self.log(f"Não consegui limpar o formulário ({e}); confira a tela do ERP.")
        self._soltar(itens, feitos)
        self.status("parado", str(motivo))

    def _soltar(self, itens, feitos):
        resto = [it["cod"] for it in itens if it["cod"] not in feitos]
        if not resto:
            return
        try:
            n = self.cli.post("/reclassificacao/api/liberar", {"cods": resto}).get("liberados", 0)
            self.log(f"Devolvi {n} produtos nao processados a fila.")
        except ErroServidor as e:
            self.log(f"Não consegui devolver os produtos ({e}); o prazo da reserva "
                     "vai devolvê-los sozinho.")

    def _fechar(self, cod, situacao, dep="", sec="", sub="", detalhe=""):
        r = self.cli.post("/reclassificacao/api/concluir", {
            "cod": cod, "situacao": situacao, "dep": dep, "sec": sec, "sub": sub,
            "detalhe": detalhe})
        if not r.get("ok"):
            self.log(f"[{cod}] o servidor recusou o fechamento: {r.get('motivo')}")
        self.feitos += 1

    def processar(self, item, cfg):
        cod = item["cod"]
        self.atual = cod
        dest = tuple(str(item.get(k) or "") for k in ("dep_novo", "sec_novo", "sub_novo"))
        chk = self.cli.get("/reclassificacao/api/item", cod=cod)
        if not chk.get("meu"):
            self.log(f"[{cod}] não é mais meu ({chk.get('estado')}, "
                     f"{chk.get('operador') or 'sem dono'}): pulei.")
            return
        drv = self.drv
        try:
            drv.limpar()
            achado = drv.buscar(cod)
            if not achado:
                raise ErroItem("produto nao encontrado no ERP")
            sim = similaridade(achado["descricao"], item.get("produto", ""))
            if sim < float(cfg.get("limiar") or 0.8):
                raise ErroItem(f"descricao divergente ({round(sim * 100)}%): ERP "
                               f"'{achado['descricao']}' x planilha '{item.get('produto')}'")
            atual = (achado["dep"], achado["sec"], achado["sub"])
            certo = atual == dest
            if cfg.get("simular") or self.forcar_simulacao:
                det = "ja_correto (simulado)" if certo else f"{'/'.join(dest)} (simulado)"
                self.log(f"[{cod}] simulado: {det}")
                self._fechar(cod, "simulado", *dest, detalhe=det)
                return
            if certo and cfg.get("pular_certos", True):
                self.log(f"[{cod}] ja estava correto ({'/'.join(dest)}).")
                self._fechar(cod, "ja_correto", *dest, detalhe="ja estava correto no ERP")
                return
            nomes = drv.gravar(*dest, antes_de_salvar=self._checar_interrupcao)
            rotulo = " > ".join(f"{c} {n}".strip() for c, n in zip(dest, nomes))
            self.log(f"[{cod}] gravado -> {rotulo}")
            self._fechar(cod, "alterado", *dest, detalhe=rotulo)
        except ErroItem as e:
            self.log(f"[{cod}] nao gravou: {e}")
            self._fechar(cod, "erro", detalhe=str(e))
            drv.limpar()                    # descarta o que ficou digitado
        except Interrompido:
            raise                           # quem limpa é _parar_por, com o ESC já solto
        except erp_base.ErroERP as e:
            self.log(f"[{cod}] ERRO do ERP: {e}")
            self._fechar(cod, "erro", detalhe=str(e))
            if not drv.viva():
                raise ErroGrave("a janela do ERP sumiu")
        except (ErroGrave, ErroServidor):
            raise
        except Exception as e:              # pywinauto/Win32: handle inválido etc.
            self.log(f"[{cod}] ERRO inesperado: {e!r}")
            self._fechar(cod, "erro", detalhe=repr(e)[:300])
            if not drv.viva():
                raise ErroGrave("a janela do ERP sumiu")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    caminho = argv[0] if argv else os.path.join(PASTA, "config.json")
    try:
        with open(caminho, encoding="utf-8") as f:
            cfg = json.load(f)
    except FileNotFoundError:
        print(f"Falta o arquivo {caminho}. Copie config.exemplo.json para config.json "
              "e preencha servidor_url e token (o token é gerado no painel).")
        return 2
    if not cfg.get("servidor_url") or not cfg.get("token"):
        print("config.json precisa de servidor_url e token.")
        return 2
    agente = Agente(cfg)
    try:
        agente.rodar()
    except KeyboardInterrupt:
        agente.encerrar = True
        try:
            agente.status("parado", "agente encerrado")
        except ErroServidor:
            pass
        print("Encerrado.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
