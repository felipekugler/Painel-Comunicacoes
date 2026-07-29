"""Disparo de mensagens por MEIO de contato (App SISDPU, WhatsApp, E-mail).

ESTADO ATUAL: os disparos reais ainda NÃO estão implementados — o usuário vai
ensinar, um a um, como enviar por cada meio. Enquanto isso, o serviço opera em
MODO SIMULADO (envio_modo="simulado" na config): registra "[simulado]" e marca
o resultado como tal, permitindo validar todo o fluxo (fila → Enviar Agora →
histórico → registro no PAJ) sem disparar nada de verdade.

Para ligar um meio real: implemente o handler `_enviar_<meio>` e faça-o retornar
{"ok": True, "status": "enviado", "detalhe": "..."}. Em modo "real", meios sem
handler retornam status "nao_configurado" (não marcam como enviado).

Assinatura dos handlers: async def _enviar_x(ctx) -> dict, onde ctx traz:
  paj, paj_norm, tramite_ids[], assistido, mensagem, telefones[], emails[],
  whatsapp_numeros[], somente_leitura (bool — App SISDPU: só leitura, sem resposta),
  prazo_app / prazo_whatsapp / prazo_email (int>=1 — prazo de resposta por meio, padrão 5),
  anexos[] (cada um: nome, arquivo, tamanho, tipo, incluido_em, caminho=ABSOLUTO no disco).
"""

from __future__ import annotations

import asyncio

from services import config_service

# Meios de ENVIO suportados (ordem de exibição). Reaproveitam os ícones do
# dashboard (app_sisdpu / whatsapp / email).
MEIOS: dict[str, str] = {
    "app_sisdpu": "Aplicativo SISDPU",
    "whatsapp": "WhatsApp",
    "email": "E-mail",
}

# Rótulos de TODOS os meios (mensagem + encaminhamento: telefone/carta), só para EXIBIÇÃO
# (tooltip/legenda no histórico). ⚠️ NÃO usar `MEIOS` p/ isso — vários fluxos filtram
# `m in MEIOS` para pegar SÓ meios de mensagem (telefone/carta são tratados à parte).
MEIOS_ROTULOS: dict[str, str] = {**MEIOS, "telefone": "Ligação telefônica", "carta": "Carta",
                                 "sem_contato": "Concluído sem contato"}

# Meios SELECIONÁVEIS ao gravar/configurar. E-mail ainda NÃO implementado (depende de
# configuração externa) → fica de fora POR ORA. O rótulo continua em MEIOS para exibir
# corretamente eventuais itens/registros antigos gravados por e-mail.
MEIOS_ATIVOS: tuple[str, ...] = ("app_sisdpu", "whatsapp")


def meios_disponiveis() -> list[dict]:
    """Meios que o usuário pode ESCOLHER ao gravar, em ordem de exibição. `tipo`:
    - 'mensagem'       → envia a mensagem por esse meio (App/WhatsApp), com prazo de resposta.
    - 'encaminhamento' → roteia o PAJ para uma central (Ligação/Carta), SEM prazo; a mensagem
                         é a base do registro da ligação / do texto da carta.
    Aparecem na MESMA lista de meios, em qualquer layout (dashboard, inline, prontos)."""
    return [
        {"key": "app_sisdpu", "label": "Aplicativo SISDPU", "tipo": "mensagem"},
        {"key": "whatsapp", "label": "WhatsApp", "tipo": "mensagem"},
        {"key": "telefone", "label": "Ligação telefônica", "tipo": "encaminhamento"},
        {"key": "carta", "label": "Carta", "tipo": "encaminhamento"},
    ]


def meios_labels() -> dict:
    """Mapa key→rótulo de TODOS os meios (inclui os não-selecionáveis e os de
    encaminhamento telefone/carta), para rotular itens/registros corretamente onde só
    se precisa do NOME (tooltip/legenda). Não confundir com os meios SELECIONÁVEIS."""
    return dict(MEIOS_ROTULOS)


def label_meio(meio: str) -> str:
    return MEIOS.get(meio, meio)


# ----------------- handlers por meio -----------------
async def _enviar_app_sisdpu(ctx: dict) -> dict:
    """Envia a notificação pelo Aplicativo SISDPU (tela 'Notificar Assistido').

    Delega a `sisdpu_client.notificar_assistido`, que abre o PAJ, marca o assistido
    (o que aderiu ao App, em PAJ multi-assistido), preenche prazo/somente-leitura e
    o texto, e clica em Enviar. Devolve {ok, status, detalhe}."""
    from ingestao import sisdpu_client

    id_proc = str(ctx.get("id_processo") or "").strip()
    if not id_proc:
        return {"ok": False, "status": "falha",
                "detalhe": "PAJ sem id de processo — sincronize a caixa antes de enviar."}
    mensagem = ctx.get("mensagem") or ""
    if not mensagem.strip():
        return {"ok": False, "status": "falha", "detalhe": "Mensagem vazia."}

    return await sisdpu_client.notificar_assistido(
        id_processo=id_proc,
        mensagem=mensagem,
        prazo_dias=ctx.get("prazo_app", 5),
        somente_leitura=bool(ctx.get("somente_leitura")),
        anexos=ctx.get("anexos") or [],
    )


async def _enviar_whatsapp(ctx: dict) -> dict:
    raise NotImplementedError("Envio por WhatsApp ainda não configurado.")


async def _enviar_email(ctx: dict) -> dict:
    raise NotImplementedError("Envio por E-mail ainda não configurado.")


_HANDLERS = {
    "app_sisdpu": _enviar_app_sisdpu,
    "whatsapp": _enviar_whatsapp,
    "email": _enviar_email,
}


async def enviar(meio: str, ctx: dict) -> dict:
    """Dispara UM meio para UM PAJ. Retorna {ok, status, detalhe}.

    status ∈ {"enviado", "simulado", "nao_configurado", "falha"}.
    """
    if meio not in _HANDLERS:
        return {"ok": False, "status": "falha", "detalhe": f"Meio desconhecido: {meio}"}

    if config_service.get_envio_modo_meio(meio) == "simulado":  # modo POR MEIO
        await asyncio.sleep(0.4)  # simula latência do disparo
        return {"ok": True, "status": "simulado",
                "detalhe": f"[simulado] {label_meio(meio)} para {ctx.get('assistido') or ctx.get('paj')}"}

    try:
        res = await _HANDLERS[meio](ctx)
        res.setdefault("ok", True)
        res.setdefault("status", "enviado")
        return res
    except NotImplementedError as e:
        return {"ok": False, "status": "nao_configurado", "detalhe": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": "falha", "detalhe": str(e)}
