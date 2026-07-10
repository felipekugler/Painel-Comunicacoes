"""Página "Histórico de mensagens" — todas as mensagens enviadas pelo painel,
de qualquer caixa. Tabela pesquisável e classificável (client-side)."""

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import envio_service, historico_service

router = APIRouter()


@router.get("/historico", response_class=HTMLResponse)
async def historico(request: Request):
    registros = historico_service.listar_por_meio()  # uma linha por meio
    labels = {m["key"]: m["label"] for m in envio_service.meios_disponiveis()}
    for r in registros:
        r["meios_labels"] = [labels.get(m, m) for m in (r.get("meios") or [])]
    template = request.app.state.jinja.get_template("historico.html")
    return HTMLResponse(template.render(request=request, registros=registros))


@router.post("/api/historico/limpar", response_class=JSONResponse)
async def limpar_historico(payload: dict = Body(...)):
    modo = (payload.get("modo") or "tudo") if isinstance(payload, dict) else "tudo"
    data = (payload.get("data") or "") if isinstance(payload, dict) else ""
    if modo == "ate" and not data:
        return JSONResponse({"ok": False, "erro": "informe a data de corte"}, status_code=400)
    removidos = historico_service.limpar(modo, data)
    return {"ok": True, "removidos": removidos}


@router.post("/api/historico/restaurar", response_class=JSONResponse)
async def restaurar_historico(payload: dict = Body(...)):
    nome = (payload.get("nome") or "") if isinstance(payload, dict) else ""
    res = historico_service.restaurar(nome)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)
