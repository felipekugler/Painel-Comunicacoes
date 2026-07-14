"""Rotas do 'cadeado' por SENHA-MESTRA (modalidade 3).

- GET  /desbloquear        -> página de desbloqueio (ou configuração inicial)
- POST /api/desbloquear     -> tenta destrancar com a senha-mestra
- POST /api/credenciais     -> define/troca usuário+senha do SISDPU + senha-mestra
- POST /api/bloquear        -> esquece as credenciais da memória (tranca de novo)

Estas rotas são LIVRES (não passam pelo cadeado) — ver o middleware em app.py.
"""

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import credenciais_service

router = APIRouter()


@router.get("/desbloquear", response_class=HTMLResponse)
async def desbloquear_page(request: Request, trocar: int = 0):
    """Mostra a tela de desbloqueio. Se ainda não há cofre (ou trocar=1), mostra o
    formulário de CONFIGURAÇÃO (usuário+senha do SISDPU + criar senha-mestra)."""
    configurado = credenciais_service.configurado()
    modo = "unlock" if (configurado and not trocar) else "setup"
    template = request.app.state.jinja.get_template("desbloquear.html")
    return HTMLResponse(template.render(request=request, modo=modo, configurado=configurado))


@router.post("/api/desbloquear", response_class=JSONResponse)
async def api_desbloquear(payload: dict = Body(...)):
    senha_mestra = (payload or {}).get("senha_mestra", "")
    if not senha_mestra:
        return JSONResponse({"ok": False, "erro": "Informe a senha-mestra."}, status_code=400)
    if credenciais_service.destrancar(senha_mestra):
        return {"ok": True}
    return JSONResponse({"ok": False, "erro": "Senha-mestra incorreta."}, status_code=401)


@router.post("/api/credenciais", response_class=JSONResponse)
async def api_credenciais(payload: dict = Body(...)):
    """Define (primeira vez) ou troca as credenciais + senha-mestra. Sobrescreve o
    cofre e já deixa a sessão destrancada."""
    p = payload or {}
    usuario = (p.get("usuario") or "").strip()
    senha = p.get("senha") or ""
    senha_mestra = p.get("senha_mestra") or ""
    confirma = p.get("senha_mestra_confirma") or ""
    if not usuario or not senha or not senha_mestra:
        return JSONResponse({"ok": False, "erro": "Preencha usuário, senha do SISDPU e senha-mestra."},
                            status_code=400)
    if len(senha_mestra) < 6:
        return JSONResponse({"ok": False, "erro": "A senha-mestra deve ter ao menos 6 caracteres."},
                            status_code=400)
    if senha_mestra != confirma:
        return JSONResponse({"ok": False, "erro": "A confirmação da senha-mestra não confere."},
                            status_code=400)
    try:
        credenciais_service.salvar(usuario, senha, senha_mestra)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)
    return {"ok": True}


@router.post("/api/bloquear", response_class=JSONResponse)
async def api_bloquear():
    """Tranca o painel de novo (esquece as credenciais da memória)."""
    credenciais_service.bloquear()
    return {"ok": True}


@router.post("/api/seguranca/testar-login", response_class=JSONResponse)
async def api_testar_login():
    """Confirma as credenciais ATIVAS do SISDPU fazendo um login real. Retorna
    {ok, status, detalhe} — se falhar por usuário/senha, avisa para conferência."""
    from ingestao import sisdpu_client
    try:
        res = await sisdpu_client.testar_login()
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "status": "erro", "detalhe": str(e)}
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/seguranca/reconectar", response_class=JSONResponse)
async def api_reconectar():
    """Reconecta a sessão do SISDPU do painel (recupera de conflito de sessão). Fecha a
    sessão atual e refaz o login com as credenciais ativas. Retorna {ok, status, detalhe}."""
    from ingestao import sisdpu_client
    try:
        res = await sisdpu_client.reconectar()
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "status": "erro", "detalhe": str(e)}
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/seguranca/status", response_class=JSONResponse)
async def api_status():
    return {"ok": True,
            "configurado": credenciais_service.configurado(),
            "destrancado": credenciais_service.destrancado(),
            "usuario": credenciais_service.get_usuario(),
            "env_legado": credenciais_service.env_tem_credenciais(),
            "conflito": credenciais_service.conexao_conflito(),
            "conflito_detalhe": credenciais_service.conexao_detalhe()}
