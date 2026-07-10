"""Rotas das caixas do Painel DIVCOM (multi-caixa).

- GET  /                                  -> redireciona p/ a 1ª caixa habilitada
- GET  /caixa/{slug}                      -> dashboard de uma caixa
- GET  /api/caixa/{slug}/sincronizar/stream -> sync da caixa (SSE, tempo real)
- POST /api/paj/{paj_norm}/whatsapp       -> escolha de WhatsApp (compartilhada)
- POST /api/caixa/{slug}/paj/{paj_norm}/sugestao -> edição da sugestão (por caixa)
"""

import json

from fastapi import APIRouter, Body, File, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, StreamingResponse)

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


@router.get("/prontos", response_class=HTMLResponse)
async def prontos_envio(request: Request):
    """Página GLOBAL "Prontos para Envio": fila de envio de todas as caixas."""
    meios = envio_service.meios_disponiveis()
    prontos = divcom_service.listar_prontos_envio()
    # Opções de filtro: caixas e tipos (texto padrão) presentes na fila.
    caixas_filtro, _vistos = [], set()
    for p in prontos:
        if p["caixa_slug"] not in _vistos:
            _vistos.add(p["caixa_slug"])
            caixas_filtro.append({"slug": p["caixa_slug"], "nome": p["caixa_nome"]})
    tipos_filtro = sorted({p.get("sugestao_tipo", "") for p in prontos if p.get("sugestao_tipo")})
    template = request.app.state.jinja.get_template("prontos.html")
    return HTMLResponse(template.render(
        request=request,
        prontos=prontos,
        meios_envio=meios,
        meio_labels={m["key"]: m["label"] for m in meios},
        caixas_filtro=caixas_filtro,
        tipos_filtro=tipos_filtro,
        envio_modo=config_service.get_envio_modo()))


@router.get("/api/prontos/enviar/stream")
async def prontos_enviar_stream():
    async def gen():
        try:
            async for ev in divcom_service.enviar_todas_filas_stream():
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'level': 'erro', 'msg': str(e)}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True, 'resumo': {'ok': False, 'erro': str(e)}})}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/api/prontos/enviar-selecao")
async def prontos_enviar_selecao(payload: dict = Body(default={})):
    """Envia SOMENTE os itens selecionados (checkbox/filtro) na página Prontos.
    Corpo: {selecao: [{slug, paj_norm, id_tramite, tipo}]}. Resposta SSE (via POST)."""
    selecao = payload.get("selecao") or [] if isinstance(payload, dict) else []

    async def gen():
        try:
            async for ev in divcom_service.enviar_selecao_stream(selecao):
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


@router.post("/api/caixa/{slug}/paj/{paj_norm}/movs-selecao", response_class=JSONResponse)
async def movs_selecao(slug: str, paj_norm: str, payload: dict = Body(default={})):
    """Salva quais movimentações vão à IA (checkboxes + recorte manual). NÃO reanalisa
    — a reanálise é disparada pelo botão "Reanálise pela IA" na seção de ações."""
    try:
        divcom_service.definir_selecao_movs(
            paj_norm, payload.get("selecao"), payload.get("texto"))
        return JSONResponse({"ok": True})
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


@router.post("/api/caixa/{slug}/paj/{paj_norm}/reanalisar", response_class=JSONResponse)
async def reanalisar(slug: str, paj_norm: str):
    """Reanálise pela IA: descarta a análise anterior (texto + classificação) e gera
    uma nova com as movimentações selecionadas/recorte — sem voltar ao SISDPU."""
    try:
        res = await divcom_service.reanalisar_paj(slug, paj_norm)
        return JSONResponse(res, status_code=200 if res.get("ok") else 502)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


@router.post("/api/caixa/{slug}/paj/{paj_norm}/tramite/{id_tramite}/gravar", response_class=JSONResponse)
async def gravar_para_envio(slug: str, paj_norm: str, id_tramite: str, payload: dict = Body(...)):
    """Grava UMA tramitação na fila de envio da caixa (mensagem + meios)."""
    mensagem = payload.get("mensagem", "") if isinstance(payload, dict) else ""
    meios = payload.get("meios", []) if isinstance(payload, dict) else []
    somente_leitura = bool(payload.get("somente_leitura")) if isinstance(payload, dict) else False
    if not meios:
        return JSONResponse({"ok": False, "erro": "Selecione ao menos um meio de contato."},
                            status_code=400)
    dados = divcom_service.gravar_para_envio(slug, paj_norm, id_tramite, mensagem, meios,
                                             somente_leitura=somente_leitura)
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
    somente_leitura = bool(payload.get("somente_leitura")) if isinstance(payload, dict) else False
    if not meios:
        return JSONResponse({"ok": False, "erro": "Selecione ao menos um meio de contato."}, status_code=400)
    dados = divcom_service.gravar_conjunto(slug, paj_norm, payload.get("mensagem", ""), meios,
                                           somente_leitura=somente_leitura)
    if not dados:
        return JSONResponse({"ok": False, "erro": "conjunto não encontrado"}, status_code=404)
    return {"ok": True}


@router.post("/api/caixa/{slug}/paj/{paj_norm}/conjunto/gravar/remover", response_class=JSONResponse)
async def remover_conjunto(slug: str, paj_norm: str):
    dados = divcom_service.remover_conjunto_da_fila(slug, paj_norm)
    if not dados:
        return JSONResponse({"ok": False, "erro": "conjunto não encontrado"}, status_code=404)
    return {"ok": True}


# ==================== anexos (documentos a enviar) ====================
async def _ler_uploads(arquivos: list[UploadFile]) -> list[tuple[str, bytes]]:
    out: list[tuple[str, bytes]] = []
    for f in arquivos or []:
        out.append((f.filename or "arquivo", await f.read()))
    return out


def _servir_anexo(paj_norm: str, tipo: str, id_tramite: str, arquivo: str):
    p = divcom_service.caminho_anexo(paj_norm, tipo, id_tramite, arquivo).resolve()
    base = divcom_service.ANEXOS_DIR.resolve()
    if base not in p.parents or not p.is_file():
        return JSONResponse({"ok": False, "erro": "arquivo não encontrado"}, status_code=404)
    nome = arquivo.split("__", 1)[1] if "__" in arquivo else arquivo  # tira o prefixo uuid__
    return FileResponse(str(p), filename=nome)


@router.post("/api/caixa/{slug}/paj/{paj_norm}/tramite/{id_tramite}/anexos", response_class=JSONResponse)
async def anexos_tramite_add(slug: str, paj_norm: str, id_tramite: str,
                             arquivos: list[UploadFile] = File(default=[])):
    res = divcom_service.adicionar_anexos(slug, paj_norm, "tramite", id_tramite,
                                          await _ler_uploads(arquivos))
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/caixa/{slug}/paj/{paj_norm}/tramite/{id_tramite}/anexos/{anexo_id}/remover",
             response_class=JSONResponse)
async def anexos_tramite_del(slug: str, paj_norm: str, id_tramite: str, anexo_id: str):
    res = divcom_service.remover_anexo(slug, paj_norm, "tramite", id_tramite, anexo_id)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/caixa/{slug}/paj/{paj_norm}/tramite/{id_tramite}/anexo/{arquivo}")
async def anexo_tramite_baixar(slug: str, paj_norm: str, id_tramite: str, arquivo: str):
    return _servir_anexo(paj_norm, "tramite", id_tramite, arquivo)


@router.post("/api/caixa/{slug}/paj/{paj_norm}/conjunto/anexos", response_class=JSONResponse)
async def anexos_conjunto_add(slug: str, paj_norm: str,
                              arquivos: list[UploadFile] = File(default=[])):
    res = divcom_service.adicionar_anexos(slug, paj_norm, "conjunto", "",
                                          await _ler_uploads(arquivos))
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/caixa/{slug}/paj/{paj_norm}/conjunto/anexos/{anexo_id}/remover",
             response_class=JSONResponse)
async def anexos_conjunto_del(slug: str, paj_norm: str, anexo_id: str):
    res = divcom_service.remover_anexo(slug, paj_norm, "conjunto", "", anexo_id)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/caixa/{slug}/paj/{paj_norm}/conjunto/anexo/{arquivo}")
async def anexo_conjunto_baixar(slug: str, paj_norm: str, arquivo: str):
    return _servir_anexo(paj_norm, "conjunto", "", arquivo)


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
