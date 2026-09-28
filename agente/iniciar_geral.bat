@echo off
rem Agente geral do ERP: deixe esta janela aberta com o RADGe aberto.
rem Quem pede as tarefas e o painel /agente. Para interromper um pedido, SEGURE ESC.
cd /d "%~dp0.."
python -m agente.geral
pause
