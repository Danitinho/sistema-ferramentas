"""
scripts/backup_routes.py
Blueprint de administração de backups (/sistema/backup).
Página + APIs para: ver status, fazer backup agora e restaurar uma cópia.
"""
from flask import Blueprint, render_template, request, jsonify
from scripts import backup

backup_bp = Blueprint("backup", __name__, url_prefix="/sistema/backup")


@backup_bp.route("/")
def index():
    return render_template("sistema/backup.html", status=backup.status())


@backup_bp.route("/api/status")
def api_status():
    return jsonify({"ok": True, "status": backup.status()})


@backup_bp.route("/api/agora", methods=["POST"])
def api_agora():
    try:
        return jsonify({"ok": True, "resultado": backup.fazer_backup()})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "msg": str(e)})


@backup_bp.route("/api/restaurar", methods=["POST"])
def api_restaurar():
    d = request.get_json(silent=True) or {}
    rel = (d.get("banco") or "").strip()
    arquivo = (d.get("arquivo") or "").strip()
    if not rel or not arquivo:
        return jsonify({"ok": False, "msg": "Informe o banco e a cópia."})
    ok, msg = backup.restaurar(rel, arquivo)
    return jsonify({"ok": ok, "msg": msg})
