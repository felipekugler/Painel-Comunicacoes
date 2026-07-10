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


def meios_disponiveis() -> list[dict]:
    return [{"key": k, "label": v} for k, v in MEIOS.items()]


def label_meio(meio: str) -> str:
    return MEIOS.get(meio, meio)


# ----------------- handlers por meio (a implementar) -----------------
async def _enviar_app_sisdpu(ctx: dict) -> dict:
    raise NotImplementedError("Envio pelo Aplicativo SISDPU ainda não configurado.")


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

    if config_service.get_envio_modo() == "simulado":
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
