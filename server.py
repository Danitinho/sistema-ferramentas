"""
server.py — entrypoint de PRODUÇÃO (serviço Windows via NSSM).

  • HTTP  na porta 80  pelo waitress (como sempre; os agentes falam por aqui).
  • HTTPS na porta 443 pelo cheroot, com o certificado da CA da loja
    (scripts/certificado.py; página /certificado). Liga sozinho se o cheroot
    e o cryptography estiverem instalados; se algo falhar, o HTTP segue no ar.
    HTTPS_PORTA=0 desliga.

Os dois servidores rodam no MESMO processo, em threads: a fila da
reclassificação e o agente geral dependem de um processo único (seção 10-B,
"o lock serializa dentro de UM processo").
"""
import logging
import os
import threading
from logging.handlers import RotatingFileHandler

from waitress import serve

from app import app

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORTA_HTTP = int(os.environ.get("HTTP_PORTA", "80"))
PORTA_HTTPS = int(os.environ.get("HTTPS_PORTA", "443"))
# As telas consultam o servidor a cada poucos segundos e os agentes também; com
# 4 threads o log enchia de "Task queue depth" (pedido esperando thread livre).
THREADS = int(os.environ.get("SERVIDOR_THREADS", "16"))


def _configurar_log():
    """Erros do app num arquivo com rotação (5 x 5 MB), além do stderr que o
    NSSM já guarda."""
    pasta = os.path.join(BASE_DIR, "logs")
    os.makedirs(pasta, exist_ok=True)
    arq = RotatingFileHandler(os.path.join(pasta, "sistema.log"), maxBytes=5 * 1024 * 1024,
                              backupCount=5, encoding="utf-8")
    arq.setLevel(logging.WARNING)
    arq.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    for nome in ("", "waitress"):
        logging.getLogger(nome).addHandler(arq)
    app.logger.addHandler(arq)


def _subir_https():
    if PORTA_HTTPS <= 0:
        return
    try:
        from cheroot import wsgi
        from cheroot.ssl.builtin import BuiltinSSLAdapter
        from scripts import certificado
        c = certificado.garantir()
        if c["refeito"]:
            msg = (f"[https] certificado do servidor refeito ({c['refeito']}): "
                   f"{', '.join(c['ips'] + c['nomes'])}")
            print(msg.encode("ascii", "replace").decode())   # stdout cp1252 (seção 8)
        srv = wsgi.Server(("0.0.0.0", PORTA_HTTPS), app, numthreads=THREADS,
                          server_name="sistema-ferramentas")
        srv.ssl_adapter = BuiltinSSLAdapter(c["cert"], c["chave"])
        srv.prepare()            # abre a porta AQUI: falhou, nem liga o aviso
    except Exception as e:  # noqa: BLE001 — sem HTTPS, o HTTP continua
        print(f"[https] NAO subiu (o HTTP segue normal): {e!r}")
        app.logger.error("HTTPS não subiu: %r", e)
        return

    def _rodar():
        try:
            srv.serve()
        except Exception as e:  # noqa: BLE001
            app.config["HTTPS_PORTA_ATIVA"] = 0
            print(f"[https] parou: {e!r}")
            app.logger.error("HTTPS parou: %r", e)

    # Só com o HTTPS de pé o celular é mandado para /certificado (base.html).
    # Sem isto, um HTTPS fora do ar trancaria todo celular na página do
    # certificado.
    app.config["HTTPS_PORTA_ATIVA"] = PORTA_HTTPS
    threading.Thread(target=_rodar, name="https", daemon=True).start()
    print(f"[https] https://0.0.0.0:{PORTA_HTTPS}  (certificado: /certificado)")


if __name__ == "__main__":
    _configurar_log()
    _subir_https()
    print(f"Servidor rodando em http://0.0.0.0:{PORTA_HTTP} ({THREADS} threads)")
    serve(app, host="0.0.0.0", port=PORTA_HTTP, threads=THREADS)
