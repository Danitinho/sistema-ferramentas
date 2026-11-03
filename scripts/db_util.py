"""
scripts/db_util.py
==================
Preparo do esquema SQLite UMA vez por processo.

Os módulos abriam a conexão e rodavam `_init_schema` (CREATE TABLE, ALTER,
backfills) em TODA chamada — o de usuários, em toda requisição, porque a guarda
de login confere as permissões a cada pedido. `preparar` roda o init só na
primeira conexão com aquele arquivo; as seguintes saem direto.

A chave é o CAMINHO do banco, lido na hora da chamada: os testes que apontam um
módulo para uma cópia (trocando `BANCO`) continuam preparando a cópia.

Restaurar um backup troca o arquivo debaixo do processo e pode trazer um
esquema mais antigo — `backup.restaurar` chama `esquecer()` para o próximo
acesso preparar de novo.

Camada de lógica: NÃO importa Flask.
"""
import os
import threading

_prontos = set()
_em_preparo = set()
# Reentrante: o init de um banco pode abrir OUTRO banco (o de vencidos lê o de
# fornecedores no backfill). Com Lock simples a mesma thread travava em si mesma.
_lock = threading.RLock()


def _chave(caminho):
    return os.path.normcase(os.path.abspath(caminho))


def preparar(conn, caminho, init):
    """Roda `init(conn)` se este banco ainda não foi preparado neste processo."""
    chave = _chave(caminho)
    if chave in _prontos:
        return
    with _lock:
        if chave in _prontos or chave in _em_preparo:
            return        # já pronto, ou o próprio init reabrindo o banco
        _em_preparo.add(chave)
        try:
            init(conn)
            _prontos.add(chave)
        finally:
            _em_preparo.discard(chave)


def esquecer(caminho=None):
    """Força o próximo acesso a preparar de novo (um banco, ou todos)."""
    with _lock:
        if caminho is None:
            _prontos.clear()
        else:
            _prontos.discard(_chave(caminho))
