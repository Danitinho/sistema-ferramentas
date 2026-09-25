"""
scripts/fornecedores_routes.py
Blueprint /fornecedores — página do cadastro central (lista, editar, juntar
duplicados) e as APIs que alimentam o seletor buscar-ou-cadastrar dos outros
módulos.

Sincronização com débitos: quando um fornecedor ganha CNPJ (no cadastro ou
depois), garantimos a linha correspondente em `empresas` no debitos.db —
invariante: fornecedor com CNPJ <=> empresa em débitos.
"""
from flask import Blueprint, request, jsonify, session, render_template
from scripts import fornecedores as fr

fornecedores_bp = Blueprint("fornecedores", __name__, url_prefix="/fornecedores")


def _usuario():
    return session.get("usuario")


def _sincronizar_empresa(antes, depois):
    """Reflete a mudança do fornecedor na projeção `empresas` de débitos:
    cria a empresa quando o fornecedor ganha CNPJ, renomeia e/ou migra a chave
    quando CNPJ/razão social mudam. `antes` pode ser None (fornecedor novo).
    Falha de débitos não impede a mudança no cadastro central."""
    if not depois or not depois.get("cnpj"):
        return
    try:
        from scripts import debitos
        cnpj_antigo = (antes or {}).get("cnpj")
        if cnpj_antigo and debitos.buscar_empresa(cnpj_antigo):
            debitos.editar_empresa(cnpj_antigo, novo_cnpj=depois["cnpj"],
                                   nova_razao=depois["nome"], usuario=_usuario())
        else:
            debitos.adicionar_empresa(depois["cnpj"], depois["nome"], usuario=_usuario())
    except Exception:
        pass


def _sincronizar_vencidos(antes, depois):
    """Propaga o nome novo para as linhas de avisos/vencidos vinculadas."""
    if not antes or not depois or antes.get("nome") == depois.get("nome"):
        return
    try:
        from scripts import vencidos
        vencidos.renomear_fornecedor(depois["id"], depois["nome"], usuario=_usuario())
    except Exception:
        pass


@fornecedores_bp.route("/api/buscar")
def api_buscar():
    q = request.args.get("q", "")
    return jsonify({"ok": True, "fornecedores": fr.listar(q=q, limite=30)})


@fornecedores_bp.route("/api/criar", methods=["POST"])
def api_criar():
    d = request.get_json() or {}
    ok, msg, f = fr.criar(d.get("nome", ""), cnpj=d.get("cnpj"), usuario=_usuario())
    if ok:
        _sincronizar_empresa(None, f)
    return jsonify({"ok": ok, "msg": msg, "fornecedor": f})


@fornecedores_bp.route("/api/<id_forn>/cnpj", methods=["POST"])
def api_definir_cnpj(id_forn):
    d = request.get_json() or {}
    antes = fr.buscar(id_forn)
    ok, msg, f = fr.definir_cnpj(id_forn, d.get("cnpj", ""), usuario=_usuario())
    if ok:
        _sincronizar_empresa(antes, f)
    return jsonify({"ok": ok, "msg": msg, "fornecedor": f})


@fornecedores_bp.route("/api/<id_forn>/nome", methods=["POST"])
def api_editar_nome(id_forn):
    d = request.get_json() or {}
    antes = fr.buscar(id_forn)
    ok, msg, f = fr.editar_nome(id_forn, d.get("nome", ""), usuario=_usuario())
    if ok:
        _sincronizar_empresa(antes, f)
        _sincronizar_vencidos(antes, f)
    return jsonify({"ok": ok, "msg": msg, "fornecedor": f})


@fornecedores_bp.route("/api/<id_forn>/editar", methods=["POST"])
def api_editar(id_forn):
    """Edita nome, CNPJ e/ou número no ERP num passo só. Sincroniza débitos
    (migra a chave se o CNPJ mudou) e vencidos. `numero` só é mexido quando vem
    no corpo — vazio limpa; ausente mantém (o modal de débitos não o envia)."""
    d = request.get_json() or {}
    antes = fr.buscar(id_forn)
    if not antes:
        return jsonify({"ok": False, "msg": "Fornecedor não encontrado."})
    nome = (d.get("nome") or "").strip()
    cnpj = (d.get("cnpj") or "").strip()
    muda_nome = bool(nome) and nome != antes["nome"]
    muda_cnpj = bool(cnpj) and cnpj != (antes["cnpj"] or "")
    muda_numero = "numero" in d and fr._numero(d.get("numero")) != antes["numero"]
    if not (muda_nome or muda_cnpj or muda_numero):
        return jsonify({"ok": True, "msg": "Nada a alterar.", "fornecedor": antes})
    # número primeiro: é o único que pode ser recusado por formato sem ter
    # mexido em nada ainda
    if muda_numero:
        ok, msg, _f = fr.definir_numero(id_forn, d.get("numero"), usuario=_usuario())
        if not ok:
            return jsonify({"ok": False, "msg": msg})
    if muda_nome:
        ok, msg, _f = fr.editar_nome(id_forn, nome, usuario=_usuario())
        if not ok:
            return jsonify({"ok": False, "msg": msg})
    if muda_cnpj:
        ok, msg, _f = fr.definir_cnpj(id_forn, cnpj, usuario=_usuario())
        if not ok:
            return jsonify({"ok": False, "msg": msg})
    depois = fr.buscar(id_forn)
    _sincronizar_empresa(antes, depois)
    _sincronizar_vencidos(antes, depois)
    return jsonify({"ok": True, "msg": "Fornecedor atualizado.", "fornecedor": depois})


# ── Página do cadastro ───────────────────────────────────────────────────────
def _contagens():
    """Lançamentos de cada fornecedor nos outros módulos. Módulo fora do ar só
    deixa a coluna vazia."""
    venc, deb = {}, {}
    try:
        from scripts import vencidos
        venc = vencidos.contar_por_fornecedor()
    except Exception:
        pass
    try:
        from scripts import debitos
        deb = debitos.contar_debitos_por_empresa()
    except Exception:
        pass
    return venc, deb


def _com_contagens(f, venc, deb):
    v = venc.get(f["id"], {})
    return {**f, "avisos": v.get("avisos", 0), "vencidos": v.get("vencidos", 0),
            "debitos": deb.get(f["cnpj"], 0) if f.get("cnpj") else 0}


@fornecedores_bp.route("/")
def index():
    venc, deb = _contagens()
    lista = [_com_contagens(f, venc, deb) for f in fr.listar_todos()]
    return render_template("fornecedores/index.html", fornecedores=lista)


@fornecedores_bp.route("/api/mesclar/previa", methods=["POST"])
def api_mesclar_previa():
    """Como a junção vai ficar, sem gravar nada — o modal mostra isto antes do
    botão de confirmar."""
    d = request.get_json() or {}
    ok, msg, plano = fr.plano_mescla(d.get("a"), d.get("b"), d.get("nome"))
    if not ok:
        return jsonify({"ok": False, "msg": msg})
    venc, deb = _contagens()
    plano["fica"] = _com_contagens(plano["fica"], venc, deb)
    plano["sai"] = _com_contagens(plano["sai"], venc, deb)
    return jsonify({"ok": True, "plano": plano})


@fornecedores_bp.route("/api/mesclar", methods=["POST"])
def api_mesclar():
    """Junta dois cadastros. Ordem pensada para poder repetir se algo falhar no
    meio: primeiro os lançamentos de vencidos vão para o que fica (repetir não
    move nada de novo), depois o cadastro que sai é encerrado, e por fim a
    empresa de débitos recebe o nome final."""
    d = request.get_json() or {}
    ok, msg, plano = fr.plano_mescla(d.get("a"), d.get("b"), d.get("nome"))
    if not ok:
        return jsonify({"ok": False, "msg": msg})
    try:
        from scripts import vencidos
        movidos = vencidos.transferir_fornecedor(plano["sai"]["id"], plano["fica"]["id"],
                                                 plano["nome"], usuario=_usuario())
        vencidos.renomear_fornecedor(plano["fica"]["id"], plano["nome"], usuario=_usuario())
    except Exception as e:
        return jsonify({"ok": False, "msg": f"Não consegui mover os lançamentos de "
                                            f"vencidos ({e}); nada foi juntado."})
    ok, msg, final = fr.mesclar(plano, usuario=_usuario())
    if not ok:
        return jsonify({"ok": False, "msg": msg})
    if plano["cnpj"]:
        # a empresa de débitos é a do CNPJ que ficou (de um dos dois); só o
        # nome dela pode ter mudado
        _sincronizar_empresa({"cnpj": plano["cnpj"]}, final)
    return jsonify({"ok": True, "msg": msg, "fornecedor": final, "movidos": movidos})
