"""
app.py — Servidor central do sistema de ferramentas.
Execute com: python app.py
Acesse em:  http://localhost:5000  (mesmo PC)
            http://192.168.x.x:5000 (outros dispositivos na rede)
"""
import os
import socket

from flask import Flask, render_template, request, jsonify, send_from_directory
from scripts.debitos_routes import debitos_bp
from scripts.layouts_routes import layouts_bp
from scripts.relatorios_routes import relatorios_bp

app = Flask(__name__)
app.register_blueprint(debitos_bp)
app.register_blueprint(layouts_bp)
app.register_blueprint(relatorios_bp)
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["OUTPUT_FOLDER"] = "outputs"
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
        custo   = float(data["custo"])
        margem  = float(data["margem"]) / 100
        imposto = float(data.get("imposto", 0)) / 100

        if (margem + imposto) >= 1:
            return jsonify({"erro": "Margem + imposto não pode ser >= 100%"}), 400

        preco = custo / (1 - margem - imposto)
        lucro = preco - custo
        return jsonify({
            "preco":  round(preco, 2),
            "lucro":  round(lucro, 2),
            "markup": round((lucro / custo) * 100, 2),
        })
    except (KeyError, ValueError) as e:
        return jsonify({"erro": str(e)}), 400


@app.route("/cadastro/placas-hortifruti")
def placas_hortifruti():
    return render_template("cadastro/placas_hortifruti.html")

@app.route("/cadastro/placas-hortifruti/gerar", methods=["POST"])
def gerar_placas_hortifruti():
    return jsonify({"status": "em breve", "mensagem": "Integre aqui seu script de hortifruti."})


@app.route("/cadastro/registro-perda")
def registro_perda():
    return render_template("cadastro/registro_perda.html")

@app.route("/cadastro/registro-perda/salvar", methods=["POST"])
def salvar_perda():
    return jsonify({"status": "em breve", "mensagem": "Integre aqui seu script de registro."})


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
