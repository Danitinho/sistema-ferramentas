# -*- coding: utf-8 -*-
"""
agente/geral.py
Agente GERAL do ERP: programa sem tela na máquina de quem usa o RADGe. Executa
PEDIDOS avulsos feitos pelo sistema (painel /agente, e depois outros módulos),
um de cada vez, e devolve o resultado.

Diferente do agente da reclassificação (lote de milhares de produtos), aqui
cada pedido é uma tarefa curta com uma pessoa esperando o resultado na tela.

Uso (na máquina):
    python -m agente.geral                 (lê agente/config.json)
    python -m agente.geral outro.json

config.json: {"servidor_url": "http://192.168.0.52", "token_geral": "..."}
(`token_geral` é o token da MÁQUINA, gerado em /agente; o `token` do mesmo
arquivo é o do operador da reclassificação, e os dois convivem.)

Capacidades (o agente declara no /api/status; o servidor só entrega o que ele
declarou):
  nf.abrir_ultima {tipo, fornecedor}  abre no ERP a nota ABERTA mais recente
                                      daquele fornecedor e tipo de entrada

Para interromper um pedido no meio, SEGURE ESC por um segundo.
"""
import json
import os
import socket
import sys
import threading
import time

from agente import erp as erp_base
from agente import entradas
from agente.entradas import DriverEntradas, _data, _int
from agente.servidor import Cliente, ErroServidor, TokenInvalido
from agente.tecla_esc import esc_segurado

VERSAO = "1.1"
PASTA = os.path.dirname(os.path.abspath(__file__))


class Interrompido(Exception):
    """ESC segurado na máquina no meio de um pedido."""


class ErroPedido(Exception):
    """O pedido não pôde ser feito; a mensagem vai para quem pediu."""


# ── capacidades ──────────────────────────────────────────────────────────────
def nf_abrir_ultima(ag, params):
    """Abre a nota ABERTA mais recente (pela data de entrada) do fornecedor.

    Passos: tela certa -> Limpar (conferido) -> filtro tipo + fornecedor ->
    busca -> grade -> varre as linhas (lendo cada registro) -> escolhe ->
    posiciona -> volta ao Form -> confere fornecedor e datas.
    """
    tipo = str(params.get("tipo") or "3")
    forn = str(params.get("fornecedor") or "")
    if not forn:
        raise ErroPedido("pedido sem fornecedor")
    drv = ag.driver()
    drv.conferir_tela()
    drv.limpar(obrigatorio=True)
    nome = drv.filtrar(tipo, forn)
    caixa = drv.buscar()
    if caixa:
        ag.log(f"O ERP respondeu à busca: {caixa}")
    drv.abrir_grade()
    linhas = drv.varrer()
    abertas = [l for l in linhas if l["aberta"]]
    ag.log(f"{len(linhas)} nota(s) na lista, {len(abertas)} aberta(s).")
    base = {"tipo": tipo, "fornecedor": forn, "fornecedor_nome": nome,
            "total": len(linhas), "abertas": len(abertas), "linhas": linhas}
    if not abertas:
        drv.abrir_form()
        drv.limpar(obrigatorio=False)
        return {"encontrada": False, **base,
                "msg": "nenhuma nota aberta desse tipo para esse fornecedor"}
    # mais recente = maior data de entrada; empate, o maior lançamento
    nota = max(abertas, key=lambda l: (_data(l["entrada"]), _int(l["lancamento"])))
    drv.ir_para(nota)
    drv.abrir_form()
    conf = drv.ler_campos(["fornecedor", "emissao", "entrada", "lancamento", "numero"])
    divergente = [k for k in ("emissao", "entrada", "lancamento", "numero")
                  if (conf.get(k) or "") != (nota.get(k) or "")]
    if nome and conf.get("fornecedor") and conf["fornecedor"] != nome:
        divergente.append("fornecedor")
    if divergente:
        raise ErroPedido(f"a nota aberta no Form não confere com a escolhida na grade "
                         f"({', '.join(divergente)}): tela {conf}, esperado {nota}")
    ag.log(f"Mais recente: nota {nota['numero']} (lancamento {nota['lancamento']}) "
           f"de {nota['entrada']} (linha {nota['i']}).")
    return {"encontrada": True, **base, "nota": nota,
            "conferencia": {k: conf.get(k) for k in ("fornecedor", "emissao", "entrada")}}


def nf_incluir_itens(ag, params):
    """Inclui itens (código de barras + quantidade em `qtd_caixa`) na nota
    ABERTA carregada na tela Entradas — ou, com `fornecedor`, na última nota
    aberta dele, que é aberta antes.

    Cada item: Incluir (se preciso) -> F9 -> código + Enter -> confere o
    produto da linha -> quantidade (relida) -> Gravar, ou Cancelar se for
    simulação. Item que falha é cancelado e o próximo segue. No fim, sai do
    modo de inclusão sempre.

    Travas: pedido sem `autorizado_por` não roda (o servidor já exige; aqui é a
    segunda chave); `forcar_simulacao` no config.json da máquina vence o
    pedido; nota fechada é recusada antes do Incluir.
    """
    pedido = ag.pedido_atual or {}
    if not pedido.get("autorizado_por"):
        raise ErroPedido("pedido de gravação sem autorização de uma pessoa")
    itens = params.get("itens") or []
    if not itens:
        raise ErroPedido("pedido sem itens")
    simular = bool(params.get("simular")) or ag.forcar_simulacao
    if params.get("fornecedor"):
        aberta = nf_abrir_ultima(ag, params)
        if not aberta.get("encontrada"):
            raise ErroPedido(f"fornecedor {params['fornecedor']}: {aberta.get('msg')}")
    drv = ag.driver()
    drv.conferir_tela()
    nota = drv.nota_atual()
    if not (nota["numero"] or nota["lancamento"]):
        raise ErroPedido("não há nota carregada na tela Entradas: abra a nota antes")
    if not nota["aberta"]:
        raise ErroPedido(f"a nota {nota['numero'] or nota['lancamento']} está FECHADA; "
                         "o ERP não aceita item em nota fechada")
    alvo = str(params.get("nota") or "")
    if alvo and _int(alvo) not in (_int(nota["numero"]), _int(nota["lancamento"])):
        raise ErroPedido(f"a nota na tela é {nota['numero']} (lançamento "
                         f"{nota['lancamento']}), não a {alvo} pedida")
    ag.log(f"Nota {nota['numero'] or nota['lancamento']} ({nota['fornecedor']}) — "
           f"{len(itens)} item(ns)" + (", em SIMULACAO." if simular else "."))
    res = {"nota": nota, "simulado": simular, "total": len(itens), "gravados": 0,
           "simulados": 0, "pulados": 0, "falhados": 0, "itens": [],
           "interrompido": "", "apos_cancelar": None}
    try:
        for n, it in enumerate(itens, 1):
            ag._checar_interrupcao()
            cb, qtd = str(it.get("codigo")), float(it.get("qtd"))
            esperado = it.get("esperado") or {}
            linha = {"codigo": cb, "qtd": qtd}
            res["itens"].append(linha)
            ag.log(f"Item {n}/{len(itens)}: codigo {cb}, qtd {entradas.fmt_qtd(qtd)}.")
            try:
                drv.entrar_inclusao()
                antes = drv.ler_linha_item()
                drv.buscar_produto(cb)
                prod = drv.ler_linha_item()
                # o rótulo da descrição no RADGe não expõe o texto (sai vazio):
                # a descrição mostrada é a do catálogo, conferida pelo código
                linha.update(produto=prod["descricao"] or esperado.get("descricao", ""),
                             codigo_erp=prod["codigo"])
                if not prod["codigo"]:
                    raise entradas.ErroItem("a busca fechou e a linha ficou sem produto")
                if esperado.get("codigo"):
                    if not entradas.mesmo_codigo(prod["codigo"], esperado["codigo"]):
                        raise entradas.ErroItem(
                            f"a linha ficou com outro produto (codigo '{prod['codigo']}', "
                            f"'{prod['descricao']}'); o catálogo espera '{esperado['codigo']}' "
                            f"'{esperado.get('descricao', '')}'")
                    linha["detalhe"] = ""
                else:
                    # sem catálogo: só vale se a linha estava vazia e mudou
                    if antes["codigo"] and entradas.mesmo_codigo(antes["codigo"], prod["codigo"]):
                        raise entradas.ErroItem(
                            f"a linha já tinha o produto '{prod['codigo']}' antes da busca; "
                            "sem o catálogo não dá para saber se a busca pegou")
                    linha["detalhe"] = "sem conferência pelo catálogo"
                drv.escrever_qtd(qtd)
                ag._checar_interrupcao()     # última chance de o ESC impedir a gravação
                if simular:
                    drv.cancelar_item()
                    linha["situacao"] = "simulado"
                    res["simulados"] += 1
                    ag.log("  -> simulado (cancelado, nada gravado)")
                else:
                    linha["apos_gravar"] = drv.gravar_item()
                    linha["situacao"] = "gravado"
                    res["gravados"] += 1
                    ag.log("  -> gravado")
            except entradas.ItemPulado as e:
                # não cadastrado: a caixa já levou Não; registra e segue
                linha.update(situacao="pulado", detalhe=str(e))
                res["pulados"] += 1
                ag.log(f"  -> ERRO, produto nao cadastrado, pulado: {e}")
                drv.cancelar_item()
            except entradas.ErroTela as e:
                # estado do ERP incerto: para a lista aqui, mas devolve o que
                # já foi feito — perder a conta dos gravados seria pior
                linha.update(situacao="falhou", detalhe=f"parei aqui: {e}")
                res["falhados"] += 1
                res["interrompido"] = f"ERP em estado incerto: {e}"
                ag.log(f"  -> PAREI: {e}")
                break
            except entradas.ErroItem as e:
                linha.update(situacao="falhou", detalhe=str(e))
                res["falhados"] += 1
                ag.log(f"  -> falhou: {e}")
                drv.cancelar_item()
    except Interrompido as e:
        res["interrompido"] = str(e)
        ag.log(f"Interrompido: {e}")
    finally:
        # fim da lista: SEMPRE o Cancelar do formulário, para sair do modo de
        # inclusão (pedido do usuário) — e dizer a verdade se não saiu
        try:
            res["apos_cancelar"] = drv.cancelar_item()
            if res["apos_cancelar"]["cancelado"]:
                ag.log("Fora do modo de inclusao.")
            else:
                ag.log("ATENCAO: cliquei em Cancelar e o ERP continua em modo de inclusao; "
                       "clique em Canc na tela.")
        except Exception as e:
            res["apos_cancelar"] = {"cancelado": False, "caixa": f"erro: {e}"}
            ag.log(f"ATENCAO: nao consegui sair do modo de inclusao ({e}); confira o ERP.")
    return res


CAPACIDADES = {
    "nf.abrir_ultima": nf_abrir_ultima,
    "nf.incluir_itens": nf_incluir_itens,
}


# ── o agente ─────────────────────────────────────────────────────────────────
class AgenteGeral:
    def __init__(self, cfg, fabrica_driver=None, esc=esc_segurado, capacidades=None):
        token = cfg.get("token_geral") or cfg.get("token_agente")
        self.cli = Cliente(cfg["servidor_url"], token,
                           msg_401="o servidor recusou o token desta máquina: gere outro "
                                   "no painel /agente e cole em token_geral no config.json")
        self.maquina = socket.gethostname()
        self.fabrica_driver = fabrica_driver or (lambda mapa, log: DriverEntradas(mapa, log))
        self.capacidades = capacidades or CAPACIDADES
        self.esc = esc
        self.mapa_local = cfg.get("mapa_erp")
        # trava da máquina: com ela nada é gravado, diga o pedido o que disser
        self.forcar_simulacao = bool(cfg.get("forcar_simulacao"))
        self.pedido_atual = None
        self.cfg = {}
        self.estado, self.msg = "iniciando", ""
        self.encerrar = False
        self._esc_visto = False
        self._drv = None
        self._mapa_drv = None
        self._log = []
        self._lock = threading.Lock()

    def log(self, texto):
        texto = str(texto)
        try:
            print(time.strftime("%H:%M:%S"), texto, flush=True)
        except Exception:
            pass
        with self._lock:
            self._log.append(texto)

    def status(self, estado=None, msg=None):
        if estado is not None:
            self.estado = estado
        if msg is not None:
            self.msg = msg
        with self._lock:
            lote, self._log = self._log[:50], self._log[50:]
        try:
            cfg = self.cli.post("/agente/api/status", {
                "estado": self.estado, "msg": self.msg, "versao": VERSAO,
                "capacidades": sorted(self.capacidades), "maquina": self.maquina,
                "log": lote})
        except TokenInvalido:
            raise
        except ErroServidor as e:
            with self._lock:
                self._log[:0] = lote
            print("sem contato com o servidor:", e, flush=True)
            return None
        self.cfg = cfg
        return cfg

    def _mapa(self):
        m = self.cfg.get("mapa_erp") or self.mapa_local
        if isinstance(m, str):
            m = json.loads(m) if m.strip() else None
        return m

    def driver(self):
        """Driver da tela, (re)criado quando o mapa muda; conecta se preciso."""
        mapa = self._mapa()
        if not mapa:
            raise ErroPedido("sem mapa do ERP: cole o mapa no painel /agente")
        if self._drv is None or mapa != self._mapa_drv:
            if self._mapa_drv is not None:
                self.log("Mapa do ERP atualizado pelo painel.")
            self._drv = self.fabrica_driver(mapa, self.log)
            self._mapa_drv = mapa
        self._drv.checar = self._checar_interrupcao
        if not self._drv.viva():
            try:
                self.log(f"Conectado ao ERP: {self._drv.conectar()}")
            except erp_base.ErroERP:
                raise ErroPedido("não achei a janela do ERP; abra o RADGe nesta máquina")
        return self._drv

    def _vigiar_esc(self):
        while not self.encerrar:
            if self.esc():
                self._esc_visto = True
                time.sleep(1.5)
            time.sleep(0.05)

    def _checar_interrupcao(self):
        if self._esc_visto:
            raise Interrompido("ESC segurado na maquina")

    def rodar(self):
        self.log(f"Agente geral do ERP iniciado (v{VERSAO}). Quem manda e o painel.")
        threading.Thread(target=self._vigiar_esc, daemon=True).start()
        while not self.encerrar:
            try:
                cfg = self.status()
            except TokenInvalido as e:
                self.log(str(e))
                return
            if cfg is None:
                time.sleep(10)
                continue
            self._esc_visto = False         # ESC fora de pedido não tem o que parar
            if cfg.get("comando") != "ativo":
                if self.estado != "pausado":
                    self.status("pausado", "pausado no painel")
                time.sleep(3)
                continue
            try:
                r = self.cli.post("/agente/api/pegar", {"capacidades": sorted(self.capacidades)})
            except TokenInvalido as e:
                self.log(str(e))
                return
            except ErroServidor as e:
                self.log(f"Não consegui pedir trabalho: {e}")
                time.sleep(10)
                continue
            pedido = r.get("pedido")
            if not pedido:
                if self.estado != "ocioso":
                    self.status("ocioso", "aguardando pedido")
                time.sleep(float(cfg.get("pausa_s") or 2))
                continue
            self.executar(pedido)

    def executar(self, pedido):
        pid, tipo = pedido["id"], pedido["tipo"]
        self.log(f"Pedido #{pid}: {tipo} (de {pedido.get('criado_por') or '?'})")
        self.status("executando", f"pedido #{pid}: {tipo}")
        ok, resultado, erro = False, None, ""
        try:
            fn = self.capacidades.get(tipo)
            if fn is None:
                raise ErroPedido(f"esta máquina não sabe fazer '{tipo}'")
            self._esc_visto = False
            self.pedido_atual = pedido
            resultado = fn(self, pedido.get("params") or {})
            ok = True
        except Interrompido as e:
            erro = f"interrompido: {e}"
        except (ErroPedido, erp_base.ErroERP) as e:
            erro = str(e)
        except Exception as e:              # pywinauto/Win32: handle inválido etc.
            erro = f"erro inesperado: {e!r}"[:500]
            if self._drv is not None:
                self._drv.j.esquecer()
        if ok:
            self.log(f"Pedido #{pid} concluido.")
        else:
            self.log(f"Pedido #{pid} falhou: {erro}")
        for tentativa in range(3):
            try:
                self.cli.post("/agente/api/concluir",
                              {"id": pid, "ok": ok, "resultado": resultado, "erro": erro})
                break
            except TokenInvalido:
                raise
            except ErroServidor as e:
                self.log(f"Não consegui entregar o resultado do #{pid} ({e}); tentando de novo.")
                time.sleep(3)
        self.status("ocioso", "aguardando pedido")


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
              "e preencha servidor_url e token_geral (gerado no painel /agente).")
        return 2
    if not cfg.get("servidor_url") or not (cfg.get("token_geral") or cfg.get("token_agente")):
        print("config.json precisa de servidor_url e token_geral (gerado no painel /agente).")
        return 2
    agente = AgenteGeral(cfg)
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
