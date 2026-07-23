"""Rotas da central de LIGAÇÕES TELEFÔNICAS e de CORRESPONDÊNCIAS.

- GET  /ligacoes                 -> página com as tabelas Pendentes e Feitas
- GET  /correspondencias         -> página com a tabela de Correspondências Pendentes
- POST /api/ligacao/marcar       -> envia um trâmite para contato telefônico
- POST /api/ligacao/cancelar     -> tira o trâmite da fila de ligações (volta à caixa)
- POST /api/ligacao/registrar    -> registra 1 tentativa de ligação (movimenta o PAJ)
- POST /api/ligacao/correspondencia -> encaminha o trâmite para a fila de correspondências
- POST /api/correspondencia/cancelar -> tira o trâmite da fila de correspondências
"""

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse

from services import config_service
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
        prazo_padrao=config_service.get_prazo_tramitacao_padrao()))


@router.get("/correspondencias", response_class=HTMLResponse)
async def correspondencias_page(request: Request):
    db = svc.listar_correspondencias()
    template = request.app.state.jinja.get_template("correspondencias.html")
    return HTMLResponse(template.render(
        request=request, pendentes=db["pendentes"],
        modo=config_service.get_envio_modo_meio("correspondencia")))


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
    r = svc.encaminhar_correspondencia(
        p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
        motivo=p.get("motivo", ""), encerrar_ligacao=True, tipo=p.get("tipo", "tramite"))
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/correspondencia/cancelar", response_class=JSONResponse)
async def api_correspondencia_cancelar(payload: dict = Body(...)):
    p = payload or {}
    r = svc.cancelar_correspondencia(p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
                                     tipo=p.get("tipo", "tramite"))
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/correspondencia/editar", response_class=JSONResponse)
async def api_correspondencia_editar(payload: dict = Body(...)):
    """Edita o texto-base da carta (mensagem-base) de uma correspondência pendente."""
    p = payload or {}
    r = svc.encaminhar_correspondencia(
        p.get("slug", ""), p.get("paj_norm", ""), p.get("id_tramite", ""),
        mensagem=p.get("mensagem", ""), tipo=p.get("tipo", "tramite"))
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)
