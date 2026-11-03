"""
scripts/certificado_routes.py
Página /certificado: baixar a CA da loja e instruções para instalar em cada
aparelho. PÚBLICA de propósito (está em auth_routes.PUBLICOS): o certificado da
CA não é segredo, e quem precisa dele ainda não consegue abrir o sistema por
HTTPS sem aviso. A CHAVE nunca é servida.
"""
import os

from flask import (Blueprint, render_template, send_file, abort, request,
                   current_app, make_response)

from scripts import certificado as cert

certificado_bp = Blueprint("certificado", __name__)

PORTA_HTTPS = int(os.environ.get("HTTPS_PORTA", "443"))


@certificado_bp.app_context_processor
def _https_movel():
    """Para o base.html: o HTTPS está de pé? (`server.py` liga
    HTTPS_PORTA_ATIVA só depois de abrir a porta.) Sem ele, o celular não é
    mandado para /certificado — senão um HTTPS fora do ar trancaria todo
    mundo lá. No `python app.py` (dev) nunca liga."""
    porta = current_app.config.get("HTTPS_PORTA_ATIVA") or 0
    return {"https_movel": {"porta": porta, "sufixo": "" if porta == 443 else f":{porta}"}}


@certificado_bp.route("/certificado/ok")
def ok():
    """O teste do celular: a página em http:// busca ESTA rota por https://.
    Se o aparelho não confia no certificado, o navegador recusa a conexão e o
    fetch falha — é assim que a tela sabe. Pública e sem corpo."""
    r = make_response("", 204)
    r.headers["Cache-Control"] = "no-store"
    return r


@certificado_bp.route("/certificado")
def pagina():
    try:
        dados = cert.info() if cert.disponivel() else {"existe": False}
    except Exception:  # noqa: BLE001 — arquivo ilegível não derruba a página
        dados = {"existe": False}
    host = (request.host or "").split(":")[0]
    sufixo = "" if PORTA_HTTPS == 443 else f":{PORTA_HTTPS}"
    voltar = request.args.get("voltar") or "/"
    if not voltar.startswith("/") or voltar.startswith("//"):
        voltar = "/"
    return render_template("sistema/certificado.html", c=dados,
                           https_url=f"https://{host}{sufixo}/",
                           voltar=voltar, em_https=request.is_secure)


@certificado_bp.route("/certificado/ca.crt")
def baixar():
    if not os.path.isfile(cert.CA_CRT):
        abort(404)
    return send_file(cert.CA_CRT, mimetype="application/x-x509-ca-cert",
                     as_attachment=True, download_name="ca-sistema-ferramentas.crt")
