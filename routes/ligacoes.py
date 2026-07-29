"""Rotas da central de LIGAÇÕES TELEFÔNICAS e de CORRESPONDÊNCIAS.

- GET  /ligacoes                 -> página com as tabelas Pendentes e Feitas
- GET  /correspondencias         -> página com a tabela de Correspondências Pendentes
- POST /api/ligacao/marcar       -> envia um trâmite para contato telefônico
- POST /api/ligacao/cancelar     -> tira o trâmite da fila de ligações (volta à caixa)
- POST /api/ligacao/registrar    -> registra 1 tentativa de ligação (movimenta o PAJ)
- POST /api/ligacao/correspondencia -> encaminha o trâmite para a fila de correspondências
- POST /api/correspondencia/cancelar -> tira o trâmite da fila de correspondências
"""

from pathlib import Path

from anyio import to_thread
from fastapi import APIRouter, Body, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               StreamingResponse)

from services import config_service
from services import disparos_service
from services import painel_comunicacoes_service as svc

router = APIRouter()


@router.get("/ligacoes", response_class=HTMLResponse)
async def ligacoes_page(request: Request):
    db = svc.listar_ligacoes()
    # "aguardando contato" = pendentes (0 tentativas) + feitas em andamento (não esgotadas)
    aguardando = len(db["pendentes"]) + sum(1 for it in db["feitas"] if not it.get("lig_esgotado"))
    template = request.app.state.jinja.get_template("ligacoes.html")
    return HTMLResponse(template.render(
        request=request, pendentes=db["pendentes"], feitas=db["feitas"],
        max_tentativas=svc.LIGACOES_MAX_TENTATIVAS,
        total_aguardando=aguardando, modo=config_service.get_envio_modo_meio("telefone"),
        prazos_predefinidos=config_service.get_prazos_predefinidos(),
        prazo_padrao=config_service.get_prazo_tramitacao_padrao(),
        disparos=disparos_service.listar_disparos(meio="telefone", limite=25)))


@router.get("/correspondencias", response_class=HTMLResponse)
async def correspondencias_page(request: Request):
    db = svc.listar_correspondencias()
    template = request.app.state.jinja.get_template("correspondencias.html")
    return HTMLResponse(template.render(
        request=request, pendentes=db["pendentes"],
        modo=config_service.get_envio_modo_meio("correspondencia"),
        carta_cabecalho=config_service.get_carta_cabecalho(),
        carta_rodape=config_service.get_carta_rodape(),
        disparos=disparos_service.listar_disparos(meio="carta", limite=25)))


@router.post("/api/ligacao/marcar", response_class=JSONResponse)
async def api_ligacao_marcar(payload: dict = Body(...)):
    p = payload or {}
    r = svc.enfileirar_ligacao(p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""))
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/ligacao/cancelar", response_class=JSONResponse)
async def api_ligacao_cancelar(payload: dict = Body(...)):
    p = payload or {}
    r = svc.cancelar_ligacao(p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
                             tipo=p.get("tipo", "tramite"))
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/ligacao/registrar", response_class=JSONResponse)
async def api_ligacao_registrar(payload: dict = Body(...)):
    p = payload or {}
    try:
        r = await svc.registrar_ligacao(
            p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
            numeros=p.get("numeros") or p.get("numero"), resultado=p.get("resultado", ""),
            registro=p.get("registro", ""), tramitar_grupo=p.get("tramitar_grupo", ""),
            concluir=bool(p.get("concluir")), prazo=p.get("prazo"), tipo=p.get("tipo", "tramite"))
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/ligacao/correspondencia", response_class=JSONResponse)
async def api_ligacao_correspondencia(payload: dict = Body(...)):
    """Botão 'Enviar correspondência' da central de Ligações (após 3 tentativas):
    encaminha para Correspondências E encerra a rotina de ligações desse trâmite."""
    p = payload or {}
    # Em thread: gera o corpo da carta pela IA (bloqueante) sem travar o event loop.
    r = await to_thread.run_sync(lambda: svc.encaminhar_correspondencia(
        p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
        motivo=p.get("motivo", ""), encerrar_ligacao=True, tipo=p.get("tipo", "tramite")))
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/correspondencia/cancelar", response_class=JSONResponse)
async def api_correspondencia_cancelar(payload: dict = Body(...)):
    p = payload or {}
    r = svc.cancelar_correspondencia(p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
                                     tipo=p.get("tipo", "tramite"))
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/correspondencia/validar-endereco", response_class=JSONResponse)
async def api_correspondencia_validar_endereco(payload: dict = Body(...)):
    """Valida o endereço do PAJ (resolve o CEP no ViaCEP e compara com o cadastro),
    para habilitar o 'Expedir carta'. Roda em thread (consulta de rede)."""
    p = payload or {}
    try:
        r = await to_thread.run_sync(lambda: svc.validar_endereco_carta(p.get("paj_norm", "")))
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/correspondencia/validar-endereco-editado", response_class=JSONResponse)
async def api_correspondencia_validar_editado(payload: dict = Body(...)):
    """Valida um endereço EDITADO no modal (sem salvar). Roda em thread (ViaCEP)."""
    p = payload or {}
    try:
        r = await to_thread.run_sync(lambda: svc.validar_endereco_editado(p.get("endereco") or {}))
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/correspondencia/salvar-endereco", response_class=JSONResponse)
async def api_correspondencia_salvar_endereco(payload: dict = Body(...)):
    """Salva o endereço corrigido APENAS no cache local do painel (para a expedição),
    SEM gravar no SISDPU — a gravação no SISDPU é opcional. Roda em thread (ViaCEP)."""
    p = payload or {}
    try:
        r = await to_thread.run_sync(
            lambda: svc.atualizar_endereco_local(p.get("paj_norm", ""), p.get("endereco") or {}))
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/correspondencia/atualizar-endereco", response_class=JSONResponse)
async def api_correspondencia_atualizar_endereco(payload: dict = Body(...)):
    """Grava o endereço corrigido no cadastro do assistido no SISDPU E no cache local,
    e revalida. Operação com Playwright (headless) — pode levar alguns segundos."""
    p = payload or {}
    try:
        r = await svc.atualizar_endereco(
            p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
            p.get("tipo", "tramite"), p.get("endereco") or {})
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/correspondencia/atualizar-endereco-cep", response_class=JSONResponse)
async def api_correspondencia_atualizar_endereco_cep(payload: dict = Body(...)):
    """Atualiza o endereço do assistido no SISDPU trocando SÓ o CEP (modal de CEP desmembrado
    na expedição): resolve o CEP novo, mantém número/complemento. Playwright — pode demorar."""
    p = payload or {}
    try:
        r = await svc.atualizar_endereco_por_cep(
            p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
            p.get("tipo", "tramite"), p.get("cep", ""))
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/correspondencia/expedir", response_class=JSONResponse)
async def api_correspondencia_expedir(payload: dict = Body(...)):
    """Expede a carta pelos Correios (SMT) e, se postada (modo real), movimenta +
    junta o comprovante + tramita p/ a caixa de aguardo. Governado pelo modo do meio
    'correspondencia'. Operação longa (Playwright) — o front mostra progresso."""
    p = payload or {}
    try:
        r = await svc.expedir_correspondencia(
            p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
            tipo=p.get("tipo", "tramite"), forcar=bool(p.get("forcar")))
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/correspondencia/rejuntar-comprovante", response_class=JSONResponse)
async def api_correspondencia_rejuntar(payload: dict = Body(...)):
    """CORREÇÃO: junta o comprovante de postagem CORRETO a um PAJ (fase 'Juntada de documento')."""
    p = payload or {}
    try:
        r = await svc.rejuntar_comprovante(
            p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
            p.get("comprovante", ""), tipo=p.get("tipo", "tramite"), previsao=p.get("previsao", ""))
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/correspondencia/expedir/stream")
async def api_correspondencia_expedir_stream(payload: dict = Body(...)):
    """Igual a /expedir, mas com LOG AO VIVO (SSE): transmite cada etapa no site dos
    Correios e depois a movimentação/tramitação no SISDPU (modal passo a passo, como nos
    outros meios). A sessão do disparo é salva por `stream_e_salvar`."""
    p = payload or {}
    gen = svc.expedir_correspondencia_stream(
        p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
        tipo=p.get("tipo", "tramite"), forcar=bool(p.get("forcar")),
        cep_override=p.get("cep_override", "") or "")
    return StreamingResponse(
        disparos_service.stream_e_salvar(gen, "carta"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/carta-arquivo/{nome}")
async def carta_arquivo(nome: str):
    """Serve um PDF da carta expedida (conteúdo OU comprovante de postagem), de
    _comprovantes_carta/. Só o nome-base (bloqueia path traversal). Abre inline."""
    base = Path(nome).name
    caminho = svc.COMPROVANTES_CARTA_DIR / base
    if base != nome or caminho.suffix.lower() != ".pdf" or not caminho.is_file():
        return JSONResponse({"erro": "arquivo não encontrado"}, status_code=404)
    return FileResponse(str(caminho), media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{base}"'})


@router.post("/api/correspondencia/gerar-carta", response_class=JSONResponse)
async def api_correspondencia_gerar_carta(payload: dict = Body(...)):
    """Gera o CORPO da carta pela IA para UM PAJ (sob demanda, botão 'Gerar carta').
    Roda em thread (chamada de IA bloqueante) p/ não travar o event loop."""
    p = payload or {}
    try:
        r = await to_thread.run_sync(lambda: svc.gerar_carta(
            p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
            tipo=p.get("tipo", "tramite")))
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.get("/api/correspondencia/modelos", response_class=JSONResponse)
async def api_correspondencia_modelos(slug: str = ""):
    """Textos padrão (comunicados) + parágrafos padrão da caixa, para o modal de edição da
    carta inserir modelos/parágrafos. ⚠️ SÓ o TEXTO é usado na carta — anexos padrão dos
    parágrafos/comunicados são IGNORADOS (a expedição pelos Correios só admite texto)."""
    try:
        return {"ok": True,
                "comunicados": config_service.comunicados_da_caixa(slug),
                "paragrafos": config_service.paragrafos_para_caixa(slug)}
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "erro": str(e)}, status_code=400)


@router.post("/api/correspondencia/editar", response_class=JSONResponse)
async def api_correspondencia_editar(payload: dict = Body(...)):
    """Salva o CORPO da carta conferido/editado manualmente (NÃO regera pela IA)."""
    p = payload or {}
    r = svc.editar_carta_corpo(
        p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
        texto=p.get("mensagem", ""), tipo=p.get("tipo", "tramite"))
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)
