"""
app.py — Servidor central do sistema de ferramentas.
Execute com: python app.py
Acesse em:  http://localhost:5000  (mesmo PC)
            http://192.168.x.x:5000 (outros dispositivos na rede)
"""
import os
import socket
import importlib
from decimal import Decimal, ROUND_HALF_UP

from flask import Flask, render_template, request, jsonify, send_from_directory, redirect

app = Flask(__name__)
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["OUTPUT_FOLDER"] = "outputs"


# ── Registro tolerante de módulos ─────────────────────────────────────────────
# Cada módulo (blueprint) é carregado isoladamente. Se um deles tiver erro de
# import/sintaxe, ele é PULADO com um aviso — os demais continuam funcionando.
# Assim, mexer num módulo não derruba o sistema inteiro.
MODULOS = [
    ("scripts.auth_routes",       "auth_bp"),
    ("scripts.debitos_routes",    "debitos_bp"),
    ("scripts.layouts_routes",    "layouts_bp"),
    ("scripts.relatorios_routes", "relatorios_bp"),
    ("scripts.vencidos_routes",   "vencidos_bp"),
    ("scripts.fornecedores_routes", "fornecedores_bp"),
    ("scripts.catalogo_routes",   "catalogo_bp"),
    ("scripts.backup_routes",     "backup_bp"),
    ("scripts.reclassificacao_routes", "reclassificacao_bp"),
    ("scripts.agente_erp_routes", "agente_bp"),
    ("scripts.certificado_routes", "certificado_bp"),  # /certificado (público)
    ("scripts.perdas_routes",     "perdas_bp"),       # /perdas
]

MODULOS_COM_FALHA = []


def registrar_modulos(flask_app):
    for caminho, attr in MODULOS:
        try:
            mod = importlib.import_module(caminho)
            flask_app.register_blueprint(getattr(mod, attr))
        except Exception as e:  # noqa: BLE001 — isolar a falha do módulo
            MODULOS_COM_FALHA.append((caminho, str(e)))
            flask_app.logger.error("Módulo '%s' não carregou: %s", caminho, e)
            print(f"[app] AVISO: módulo '{caminho}' não carregou e foi ignorado: {e}")


registrar_modulos(app)

# ── Limites e cookie de sessão ────────────────────────────────────────────────
# Upload: o maior arquivo legítimo é o relatório completo do cadastro do RADGe
# (168 colunas, ~100k linhas). 256 MB cobre com folga e barra o absurdo.
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024
# SameSite=Lax: o navegador não manda o cookie de sessão em POST disparado por
# outro site (a guarda ainda confere o Origin — ver auth_routes.origem_confiavel).
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True

# ── Autenticação ──────────────────────────────────────────────────────────────
# Secret key persistente (sessões sobrevivem a reinícios) + guarda global de
# login. FAIL-CLOSED (03/11/2026): se o módulo de auth não carregar, as telas
# respondem 503 em vez de abrir débitos e cadastros para a rede sem senha. Só a
# API dos agentes continua — ela tem autenticação própria por token e não
# depende do login. Antes era fail-open.
LOGIN_ATIVO = False
try:
    from datetime import timedelta
    from scripts import auth
    from scripts.auth_routes import instalar_guarda
    app.secret_key = auth.obter_ou_criar_secret()
    app.permanent_session_lifetime = timedelta(days=7)
    instalar_guarda(app)
    LOGIN_ATIVO = True
except Exception as e:  # noqa: BLE001
    print(f"[app] ERRO: login nao carregou; telas bloqueadas (503) ate corrigir: {e!r}")
    app.logger.error("Login não carregou; telas bloqueadas: %r", e)

    @app.before_request
    def _sem_login_fechado():
        ep = request.endpoint or ""
        if ep.startswith(("reclassificacao.api_", "agente.api_")) or ep == "static":
            return None
        if ep == "assets" and (request.view_args or {}).get("filename", "").startswith("fonts/"):
            return None
        msg = ("O login do sistema não carregou, então as telas estão bloqueadas "
               "por segurança. Avise o administrador (detalhe no log do serviço).")
        if "/api/" in request.path:
            return jsonify({"ok": False, "erro": msg}), 503
        return (f"<!doctype html><meta charset='utf-8'><title>Sistema indisponível</title>"
                f"<p style='font:16px sans-serif;max-width:560px;margin:15vh auto'>{msg}</p>"), 503

# Sobe o agendador de backup em thread daemon (tolerante a falha).
try:
    from scripts import backup
    backup.iniciar_agendador()
except Exception as e:  # noqa: BLE001
    print(f"[app] AVISO: agendador de backup não iniciou: {e}")

@app.template_filter("num_br")
def num_br_filter(value, casas=None):
    """Número com vírgula decimal: 2.5 -> "2,5"; casas=1 -> "51,8"."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "—"
    txt = f"{v:,.{casas}f}" if casas is not None else f"{v:,.3f}".rstrip("0").rstrip(".")
    return txt.replace(",", "X").replace(".", ",").replace("X", ".")


@app.template_filter("brl")
def brl_filter(value):
    try:
        return f"R$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "R$ 0,00"
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["OUTPUT_FOLDER"], exist_ok=True)


# ── Arquivos estáticos de assets ─────────────────────────────────────────────
@app.errorhandler(413)
def _upload_grande(_e):
    msg = "Arquivo grande demais (limite de 256 MB)."
    return jsonify({"ok": False, "erro": msg, "mensagem": msg, "msg": msg}), 413


@app.route("/assets/<path:filename>")
def assets(filename):
    return send_from_directory("assets", filename)


# ── Utilitário: descobre o IP local da máquina ────────────────────────────────
def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ════════════════════════════════════════════════════════════════════════════
# MENU PRINCIPAL
# ════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    return render_template("index.html")


# ════════════════════════════════════════════════════════════════════════════
# SETOR: CADASTRO
# ════════════════════════════════════════════════════════════════════════════

@app.route("/cadastro/calculadora")
def calculadora():
    return render_template("cadastro/calculadora.html")

def _rs(v):
    """Centavos no arredondamento comercial (meio centavo sobe), pelo valor
    ESCRITO do número: 9,90 x 1,15 = 11,385 -> 11,39. O round() do Python
    olha o binário (11,38499...) e dava 11,38, um centavo diferente da tela,
    que formata com Intl (calculadora.html)."""
    return float(Decimal(repr(v)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def arredondar_preco(preco, modo):
    """Arredonda PARA CIMA até o final escolhido ("99", "90" ou "49_99"); o
    preço nunca fica abaixo do calculado (não come a margem). Em centavos, para
    não errar 0,01 no float. Mesma conta da tela (calculadora.html)."""
    if not modo:
        return preco
    finais = {"99": (99,), "90": (90,), "49_99": (49, 99)}.get(modo)
    if not finais:
        raise ValueError("arredondamento inválido")
    c = round(preco * 100)
    base = (c // 100) * 100
    while True:
        for f in finais:
            if base + f >= c:
                return (base + f) / 100
        base += 100


@app.route("/cadastro/calculadora/calcular", methods=["POST"])
def calcular_preco():
    data = request.get_json(silent=True) or {}
    try:
        custo  = float(data["custo"])
        margem = float(data["margem"]) / 100
        if custo <= 0 or margem < 0:
            raise ValueError("custo deve ser maior que zero e margem não negativa")
        # "custo" (padrão, markup): preço = custo × (1 + m).
        # "venda": a margem é a fatia do PREÇO — preço = custo ÷ (1 − m).
        tipo   = data.get("tipo_margem") or "custo"
        if tipo not in ("custo", "venda"):
            raise ValueError("tipo_margem deve ser 'custo' ou 'venda'")
        if tipo == "venda" and margem >= 1:
            raise ValueError("margem sobre a venda tem de ficar abaixo de 100%")
        # Acréscimo por origem: dentro do estado 5%, fora 19%, "sem" = só a
        # margem. Origem desconhecida cai em "dentro", como sempre foi.
        taxa   = {"dentro": 0.05, "fora": 0.19, "sem": 0.0}.get(data.get("origem"), 0.05)
        # Desconto no custo (%), opcional; aplicado antes do imposto e da margem.
        desconto = float(data.get("desconto") or 0)
        if not 0 <= desconto < 100:
            raise ValueError("desconto deve estar entre 0 e 99,9%")

        custo_liquido     = custo * (1 - desconto / 100)
        custo_com_imposto = custo_liquido * (1 + taxa)
        preco_calc        = (custo_com_imposto / (1 - margem) if tipo == "venda"
                             else custo_com_imposto * (1 + margem))
        preco             = arredondar_preco(preco_calc, data.get("arredondamento"))

        return jsonify({
            "preco":              _rs(preco),
            "valor_desconto":     _rs(custo - custo_liquido),
            "custo_liquido":      _rs(custo_liquido),
            "custo_com_imposto":  _rs(custo_com_imposto),
            "valor_imposto":      _rs(custo_com_imposto - custo_liquido),
            "valor_margem":       _rs(preco_calc - custo_com_imposto),
            "valor_arredondamento": _rs(preco - preco_calc),
            # as duas leituras do preço final (já arredondado)
            "markup_pct":         round((preco / custo_com_imposto - 1) * 100, 2),
            "margem_venda_pct":   round((1 - custo_com_imposto / preco) * 100, 2),
        })
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400



# Rota legada: o registro de perdas virou o módulo /vencidos.
@app.route("/cadastro/registro-perda")
def registro_perda():
    return redirect("/vencidos")


# ════════════════════════════════════════════════════════════════════════════
# SETOR: LOJA
# ════════════════════════════════════════════════════════════════════════════

@app.route("/loja/lote-vencimento")
def lote_vencimento():
    return render_template("loja/lote_vencimento.html")

@app.route("/loja/lote-vencimento/consultar", methods=["POST"])
def consultar_lote():
    return jsonify({"status": "em breve", "mensagem": "Integre aqui seu script de lote/vencimento."})


# ════════════════════════════════════════════════════════════════════════════
# INICIALIZAÇÃO
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ip = get_local_ip()
    sep = "=" * 52
    print(f"\n{sep}")
    print("  Sistema de Ferramentas -- iniciando...")
    print(sep)
    print(f"  Acesso local:  http://localhost:5000")
    print(f"  Acesso na rede: http://{ip}:5000")
    print("  (compartilhe o endereco da rede com sua equipe)")
    print(f"{sep}\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
