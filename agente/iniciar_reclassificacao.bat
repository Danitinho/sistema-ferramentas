@echo off
rem Agente da reclassificacao: deixe esta janela aberta com o RADGe na tela de Produtos.
rem Para parar o lote na hora, SEGURE a tecla ESC por um segundo.
cd /d "%~dp0.."
python -m agente.reclassificacao
pause
