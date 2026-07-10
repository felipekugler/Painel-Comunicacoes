"""Rotas das caixas do Painel DIVCOM (multi-caixa).

- GET  /                                  -> redireciona p/ a 1ª caixa habilitada
- GET  /caixa/{slug}                      -> dashboard de uma caixa
- GET  /api/caixa/{slug}/sincronizar/stream -> sync da caixa (SSE, tempo real)
- POST /api/paj/{paj_norm}/whatsapp       -> escolha de WhatsApp (compartilhada)
- POST /api/caixa/{slug}/paj/{paj_norm}/sugestao -> edição da sugestão (por caixa)
"""

import json

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from services import config_service, divcom_service, envio_service

router = APIRouter()


@router.get("/")
async def home():
    caixas = config_service.listar_caixas_habilitadas()
    if caixas:
        return RedirectResponse(f"/caixa/{caixas[0]['slug']}")
    return RedirectResponse("/configuracoes")


@router.get("/caixa/{slug}", response_class=HTMLResponse)
async def dashboard(request: Request, slug: str):
    caixa = config_service.get_caixa(slug)
    if not caixa:
        return RedirectResponse("/configuracoes")
    # Classificação central (tramitações + conjuntos + triagem).
    db = divcom_service.montar_dashboard(slug)
    paragrafos = config_service.paragrafos_para_caixa(slug)
    comunicados = config_service.comunicados_da_caixa(slug)
    template = request.app.state.jinja.get_template("dashboard.html")
    return HTMLResponse(template.render(
        request=request, caixa=caixa,
        triagem=db["triagem"], pendentes=db["pendentes"], fila=db["fila"], enviados=db["enviados"],
        paragrafos=paragrafos, comunicados=comunicados,
        meios_envio=envio_service.meios_disponiveis(),
        envio_modo=config_service.get_envio_modo()))


@router.get("/api/caixa/{slug}/sincronizar/stream")
async def sincronizar_stream(slug: str):
    async def gen():
        try:
            async for ev in divcom_service.sincronizar_stream(slug):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'level': 'erro', 'msg': str(e)}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True, 'resumo': {'ok': False, 'erro': str(e)}})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/sincronizar-todas/stream")
async def sincronizar_todas_stream():
    async def gen():
        try:
            async for ev in divcom_service.sincronizar_todas_stream():
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'level': 'erro', 'msg': str(e)}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True, 'resumo': {'ok': False, 'erro': str(e)}})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/paj/{paj_norm}/whatsapp", response_class=JSONResponse)
async def definir_whatsapp(paj_norm: str, payload: dict = Body(...)):
    numeros = payload.get("numeros", []) if isinstance(payload, dict) else []
    dados = divcom_service.definir_whatsapp(paj_norm, numeros)
    return {"ok": True, "whatsapp_numeros": dados.get("whatsapp_numeros", [])}


@router.post("/api/caixa/{slug}/paj/{paj_norm}/tramite/{id_tramite}/sugestao", response_class=JSONResponse)
async def definir_sugestao(slug: str, paj_norm: str, id_tramite: str, payload: dict = Body(...)):
    dados = divcom_service.definir_sugestao(slug, paj_norm, id_tramite,
                                            payload.get("texto", ""), payload.get("tipo"))
    if not dados:
        return JSONResponse({"ok": False, "erro": "tramitação não encontrada"}, status_code=404)
    return {"ok": True}


@router.post("/api/caixa/{slug}/paj/{paj_norm}/sincronizar", response_class=JSONResponse)
async def sincronizar_paj(slug: str, paj_norm: str):
    """Re-sincroniza apenas um PAJ (detalhamento + análise)."""
    try:
        res = await divcom_service.sincronizar_paj(slug, paj_norm)
        return JSONResponse(res, status_code=200 if res.get("ok") else 502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


@router.post("/api/caixa/{slug}/paj/{paj_norm}/tramite/{id_tramite}/gravar", response_class=JSONResponse)
async def gravar_para_envio(slug: str, paj_norm: str, id_tramite: str, payload: dict = Body(...)):
    """Grava UMA tramitação na fila de envio da caixa (mensagem + meios)."""
    mensagem = payload.get("mensagem", "") if isinstance(payload, dict) else ""
    meios = payload.get("meios", []) if isinstance(payload, dict) else []
    if not meios:
        return JSONResponse({"ok": False, "erro": "Selecione ao menos um meio de contato."},
                            status_code=400)
    dados = divcom_service.gravar_para_envio(slug, paj_norm, id_tramite, mensagem, meios)
    if not dados:
        return JSONResponse({"ok": False, "erro": "tramitação não encontrada"}, status_code=404)
    return {"ok": True}


@router.post("/api/caixa/{slug}/paj/{paj_norm}/tramite/{id_tramite}/gravar/remover", response_class=JSONResponse)
async def remover_da_fila(slug: str, paj_norm: str, id_tramite: str):
    """Tira uma tramitação da fila de envio (volta a pendente)."""
    dados = divcom_service.remover_da_fila(slug, paj_norm, id_tramite)
    if not dados:
        return JSONResponse({"ok": False, "erro": "tramitação não encontrada"}, status_code=404)
    return {"ok": True}


@router.post("/api/caixa/{slug}/paj/{paj_norm}/triar/conjunto", response_class=JSONResponse)
async def triar_conjunto(slug: str, paj_norm: str):
    """Trata em conjunto as tramitações pendentes do PAJ (síntese única pela IA)."""
    try:
        res = await divcom_service.tratar_conjunto(slug, paj_norm)
        return JSONResponse(res, status_code=200 if res.get("ok") else 502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


@router.post("/api/caixa/{slug}/paj/{paj_norm}/triar/separado", response_class=JSONResponse)
async def triar_separado(slug: str, paj_norm: str, payload: dict = Body(default={})):
    mapeamento = (payload or {}).get("mapeamento") or {}
    try:
        res = await divcom_service.tratar_separado(slug, paj_norm, mapeamento)
        return JSONResponse(res, status_code=200 if res.get("ok") else 502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


@router.post("/api/caixa/{slug}/paj/{paj_norm}/triar/desfazer", response_class=JSONResponse)
async def triar_desfazer(slug: str, paj_norm: str):
    return JSONResponse(divcom_service.desfazer_triagem(slug, paj_norm))


@router.post("/api/caixa/{slug}/paj/{paj_norm}/conjunto/gravar", response_class=JSONResponse)
async def gravar_conjunto(slug: str, paj_norm: str, payload: dict = Body(...)):
    meios = payload.get("meios", []) if isinstance(payload, dict) else []
    if not meios:
        return JSONResponse({"ok": False, "erro": "Selecione ao menos um meio de contato."}, status_code=400)
    dados = divcom_service.gravar_conjunto(slug, paj_norm, payload.get("mensagem", ""), meios)
    if not dados:
        return JSONResponse({"ok": False, "erro": "conjunto não encontrado"}, status_code=404)
    return {"ok": True}


@router.post("/api/caixa/{slug}/paj/{paj_norm}/conjunto/gravar/remover", response_class=JSONResponse)
async def remover_conjunto(slug: str, paj_norm: str):
    dados = divcom_service.remover_conjunto_da_fila(slug, paj_norm)
    if not dados:
        return JSONResponse({"ok": False, "erro": "conjunto não encontrado"}, status_code=404)
    return {"ok": True}


@router.post("/api/caixa/{slug}/paj/{paj_norm}/conjunto/sugestao", response_class=JSONResponse)
async def sugestao_conjunto(slug: str, paj_norm: str, payload: dict = Body(...)):
    dados = divcom_service.definir_sugestao_conjunto(slug, paj_norm,
                                                     payload.get("texto", ""), payload.get("tipo"))
    if not dados:
        return JSONResponse({"ok": False, "erro": "conjunto não encontrado"}, status_code=404)
    return {"ok": True}


@router.get("/api/caixa/{slug}/enviar-fila/stream")
async def enviar_fila_stream(slug: str):
    async def gen():
        try:
            async for ev in divcom_service.enviar_fila_stream(slug):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'level': 'erro', 'msg': str(e)}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True, 'resumo': {'ok': False, 'erro': str(e)}})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
