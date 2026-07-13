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


@router.get("/caixas", response_class=HTMLResponse)
async def caixas_home(request: Request):
    """Página de RESUMO das caixas sincronizadas (habilitadas), com nº de PAJs a
    tratar e link para abrir cada uma. Espelha a lógica do resumo de Prontos."""
    caixas = divcom_service.menu_caixas()  # habilitadas + qtd
    template = request.app.state.jinja.get_template("caixas_resumo.html")
    return HTMLResponse(template.render(
        request=request, caixas=caixas,
        total=sum(c.get("qtd", 0) for c in caixas)))


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
        triagem=db["triagem"], pendentes=db["pendentes"], fila=db["fila"],
        paragrafos=paragrafos, comunicados=comunicados,
        meios_envio=envio_service.meios_disponiveis(),
        prazos_predefinidos=config_service.get_prazos_predefinidos(),
        layout_pendentes=config_service.get_layout_pendentes(),
        envio_modos_resumo=config_service.resumo_modos()))


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
async def prontos_home(request: Request):
    """Página de RESUMO: quantas mensagens gravadas há em cada meio (com link para
    abrir), ou aviso de que não há mensagens gravadas em nenhum meio."""
    counts = divcom_service.prontos_por_meio()
    linhas = [{"slug": slug, "key": key, "label": lab, "qtd": counts.get(slug, 0),
               "modo": config_service.get_envio_modo_meio(key)}
              for slug, key, lab in divcom_service.PRONTOS_MEIOS if counts.get(slug, 0)]
    template = request.app.state.jinja.get_template("prontos_resumo.html")
    return HTMLResponse(template.render(
        request=request, linhas=linhas,
        total=counts.get("total", 0),
        total_meios=sum(l["qtd"] for l in linhas)))


@router.get("/prontos/{meio}", response_class=HTMLResponse)
async def prontos_envio(request: Request, meio: str):
    """Página "Prontos para Envio" de UM meio (submenu). App/E-mail = lote;
    WhatsApp = assistido por linha; Telefone/Carta = a definir."""
    key_por_slug = {slug: k for slug, k, _l in divcom_service.PRONTOS_MEIOS}
    label_por_slug = {slug: lab for slug, _k, lab in divcom_service.PRONTOS_MEIOS}
    if meio not in key_por_slug:
        return RedirectResponse("/prontos/app")
    key = key_por_slug[meio]
    meios = envio_service.meios_disponiveis()
    todos = divcom_service.listar_prontos_envio()
    itens = [p for p in todos if key in (p.get("envio_meios") or [])]
    prontos_app = itens if meio in ("app", "email") else []
    prontos_wa = itens if meio == "whatsapp" else []
    # Opções de filtro (caixas/tipos presentes NESTE meio).
    caixas_filtro, _vistos = [], set()
    for p in itens:
        if p["caixa_slug"] not in _vistos:
            _vistos.add(p["caixa_slug"])
            caixas_filtro.append({"slug": p["caixa_slug"], "nome": p["caixa_nome"]})
    tipos_filtro = sorted({p.get("sugestao_tipo", "") for p in itens if p.get("sugestao_tipo")})
    # Título do cabeçalho: prefixo + nome do meio (colorido com a cor identificadora).
    _titulos = {
        "app":      ("Prontos para enviar pelo ", "Aplicativo SISDPU", "--c-app"),
        "whatsapp": ("Prontos para enviar pelo ", "WhatsApp",          "--c-zap"),
        "email":    ("Prontos para enviar via ",  "e-mail",            "--c-mail"),
        "telefone": ("Prontos para enviar por ",  "telefone",          "--c-tel"),
        "carta":    ("Prontos para enviar por ",  "carta",             "--c-addr"),
    }
    titulo_pre, titulo_nome, meio_cor = _titulos.get(
        meio, ("Prontos para enviar por ", label_por_slug[meio], "--text-secondary"))
    template = request.app.state.jinja.get_template("prontos.html")
    return HTMLResponse(template.render(
        request=request,
        prontos=itens,
        prontos_app=prontos_app,
        prontos_wa=prontos_wa,
        meio_ativo=meio,
        meio_key=key,
        meio_label=label_por_slug[meio],
        titulo_pre=titulo_pre,
        titulo_nome=titulo_nome,
        meio_cor=meio_cor,
        meio_modo=config_service.get_envio_modo_meio(key),
        meios_envio=meios,
        meio_labels=envio_service.meios_labels(),
        caixas_filtro=caixas_filtro,
        tipos_filtro=tipos_filtro,
        prazos_predefinidos=config_service.get_prazos_predefinidos(),
        envio_modos_resumo=config_service.resumo_modos()))


@router.post("/api/prontos/whatsapp/enviar")
async def prontos_whatsapp_enviar(payload: dict = Body(...)):
    """Envio ASSISTIDO por WhatsApp de UMA tramitação (fora do lote). 1 clique →
    Playwright envia texto→anexos, gera o comprovante, movimenta e (se escolhido)
    tramita. Resposta SSE: transmite cada PASSO ao popup em tempo real e, no fim,
    um evento {done, resultado:{ok, status, detalhe, comprovante_nome}}."""
    slug = (payload or {}).get("slug", "")
    paj = (payload or {}).get("paj_norm", "")
    tr = (payload or {}).get("id_tramite", "")
    tipo = (payload or {}).get("tipo", "tramite") or "tramite"
    # conjunto não tem id_tramite único (usa tramite_ids); só exige slug + paj_norm nesse caso
    if not (slug and paj) or (tipo != "conjunto" and not tr):
        return JSONResponse({"ok": False, "detalhe": "Parâmetros ausentes."}, status_code=400)

    async def gen():
        try:
            async for ev in divcom_service.enviar_whatsapp_tramite_stream(slug, paj, tr, tipo):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'level': 'erro', 'msg': str(e)}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True, 'resultado': {'ok': False, 'detalhe': str(e)}})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/prontos/enviar-item")
async def prontos_enviar_item(payload: dict = Body(...)):
    """Envio INDIVIDUAL de um item por UM meio (App/E-mail) a partir de Prontos.
    Corpo: {slug, paj_norm, tipo, id_tramite, meio}. Resposta SSE: transmite cada
    passo (envio → movimentação → tramitação) e, no fim, {done, resultado}."""
    p = payload or {}
    slug, paj, tipo = p.get("slug", ""), p.get("paj_norm", ""), p.get("tipo", "tramite")
    tr, meio = p.get("id_tramite", ""), p.get("meio", "")
    if not (slug and paj and meio):
        return JSONResponse({"ok": False, "detalhe": "Parâmetros ausentes."}, status_code=400)

    async def gen():
        try:
            async for ev in divcom_service.enviar_item_meio_stream(slug, paj, tipo, tr, meio):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'level': 'erro', 'msg': str(e)}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True, 'resultado': {'ok': False, 'detalhe': str(e)}})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/prontos/whatsapp/enviar-lote")
async def prontos_whatsapp_lote(payload: dict = Body(default={})):
    """Envio em LOTE por WhatsApp (com intervalo entre mensagens). Corpo:
    {selecao: [{slug, paj_norm, id_tramite, paj}]}. Resposta SSE (via POST)."""
    selecao = payload.get("selecao") or [] if isinstance(payload, dict) else []

    async def gen():
        try:
            async for ev in divcom_service.enviar_whatsapp_lote_stream(selecao):
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'level': 'erro', 'msg': str(e)}, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'done': True, 'resumo': {'ok': False, 'erro': str(e)}})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/comprovante/whatsapp/{nome}")
async def comprovante_whatsapp(nome: str):
    """Serve o PDF do comprovante de envio por WhatsApp (abre inline no navegador).
    Só o nome-base, dentro de COMPROVANTES_WA_DIR (bloqueia path traversal)."""
    from pathlib import Path
    base = Path(nome).name
    caminho = divcom_service.COMPROVANTES_WA_DIR / base
    if base != nome or caminho.suffix.lower() != ".pdf" or not caminho.is_file():
        return JSONResponse({"erro": "comprovante não encontrado"}, status_code=404)
    return FileResponse(str(caminho), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{base}"'})


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


@router.post("/api/prontos/devolver-pendentes", response_class=JSONResponse)
async def prontos_devolver_pendentes(payload: dict = Body(...)):
    """Devolve tramitações GRAVADAS de um mesmo PAJ (mesma caixa) para 'pendentes'
    (ficam na triagem da caixa; SEM rodar a IA). Corpo: {slug, paj_norm,
    id_tramites: [...]}. Remediação do conflito de App SISDPU."""
    slug = payload.get("slug") if isinstance(payload, dict) else None
    paj_norm = payload.get("paj_norm") if isinstance(payload, dict) else None
    ids = (payload.get("id_tramites") or []) if isinstance(payload, dict) else []
    if not slug or not paj_norm:
        return JSONResponse({"ok": False, "erro": "slug/paj_norm ausentes"}, status_code=400)
    try:
        res = divcom_service.devolver_para_pendentes(slug, paj_norm, ids)
        return JSONResponse(res, status_code=200 if res.get("ok") else 400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=500)


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
    prazos = payload.get("prazos") if isinstance(payload, dict) else None
    tramitar_grupo = payload.get("tramitar_grupo", "") if isinstance(payload, dict) else ""
    if not meios:
        return JSONResponse({"ok": False, "erro": "Selecione ao menos um meio de contato."},
                            status_code=400)
    dados = divcom_service.gravar_para_envio(slug, paj_norm, id_tramite, mensagem, meios,
                                             somente_leitura=somente_leitura, prazos=prazos,
                                             tramitar_grupo=tramitar_grupo)
    if not dados:
        return JSONResponse({"ok": False, "erro": "tramitação não encontrada"}, status_code=404)
    return {"ok": True}


@router.get("/api/grupos-unidade", response_class=JSONResponse)
async def grupos_unidade(refresh: int = 0):
    """Lista as caixas de GRUPO da unidade (p/ o dropdown de tramitação no modal Gravar).
    Usa cache em config; busca no SISDPU se vazio ou refresh=1."""
    from ingestao import sisdpu_client
    cache = config_service.get_grupos_unidade()
    if refresh or not cache:
        try:
            grupos = await sisdpu_client.listar_grupos_unidade()
            if grupos:
                cache = config_service.set_grupos_unidade(grupos)
        except Exception as e:  # noqa: BLE001
            return {"ok": bool(cache), "grupos": cache,
                    "favoritos": config_service.get_grupos_favoritos(), "erro": str(e)}
    return {"ok": True, "grupos": cache, "favoritos": config_service.get_grupos_favoritos()}


@router.post("/api/caixa/{slug}/paj/{paj_norm}/tramite/{id_tramite}/gravar/remover", response_class=JSONResponse)
async def remover_da_fila(slug: str, paj_norm: str, id_tramite: str, payload: dict = Body(default={})):
    """Tira uma tramitação da fila de envio. Se `meio` vier no corpo e houver outros
    meios pendentes, remove só aquele meio; senão volta a pendente."""
    meio = (payload or {}).get("meio") or None
    dados = divcom_service.remover_da_fila(slug, paj_norm, id_tramite, meio)
    if not dados:
        return JSONResponse({"ok": False, "erro": "tramitação não encontrada"}, status_code=404)
    return {"ok": True}


@router.post("/api/caixa/{slug}/paj/{paj_norm}/triar/conjunto", response_class=JSONResponse)
async def triar_conjunto(slug: str, paj_norm: str):
    """Dispara (em segundo plano) o tratamento em conjunto do PAJ. Retorna na hora;
    o progresso é consultado em /triagem-status."""
    res = divcom_service.iniciar_triagem(slug, paj_norm, "conjunto")
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/caixa/{slug}/paj/{paj_norm}/triar/separado", response_class=JSONResponse)
async def triar_separado(slug: str, paj_norm: str, payload: dict = Body(default={})):
    mapeamento = (payload or {}).get("mapeamento") or {}
    res = divcom_service.iniciar_triagem(slug, paj_norm, "separado", mapeamento)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.get("/api/caixa/{slug}/triagem-status", response_class=JSONResponse)
async def triagem_status(slug: str):
    """Triagens em andamento (ou com erro) da caixa — para o front manter o spinner e
    recarregar quando concluir."""
    return {"ok": True, "jobs": divcom_service.status_triagem(slug)}


@router.post("/api/caixa/{slug}/paj/{paj_norm}/triar/desfazer", response_class=JSONResponse)
async def triar_desfazer(slug: str, paj_norm: str):
    return JSONResponse(divcom_service.desfazer_triagem(slug, paj_norm))


@router.post("/api/caixa/{slug}/paj/{paj_norm}/conjunto/gravar", response_class=JSONResponse)
async def gravar_conjunto(slug: str, paj_norm: str, payload: dict = Body(...)):
    meios = payload.get("meios", []) if isinstance(payload, dict) else []
    somente_leitura = bool(payload.get("somente_leitura")) if isinstance(payload, dict) else False
    prazos = payload.get("prazos") if isinstance(payload, dict) else None
    tramitar_grupo = payload.get("tramitar_grupo", "") if isinstance(payload, dict) else ""
    if not meios:
        return JSONResponse({"ok": False, "erro": "Selecione ao menos um meio de contato."}, status_code=400)
    dados = divcom_service.gravar_conjunto(slug, paj_norm, payload.get("mensagem", ""), meios,
                                           somente_leitura=somente_leitura, prazos=prazos,
                                           tramitar_grupo=tramitar_grupo)
    if not dados:
        return JSONResponse({"ok": False, "erro": "conjunto não encontrado"}, status_code=404)
    return {"ok": True}


@router.post("/api/caixa/{slug}/paj/{paj_norm}/conjunto/gravar/remover", response_class=JSONResponse)
async def remover_conjunto(slug: str, paj_norm: str, payload: dict = Body(default={})):
    meio = (payload or {}).get("meio") or None
    dados = divcom_service.remover_conjunto_da_fila(slug, paj_norm, meio)
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


@router.post("/api/caixa/{slug}/paj/{paj_norm}/anexar-padrao", response_class=JSONResponse)
async def anexar_padrao(slug: str, paj_norm: str, payload: dict = Body(...)):
    """Copia os anexos PADRÃO de um comunicado/parágrafo para a tramitação/conjunto
    (ao aplicar o texto/parágrafo no dashboard). Corpo: {tipo, id_tramite, kind,
    owner_id, owner_slug}."""
    res = divcom_service.anexar_padrao_em_tramite(
        slug, paj_norm, payload.get("tipo", "tramite"), payload.get("id_tramite", ""),
        payload.get("kind", ""), payload.get("owner_id", ""), payload.get("owner_slug") or None)
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


@router.post("/api/caixa/{slug}/paj/{paj_norm}/remover-anexo-padrao", response_class=JSONResponse)
async def remover_anexo_padrao_tramite(slug: str, paj_norm: str, payload: dict = Body(...)):
    """Remove da tramitação os anexos incluídos por um dado texto/parágrafo padrão
    (quando o usuário desfaz a aplicação). Corpo: {tipo, id_tramite, owner_id}."""
    res = divcom_service.remover_anexos_padrao_do_alvo(
        slug, paj_norm, payload.get("tipo", "tramite"), payload.get("id_tramite", ""),
        payload.get("owner_id", ""))
    return JSONResponse(res, status_code=200 if res.get("ok") else 400)


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
