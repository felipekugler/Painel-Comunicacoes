"""Rotas da Central de Confirmações de leitura (WhatsApp).

Mensagens enviadas por WhatsApp costumam pedir "responda OK" para confirmar o
recebimento. Esta central automatiza a LEITURA das respostas (WhatsApp) — a
DECISÃO de movimentar/concluir o PAJ fica sempre com o usuário, 1 clique por item
(ou em bloco, vários de uma vez).

- GET  /confirmacoes                                  -> página (pendências + confirmadas, sem ler o WhatsApp ainda)
- POST /api/confirmacoes/verificar/stream             -> lê o WhatsApp de cada pendência (SSE, log ao vivo)
- POST /api/confirmacoes/{historico_id}/confirmar     -> movimenta (ciente do recebimento) + conclui a tramitação
- POST /api/confirmacoes/{historico_id}/remover       -> tira da lista de pendentes, sem ação no SISDPU
- POST /api/confirmacoes/confirmar-lote/stream        -> confirma vários itens em sequência (SSE, log ao vivo)
- POST /api/confirmacoes/{historico_id}/conversa      -> lê a conversa inteira (p/ o celular simulado)
"""

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from services import disparos_service
from services import painel_comunicacoes_service as svc

router = APIRouter()


@router.get("/confirmacoes", response_class=HTMLResponse)
async def confirmacoes_page(request: Request):
    pendentes = svc.listar_aguardando_confirmacao()
    confirmadas = svc.listar_confirmadas()
    template = request.app.state.jinja.get_template("confirmacoes.html")
    return HTMLResponse(template.render(
        request=request, pendentes=pendentes, confirmadas=confirmadas,
        disparos=disparos_service.listar_disparos(meio="confirmacao_leitura", limite=25)))


@router.post("/api/confirmacoes/verificar/stream")
async def api_verificar_confirmacoes():
    """Lê o WhatsApp de cada pendência (SOMENTE LEITURA) e devolve, via SSE, a lista
    pronta para a central exibir com o botão 'Confirmar recebimento' por item."""
    gen = svc.verificar_confirmacoes_stream()
    return StreamingResponse(
        disparos_service.stream_e_salvar(gen, "confirmacao_leitura"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/confirmacoes/{historico_id}/confirmar", response_class=JSONResponse)
async def api_confirmar_recebimento(historico_id: str, payload: dict = Body(default={})):
    """Ação REAL (sempre por clique do usuário): movimenta o PAJ (fase 'Juntada de
    documento', citando a mov do envio e a resposta do assistido) e dá baixa na
    tramitação da caixa de aguardo. Corpo opcional: {texto_resposta, data_resposta}
    (a última resposta lida em "Verificar respostas")."""
    try:
        r = await svc.confirmar_recebimento(
            historico_id, payload.get("texto_resposta", ""), payload.get("data_resposta", ""))
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/confirmacoes/{historico_id}/remover", response_class=JSONResponse)
async def api_remover_pendencia(historico_id: str):
    """Tira o item da lista de pendentes de verificação — local/instantâneo, SEM
    nenhuma ação no SISDPU (ex.: já foi tratado por fora, ou não se aplica mais)."""
    try:
        r = svc.remover_pendencia_confirmacao(historico_id)
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)


@router.post("/api/confirmacoes/confirmar-lote/stream")
async def api_confirmar_lote(payload: dict = Body(default={})):
    """Confirma em bloco os itens escolhidos pelo usuário. Corpo: {itens: [{historico_id,
    texto_resposta, data_resposta}, ...]}. Cada um é uma ação REAL própria no SISDPU,
    executada em sequência; progresso via SSE."""
    gen = svc.confirmar_recebimento_lote_stream(payload.get("itens") or [])
    return StreamingResponse(
        disparos_service.stream_e_salvar(gen, "confirmacao_leitura"),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.post("/api/confirmacoes/{historico_id}/conversa", response_class=JSONResponse)
async def api_conversa_whatsapp(historico_id: str, payload: dict = Body(default={})):
    """Mostra a conversa do WhatsApp (celular simulado) — botão 'Abrir conversa' na
    coluna Resposta. Por padrão usa o cache da última "Verificar respostas" (instantâneo);
    {forcar: true} (botão 'Atualizar agora' no modal) lê o WhatsApp ao vivo de novo."""
    try:
        r = await svc.obter_conversa_whatsapp(historico_id, bool(payload.get("forcar")))
    except Exception as e:  # noqa: BLE001
        r = {"ok": False, "erro": str(e)}
    return JSONResponse(r, status_code=200 if r.get("ok") else 400)
