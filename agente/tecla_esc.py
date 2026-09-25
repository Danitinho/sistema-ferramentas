# -*- coding: utf-8 -*-
"""
agente/tecla_esc.py
A tecla de pânico: ESC SEGURADO na máquina do operador para o lote.

Segurado, nunca tocado: o operador aperta ESC o tempo todo para fechar caixa
do ERP, e um toque solto não pode derrubar um lote de 100. A regra é 0,6 s
(`ESC_SEGURAR_S`) com a tecla presa em 4 amostras seguidas. A amostragem só
começa quando a tecla já está pressionada, então o caso comum (ninguém
apertando nada) custa uma chamada de API e mais nada.

Funciona com o ERP em primeiro plano — é justamente aí que importa: quando o
robô está digitando e a tela mostra algo errado, achar a janela do console para
dar Ctrl+C custa segundos, com o pywinauto ainda disputando o foco.
"""
import sys
import time

ESC_SEGURAR_S = 0.6
AMOSTRAS = 4
VK_ESCAPE = 0x1B

if sys.platform == "win32":
    import ctypes
    _GetAsyncKeyState = ctypes.windll.user32.GetAsyncKeyState

    def _esc_preso():
        return bool(_GetAsyncKeyState(VK_ESCAPE) & 0x8000)
else:                                   # fora do Windows (testes): nunca
    def _esc_preso():
        return False


def esc_segurado(preso=None, dormir=time.sleep):
    """True se o ESC está pressionado e continua assim por ESC_SEGURAR_S.
    `preso`/`dormir` existem para os testes."""
    preso = preso or _esc_preso
    if not preso():
        return False
    intervalo = ESC_SEGURAR_S / AMOSTRAS
    for _ in range(AMOSTRAS):
        dormir(intervalo)
        if not preso():
            return False
    return True
