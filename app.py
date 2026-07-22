"""
app.py — Servidor central do sistema de ferramentas.
Execute com: python app.py
Acesse em:  http://localhost:5000  (mesmo PC)
            http://192.168.x.x:5000 (outros dispositivos na rede)
"""
import os
import socket
import importlib

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
    ("scripts.backup_routes",     "backup_bp"),
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

# ── Autenticação ──────────────────────────────────────────────────────────────
# Secret key persistente (sessões sobrevivem a reinícios) + guarda global de
# login. Fail-open: se o módulo de auth falhar em carregar, o sistema segue no
# ar SEM login (um aviso é logado) — melhor que ficar inacessível.
try:
    from datetime import timedelta
    from scripts import auth
    from scripts.auth_routes import instalar_guarda
    app.secret_key = auth.obter_ou_criar_secret()
    app.permanent_session_lifetime = timedelta(days=7)
    instalar_guarda(app)
except Exception as e:  # noqa: BLE001
    print(f"[app] AVISO: login NÃO foi ativado (sistema seguirá sem login): {e}")

# Sobe o agendador de backup em thread daemon (tolerante a falha).
try:
    from scripts import backup
    backup.iniciar_agendador()
except Exception as e:  # noqa: BLE001
    print(f"[app] AVISO: agendador de backup não iniciou: {e}")

@app.template_filter("brl")
def brl_filter(value):
    try:
        return f"R$ {float(value):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "R$ 0,00"
os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
os.makedirs(app.config["OUTPUT_FOLDER"], exist_ok=True)


# ── Arquivos estáticos de assets ─────────────────────────────────────────────
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

@app.route("/cadastro/calculadora/calcular", methods=["POST"])
def calcular_preco():
    data = request.get_json()
    try:
        custo  = float(data["custo"])
        margem = float(data["margem"]) / 100
        taxa   = 0.19 if data.get("origem") == "fora" else 0.05

        custo_com_imposto = custo * (1 + taxa)
        preco             = custo_com_imposto * (1 + margem)

        return jsonify({
            "preco":              round(preco, 2),
            "custo_com_imposto":  round(custo_com_imposto, 2),
            "valor_imposto":      round(custo_com_imposto - custo, 2),
            "valor_margem":       round(preco - custo_com_imposto, 2),
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
