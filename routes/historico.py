"""Página "Histórico de mensagens" — todas as mensagens enviadas pelo painel,
de qualquer caixa. Tabela pesquisável e classificável (client-side)."""

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import divcom_service, envio_service, historico_service

router = APIRouter()


@router.get("/historico", response_class=HTMLResponse)
async def historico(request: Request):
    registros = historico_service.listar_por_meio()  # uma linha por meio
    labels = envio_service.meios_labels()  # inclui rótulos de meios antigos (ex.: e-mail)
    _url_cache: dict[tuple, str] = {}  # backfill da URL de detalhamento (registros antigos)
    for r in registros:
        r["meios_labels"] = [labels.get(m, m) for m in (r.get("meios") or [])]
        if not r.get("url_detalhe"):
            chave = (r.get("caixa_slug"), r.get("paj_norm"))
            if chave not in _url_cache and chave[0] and chave[1]:
                _url_cache[chave] = divcom_service.url_detalhe_do_paj(chave[0], chave[1])
            r["url_detalhe"] = _url_cache.get(chave, "")
    template = request.app.state.jinja.get_template("historico.html")
    # Histórico tem tabela larga → abre com o menu recolhido (libera a largura da tela).
    return HTMLResponse(template.render(request=request, registros=registros,
                                        menu_recolhido=True))


@router.post("/api/historico/{historico_id}/refazer-movimentacao", response_class=JSONResponse)
async def refazer_movimentacao(historico_id: str):
    """Refaz a movimentação que falhou (ex.: após resolver um conflito de sessão)."""
    res = await divcom_service.refazer_movimentacao(historico_id)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/historico/{historico_id}/refazer-tramitacao", response_class=JSONResponse)
async def refazer_tramitacao(historico_id: str):
    """Refaz a tramitação que falhou/não foi realizada (exige movimentação OK antes)."""
    res = await divcom_service.refazer_tramitacao(historico_id)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


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
