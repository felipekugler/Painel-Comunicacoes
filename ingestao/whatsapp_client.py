"""Cliente WhatsApp Web via Playwright (perfil persistente dedicado).

Meio de contato ASSISTIDO: o painel prepara a conversa (número + texto + anexo)
e o SERVIDOR dá o clique final em enviar; depois capturamos o print do envio.

IMPORTANTE sobre HEADED x HEADLESS:
  - O LOGIN (QR) e o uso em PRODUÇÃO precisam de janela VISÍVEL (headed) — rodam na
    máquina do servidor, pelo painel/atalho. NÃO funcionam no ambiente headless do
    dev (lá a janela não tem rede/tela).
  - Depois de logado uma vez (QR), a sessão fica no user_data_dir e pode ser reaberta
    (inclusive headless para desenvolvimento/captura de seletores).

Sessão isolada em `<PAINEL COMUNICACOES>/dados/_whatsapp_profile` — separada do SISDPU.
Só UM processo pode usar o perfil por vez (Chromium tranca o user_data_dir).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as _dt
import re
import sys
from pathlib import Path

from playwright.async_api import async_playwright

# <Projeto>/ingestao/whatsapp_client.py -> profile em <PAINEL COMUNICACOES>/dados/_whatsapp_profile
_PROJ = Path(__file__).resolve().parents[1]
WA_PROFILE_DIR = _PROJ.parent / "dados" / "_whatsapp_profile"

WA_URL = "https://web.whatsapp.com"

# Usar o CHROME INSTALADO (channel), NÃO o Chromium embutido do Playwright: nesta
# máquina/rede (DPU) o Chromium avulso em headed cai em ERR_NETWORK_ACCESS_DENIED,
# enquanto o Chrome/Edge instalado tem rede normal (confirmado por diagnóstico).
WA_CHANNEL = "chrome"


def sessao_salva() -> bool:
    """Readiness LEVE do WhatsApp, SEM abrir o navegador (o perfil persistente é
    travado por 1 processo, então não dá para "pingar" a sessão a cada polling).
    True se o perfil já foi usado/logado alguma vez — o WhatsApp Web mantém a sessão
    salva no user_data_dir. Não garante que a sessão siga válida; para reconectar,
    use o login por QR."""
    try:
        if not WA_PROFILE_DIR.exists():
            return False
        return any((WA_PROFILE_DIR / m).exists() for m in ("Default", "Local State"))
    except Exception:  # noqa: BLE001
        return False


async def abrir_contexto(p, headless: bool = True):
    """Abre o contexto persistente do WhatsApp (perfil dedicado + Chrome instalado).
    headless=False p/ o QR e produção (na máquina do servidor); headless=True p/ o
    assistente reabrir a sessão já logada e desenvolver/testar."""
    WA_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return await p.chromium.launch_persistent_context(
        str(WA_PROFILE_DIR),
        channel=WA_CHANNEL,
        headless=headless,
        args=["--disable-blink-features=AutomationControlled"],
        viewport={"width": 1200, "height": 840},
    )

# Detecta a interface logada (lista de conversas). WhatsApp muda o DOM com
# frequência — mantemos vários sinais.
_JS_LOGADO = r"""
() => !!(document.querySelector('#pane-side')
      || document.querySelector('[aria-label="Lista de conversas"]')
      || document.querySelector('[aria-label="Chat list"]')
      || document.querySelector('header [data-icon="new-chat-outline"]'))
"""


def normalizar_numero(raw: str) -> str:
    """Número BR -> dígitos internacionais para o wa/whatsapp send.

    Regra (herdada do painel): decide pelo COMPRIMENTO. 10/11 dígitos (DDD+número)
    -> prefixa '55'. NÃO checar prefixo '55' cegamente (DDD 55 = Santa Maria/RS
    existe). Se já vier com 12/13 dígitos, assume que já tem o país.
    """
    d = re.sub(r"\D", "", raw or "")
    if not d:
        return ""
    if len(d) in (10, 11):          # DDD (2) + número (8/9)
        return "55" + d
    if len(d) in (12, 13) and d.startswith("55"):
        return d
    return d                         # deixa como veio (casos atípicos)


async def esta_logado(page) -> bool:
    try:
        return bool(await page.evaluate(_JS_LOGADO))
    except Exception:
        return False


async def login_interativo(timeout_seg: int = 600) -> bool:
    """Abre o WhatsApp Web HEADED e espera o usuário escanear o QR.
    RODAR NA MÁQUINA DO SERVIDOR (headed precisa de tela+rede reais).
    Ao detectar login, a sessão já está persistida no perfil; fecha e retorna True."""
    print(f"[wa] perfil: {WA_PROFILE_DIR} (Chrome instalado, channel={WA_CHANNEL})", flush=True)
    async with async_playwright() as p:
        ctx = await abrir_contexto(p, headless=False)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await page.goto(WA_URL, wait_until="domcontentloaded", timeout=60000)
        print(">>> Escaneie o QR com o telefone do NÚMERO DEDICADO "
              "(WhatsApp > Aparelhos conectados > Conectar aparelho). <<<", flush=True)
        logado = False
        for i in range(max(1, timeout_seg // 2)):
            await page.wait_for_timeout(2000)
            if await esta_logado(page):
                logado = True
                break
            if i % 5 == 0:
                print(f"[wa] aguardando login... ({i * 2}s)", flush=True)
        if logado:
            print("[wa] LOGADO! Sessão salva no perfil (pode fechar e reabrir depois).", flush=True)
            await page.wait_for_timeout(5000)  # garante gravação do IndexedDB da sessão
        else:
            print("[wa] NÃO detectei login no tempo limite. Rode novamente para tentar.", flush=True)
        await ctx.close()
        return logado


# ---------------- Seletores do WhatsApp Web (capturados ao vivo) ----------------
_SEL_COMPOSE = 'footer div[contenteditable="true"][role="textbox"]'
_SEL_SEND_FOOTER = 'footer [aria-label="Enviar"]'          # aparece após digitar texto
_SEL_ATTACH = 'footer [aria-label="Anexar"]'               # botão "+" (data-icon plus-rounded)
_SEL_CAPTION = 'div[contenteditable="true"][aria-label="Digite uma mensagem"]'  # legenda no preview
_SEL_SEND_PREVIEW = '[aria-label^="Enviar"]'               # enviar na tela de preview do anexo


async def _numero_invalido(page) -> bool:
    try:
        return bool(await page.evaluate(
            r"""() => [...document.querySelectorAll('[role="dialog"]')]
                    .some(d => /n[ãa]o est[áa] no WhatsApp/i.test(d.textContent || ''))"""))
    except Exception:
        return False


# ---------------- Abertura de conversa: número(s) cadastrado(s) + descoberta por PAJ ----------------
# Problema real relatado pelo usuário (2026-10-28): em vários casos a leitura volta
# SEM mensagem nenhuma. Duas causas, as DUAS confirmadas ao vivo com o usuário:
#   1) o número cadastrado não bate EXATAMENTE com o JID real do WhatsApp do contato
#      (9º dígito, número desatualizado) — o link "send?phone=" abre uma conversa
#      NOVA/VAZIA em vez da real;
#   2) o painel às vezes manda a MESMA mensagem para VÁRIOS números do mesmo
#      assistido, e nunca se sabe se a resposta vai vir por um, por todos, ou por
#      nenhum — só olhar `numeros[0]` perde respostas que vieram pelos outros.
# Solução: a busca GLOBAL do WhatsApp pelo padrão "PAJ AAAA/UUU-NNNNN" (validado AO
# VIVO pelo usuário, 2026-10-28 — é o formato que funciona de forma confiável, mais
# do que só o número solto) mostra TODAS as conversas relacionadas ao PAJ — cada uma
# identificada pelo próprio número de telefone quando o contato não está salvo.
# Extraímos esses números e verificamos CADA UM (mais os já cadastrados no histórico),
# agregando: qualquer um com resposta conta.
_SEL_BUSCA_GLOBAL = 'input[aria-label="Pesquisar ou começar uma nova conversa"]'
_SEL_RESULTADO_BUSCA = '#pane-side [role="listitem"], #pane-side div[role="row"]'
_RE_TEL_BR = re.compile(r"\+?\s*55\s*\(?\d{2}\)?\s*\d{4,5}-?\d{4}")


async def _contagem_mensagens(page) -> int:
    try:
        return await page.locator("#main [data-id]").count()
    except Exception:
        return 0


async def _fechar_dialogo_numero_invalido(page) -> None:
    """Fecha o diálogo "não está no WhatsApp" (botão "OK") — ele cobre a tela e
    bloqueia qualquer clique seguinte (ex.: na busca global). Bug real encontrado ao
    vivo 2026-10-28."""
    with contextlib.suppress(Exception):
        await page.get_by_role("button", name="OK", exact=True).first.click(timeout=3000)
    await page.wait_for_timeout(300)


async def _abrir_numero_direto(page, numero: str) -> bool:
    """Abre a conversa de `numero` direto pelo link do WhatsApp (sem busca). Retorna
    True só se abriu E tem pelo menos 1 mensagem (uma conversa vazia é sinal de
    número errado/desatualizado — não conta como "aberta")."""
    intl = normalizar_numero(numero)
    if not intl:
        return False
    await page.goto(f"{WA_URL}/send?phone={intl}", wait_until="domcontentloaded", timeout=60000)
    aberto = False
    for _ in range(30):
        await page.wait_for_timeout(1000)
        if await _numero_invalido(page):
            await _fechar_dialogo_numero_invalido(page)
            break
        if await page.locator(_SEL_COMPOSE).count():
            aberto = True
            break
    if not aberto:
        return False
    await page.wait_for_timeout(800)
    return bool(await _contagem_mensagens(page))


async def _ir_para_busca_global(page) -> None:
    """Garante que a busca global esteja acessível na tela — navega pra base do
    WhatsApp se a página ainda não carregou nada. Espera ATIVA (até ~15s): carregar o
    app do ZERO é mais lento que abrir uma conversa específica (bug real encontrado
    ao vivo 2026-10-28: um sleep fixo curto não bastava)."""
    if await page.locator(_SEL_BUSCA_GLOBAL).count():
        return
    with contextlib.suppress(Exception):
        await page.goto(WA_URL, wait_until="domcontentloaded", timeout=60000)
        for _ in range(15):
            await page.wait_for_timeout(1000)
            if await page.locator(_SEL_BUSCA_GLOBAL).count():
                break


async def _descobrir_numeros_por_paj(page, paj: str) -> list[str]:
    """Busca "PAJ <numero>" (padrão validado AO VIVO pelo usuário, 2026-10-28 — mais
    confiável que só o número solto) na busca GLOBAL do WhatsApp e extrai os NÚMEROS
    reais das conversas encontradas — até números que não estão cadastrados no painel
    (contato não salvo = WhatsApp mostra o próprio número como título do resultado).
    Não abre nada; só descobre candidatos. Requer que a busca já esteja acessível na
    tela (ver `_ir_para_busca_global`)."""
    paj = (paj or "").strip()
    if not paj:
        return []
    query = paj if paj.upper().startswith("PAJ") else f"PAJ {paj}"
    try:
        campo = page.locator(_SEL_BUSCA_GLOBAL)
        if not await campo.count():
            return []
        await campo.click()
        await campo.fill("")
        await campo.fill(query)
        await page.wait_for_timeout(1500)
        itens = page.locator(_SEL_RESULTADO_BUSCA)
        n = await itens.count()
        numeros: list[str] = []
        for i in range(min(n, 30)):
            texto = await itens.nth(i).inner_text() or ""
            m = _RE_TEL_BR.search(texto)
            if not m:
                continue
            cand = normalizar_numero(m.group(0))
            if cand and cand not in numeros:
                numeros.append(cand)
        with contextlib.suppress(Exception):
            await campo.fill("")  # limpa a busca p/ não atrapalhar o próximo uso da página
        return numeros
    except Exception:  # noqa: BLE001
        return []


async def _ler_conversas_do_paj(page, numeros_registrados: list[str] | None, paj_ref: str,
                                max_mensagens: int) -> dict:
    """Lê TODAS as conversas relacionadas ao PAJ: os números já cadastrados no
    histórico MAIS os descobertos pela busca "PAJ <numero>" (pode haver mais de um —
    ver comentário acima de `_SEL_BUSCA_GLOBAL`). Para cada candidato que abrir
    (com pelo menos 1 mensagem), lê via `_JS_LER_CHAT`.
    Retorna {ok, dumps: {numero: dump}, numeros_verificados: [...], detalhe}."""
    candidatos = [normalizar_numero(n) for n in (numeros_registrados or []) if n]
    candidatos = [n for n in candidatos if n]
    if paj_ref:
        await _ir_para_busca_global(page)
        for n in await _descobrir_numeros_por_paj(page, paj_ref):
            if n not in candidatos:
                candidatos.append(n)
    if not candidatos:
        return {"ok": False, "dumps": {}, "numeros_verificados": [],
                "detalhe": "Sem número cadastrado e nada encontrado na busca pelo PAJ."}
    dumps: dict[str, dict] = {}
    verificados: list[str] = []
    for numero in candidatos:
        if await _abrir_numero_direto(page, numero):
            verificados.append(numero)
            dumps[numero] = await page.evaluate(_JS_LER_CHAT, max_mensagens)
    if not verificados:
        return {"ok": False, "dumps": {}, "numeros_verificados": [],
                "detalhe": f"Não consegui abrir nenhuma conversa ({len(candidatos)} "
                           "candidato(s) tentado(s): cadastrado(s) e/ou achado(s) na busca pelo PAJ)."}
    return {"ok": True, "dumps": dumps, "numeros_verificados": verificados, "detalhe": ""}


def _processar_dump_resposta(dump: dict | None) -> dict:
    """Extrai tem_resposta/texto_resposta/data_resposta de UM dump de `_JS_LER_CHAT`
    (lógica usada por `ler_respostas_apos_envio`, isolada para rodar por conversa
    quando há mais de um número candidato ao mesmo PAJ)."""
    vazio = {"tem_resposta": False, "texto_resposta": "", "data_resposta": ""}
    if not dump or dump.get("erro") or not dump.get("achou_nossa_mensagem"):
        return vazio
    respostas = dump.get("respostas") or []
    if not respostas:
        return vazio
    linhas_resposta = []
    for r in respostas:
        texto = (r.get("texto") or "").strip()
        if not texto:
            continue
        m_hora = re.search(r"\[(\d{2}:\d{2}),", r.get("preplain") or "")
        hora = m_hora.group(1) if m_hora else ""
        if not hora:
            m_fim = re.search(r"(\d{2}:\d{2})\s*$", texto)
            hora = m_fim.group(1) if m_fim else ""
        if hora:
            texto = re.sub(r"\n?\s*" + re.escape(hora) + r"\s*$", "", texto).strip()
        if not texto:
            continue
        linhas_resposta.append(f"{hora} {texto}" if hora else texto)
    texto_resposta = "\n".join(linhas_resposta).strip()
    data_resposta = ""
    for r in reversed(respostas):  # última data-hora disponível (preplain pode faltar em bolhas agrupadas)
        m = re.search(r"\[(\d{2}:\d{2}), (\d{2}/\d{2}/\d{4})\]", r.get("preplain") or "")
        if m:
            data_resposta = f"{m.group(2)} {m.group(1)}"
            break
    return {"tem_resposta": bool(texto_resposta), "texto_resposta": texto_resposta,
            "data_resposta": data_resposta}


# Lê as ÚLTIMAS mensagens de um chat (nossa + do assistido), identificando as NOSSAS
# pelo mesmo sinal já usado em _confirmar_enviado (ícone de saída "tail-out" — validado
# ao vivo 2026-10-28: o WhatsApp Web atual marca mensagens enviadas por nós com
# [data-icon="tail-out"] dentro da bolha; não há um "tail-in" equivalente confiável
# para o lado recebido, então tratamos "tudo que vem DEPOIS da nossa última bolha"
# como a resposta do assistido, em vez de tentar classificar bolha a bolha).
# Uma ÚNICA leitura serve a DOIS propósitos (evita abrir duas sessões do WhatsApp
# para a mesma conversa): "respostas" (só o que vem depois da nossa última mensagem,
# usado por ler_respostas_apos_envio) e "mensagens" (a conversa INTEIRA, nossa/dele,
# usada pelo celular simulado em obter_conversa — e cacheada no histórico a cada
# "Verificar respostas", pedido do usuário em 2026-10-28, p/ "Abrir conversa" não
# precisar abrir outra sessão toda vez).
_JS_LER_CHAT = r"""
(maxMsgs) => {
    const main = document.querySelector('#main');
    if (!main) return {erro: 'sem #main'};
    const rows = [...main.querySelectorAll('[data-id]')].slice(-maxMsgs);
    const autorDe = (preplain) => {
        const i = preplain.indexOf('] ');
        if (i < 0) return '';
        return preplain.slice(i + 2).replace(/:\s*$/, '').trim();
    };
    const itens = rows.map(el => {
        const tailOut = !!el.querySelector('[data-icon="tail-out"]');
        const copyable = el.querySelector('.copyable-text');
        const preplain = copyable ? (copyable.getAttribute('data-pre-plain-text') || '') : '';
        const autor = autorDe(preplain);
        const mHora = preplain.match(/\[(\d{2}:\d{2}),/);
        let hora = mHora ? mHora[1] : '';
        let texto = (el.innerText || '').trim();
        const mFim = texto.match(/(\d{2}:\d{2})\s*$/);
        if (!hora && mFim) hora = mFim[1];
        if (hora) texto = texto.replace(new RegExp('\\n?\\s*' + hora + '\\s*$'), '').trim();
        const temMidia = !!el.querySelector('[data-icon="document-PDF-icon"], [data-icon="media-play"], img');
        return {tailOut, preplain, autor, hora, texto, temMidia};
    });
    // O ícone "tail-out" (mensagem enviada por nós) só aparece na PRIMEIRA bolha de
    // uma sequência seguida do mesmo remetente — as seguintes ficam "agrupadas" sem
    // o ícone, mas o atributo de data/autor (data-pre-plain-text) aparece em TODAS.
    // Por isso usamos o NOME do autor (constante em toda sequência nossa, inclusive
    // em mensagens de outro PAJ enviadas em seguida), e não só o ícone, para não
    // confundir uma continuação de mensagem NOSSA com uma resposta do assistido.
    let nomeNosso = '';
    for (const it of itens) { if (it.tailOut && it.autor) nomeNosso = it.autor; }
    // Mídia/anexo SEM legenda não tem .copyable-text (preplain vazio) — sem autor E
    // às vezes sem tail-out (bolha agrupada), não dá pra saber a direção por esses
    // sinais. Nesse caso herda a direção da mensagem ANTERIOR — MAS só se o horário
    // for COMPATÍVEL (poucos minutos de diferença), ou seja, só se fizer parte da
    // MESMA rodada de envio; um gap grande não herda (evita herdar de algo distante
    // no tempo). Sem essa checagem/sem horário em algum dos dois lados: não herda,
    // cai no valor seguro (não-nossa). Bug real encontrado ao vivo 2026-10-28: um PDF
    // nosso sem legenda, mandado logo após outra mensagem nossa, aparecia como se
    // fosse do assistido no celular simulado (a confirmação de leitura não era afetada,
    // pois o filtro de "respostas" já exige autor identificado — só o visual errava).
    const JANELA_MESMA_RODADA_MIN = 2;
    const horaParaMin = (h) => {
        const m = /^(\d{2}):(\d{2})$/.exec(h || '');
        return m ? (parseInt(m[1], 10) * 60 + parseInt(m[2], 10)) : null;
    };
    let anteriorEhNossa = null;
    let anteriorHoraMin = null;
    const ehNossaPorIdx = itens.map(it => {
        let ehNossa;
        const horaMin = horaParaMin(it.hora);
        if (it.tailOut) {
            ehNossa = true;
        } else if (it.autor) {
            ehNossa = (!!nomeNosso && it.autor === nomeNosso);
        } else {
            let diff = (anteriorHoraMin !== null && horaMin !== null) ? Math.abs(horaMin - anteriorHoraMin) : null;
            if (diff !== null) diff = Math.min(diff, 1440 - diff);  // virada de meia-noite
            const mesmaRodada = anteriorEhNossa !== null && diff !== null && diff <= JANELA_MESMA_RODADA_MIN;
            ehNossa = mesmaRodada ? anteriorEhNossa : false;
        }
        anteriorEhNossa = ehNossa;
        if (horaMin !== null) anteriorHoraMin = horaMin;
        return ehNossa;
    });
    let ultimoNossoIdx = -1;
    itens.forEach((it, i) => { if (ehNossaPorIdx[i]) ultimoNossoIdx = i; });
    const depois = ultimoNossoIdx >= 0 ? itens.slice(ultimoNossoIdx + 1) : [];
    const respostas = depois.filter(it => it.autor && it.autor !== nomeNosso);
    const mensagens = itens
        .map((it, i) => ({ nossa: !!ehNossaPorIdx[i],
                       texto: it.texto || (it.temMidia ? '[mídia/anexo]' : ''), hora: it.hora, temMidia: it.temMidia }))
        .filter(it => it.texto || it.temMidia);
    return {
        total_lido: itens.length,
        achou_nossa_mensagem: ultimoNossoIdx >= 0,
        respostas: respostas.map(it => ({texto: it.texto, preplain: it.preplain})),
        mensagens,
    };
}
"""


async def ler_respostas_apos_envio(numero: str, max_mensagens: int = 60, headless: bool = True,
                                   paj_ref: str = "", numeros_registrados: list[str] | None = None) -> dict:
    """Lê TODAS as conversas relacionadas ao PAJ (o(s) número(s) cadastrado(s) MAIS
    o(s) descoberto(s) pela busca "PAJ <numero>" — ver `_ler_conversas_do_paj`) e
    procura mensagens recebidas DEPOIS da NOSSA última mensagem enviada em QUALQUER
    uma delas — para checar se o assistido respondeu confirmando o recebimento (ex.:
    "OK"), não importa por qual número. Operação SOMENTE LEITURA: não digita nem
    envia nada.
    `numero`: número "principal" já cadastrado (pode ser vazio se só `paj_ref` for
    conhecido). `numeros_registrados` (opcional): outros números já cadastrados para
    o mesmo PAJ, verificados junto. `paj_ref`: PAJ usado para descobrir mais números.

    Retorna {ok, tem_resposta, texto_resposta, data_resposta, confirmou, mensagens, detalhe}:
      - tem_resposta: True se há ao menos uma mensagem do assistido após a nossa, em
        qualquer uma das conversas verificadas;
      - texto_resposta: texto concatenado das respostas; quando mais de uma conversa
        tem resposta, cada uma vem prefixada por "[<numero>]" numa linha própria;
      - data_resposta: 'DD/MM/AAAA HH:MM' extraída da resposta mais recente entre
        todas as conversas, se disponível;
      - confirmou: True sempre que há QUALQUER resposta do assistido em qualquer
        conversa relacionada ao PAJ (a direção já é identificada com segurança pelo
        autor — não exige mais bater com uma palavra específica como "OK"). É só uma
        PRÉ-SELEÇÃO; a decisão final é sempre do usuário (ver central de Confirmações
        de leitura).
      - mensagens: a conversa (nossa/dele/texto/hora) da conversa com resposta — ou,
        se nenhuma tem resposta, da primeira conversa verificada — permite cachear
        sem abrir outra sessão do WhatsApp só para o botão "Abrir conversa" (ver
        `obter_conversa_whatsapp` no serviço)."""
    candidatos = [numero] + list(numeros_registrados or [])
    async with async_playwright() as p:
        ctx = await abrir_contexto(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            leitura = await _ler_conversas_do_paj(page, candidatos, paj_ref, max_mensagens)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detalhe": f"exceção ao ler o chat: {e}"}
        finally:
            with contextlib.suppress(Exception):
                await ctx.close()

    if not leitura.get("ok"):
        return {"ok": False, "detalhe": leitura.get("detalhe", "")}
    dumps = leitura["dumps"]
    numeros_verificados = leitura["numeros_verificados"]
    metodo = "numero" if numero and normalizar_numero(numero) in numeros_verificados else "busca_texto"

    processados = {n: _processar_dump_resposta(d) for n, d in dumps.items()}
    com_resposta = [n for n in numeros_verificados if processados[n]["tem_resposta"]]
    dump_ref = dumps.get(com_resposta[0]) if com_resposta else next(iter(dumps.values()), {})
    mensagens_ref = (dump_ref or {}).get("mensagens") or []

    if not any(dumps[n].get("achou_nossa_mensagem") for n in numeros_verificados):
        return {"ok": True, "tem_resposta": False, "texto_resposta": "", "data_resposta": "",
                "confirmou": False, "mensagens": mensagens_ref, "metodo_abertura": metodo,
                "detalhe": "Não encontrei nossa última mensagem enviada nas últimas "
                           f"{max_mensagens} mensagens de nenhuma conversa verificada "
                           f"({len(numeros_verificados)}) — pode haver mais conversa depois "
                           "do envio; confira manualmente."}
    if not com_resposta:
        return {"ok": True, "tem_resposta": False, "texto_resposta": "", "data_resposta": "",
                "confirmou": False, "mensagens": mensagens_ref, "metodo_abertura": metodo,
                "detalhe": "Ainda sem resposta do assistido (verificado em "
                           f"{len(numeros_verificados)} conversa(s))."}
    multiplas = len(com_resposta) > 1
    textos = []
    for n in com_resposta:
        t = processados[n]["texto_resposta"]
        textos.append(f"[{n}]\n{t}" if multiplas else t)
    texto_resposta = "\n\n".join(textos).strip()
    data_resposta = max((processados[n]["data_resposta"] for n in com_resposta if processados[n]["data_resposta"]),
                         default="")
    mensagens_ref = dumps[com_resposta[0]].get("mensagens") or []
    return {"ok": True, "tem_resposta": True, "texto_resposta": texto_resposta,
            "data_resposta": data_resposta, "confirmou": True, "mensagens": mensagens_ref,
            "metodo_abertura": metodo, "detalhe": ""}


async def obter_conversa(numero: str, max_mensagens: int = 60, headless: bool = True,
                         paj_ref: str = "", numeros_registrados: list[str] | None = None) -> dict:
    """Lê a conversa INTEIRA (nossas mensagens + respostas) relacionada ao PAJ, para
    exibição num celular simulado — ex.: botão "Abrir conversa"/"Atualizar agora" na
    central de Confirmações de leitura (leitura ao vivo; o caminho "normal" é o cache
    gravado por `ler_respostas_apos_envio` a cada "Verificar respostas" — ver
    `obter_conversa_whatsapp` no serviço). Operação SOMENTE LEITURA: não digita nem
    envia nada. Quando há mais de uma conversa relacionada ao PAJ (ver
    `_ler_conversas_do_paj`), mostra a que tiver resposta do assistido; sem nenhuma
    resposta, mostra a primeira verificada.
    Retorna {ok, numero, mensagens:[{nossa,texto,hora}], detalhe}."""
    candidatos = [numero] + list(numeros_registrados or [])
    async with async_playwright() as p:
        ctx = await abrir_contexto(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            leitura = await _ler_conversas_do_paj(page, candidatos, paj_ref, max_mensagens)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "detalhe": f"exceção ao ler o chat: {e}"}
        finally:
            with contextlib.suppress(Exception):
                await ctx.close()
    if not leitura.get("ok"):
        return {"ok": False, "detalhe": leitura.get("detalhe", "")}
    dumps = leitura["dumps"]
    numeros_verificados = leitura["numeros_verificados"]
    com_resposta = [n for n in numeros_verificados if _processar_dump_resposta(dumps[n])["tem_resposta"]]
    numero_escolhido = com_resposta[0] if com_resposta else numeros_verificados[0]
    return {"ok": True, "numero": numero_escolhido,
            "mensagens": dumps[numero_escolhido].get("mensagens") or [], "detalhe": ""}


async def _confirmar_enviado(page, texto: str = "", timeout_seg: int = 20) -> bool:
    """Confirma que a mensagem SAIU. O DOM do WhatsApp é ofuscado (classes atômicas
    da Meta); em vez de classes instáveis, checamos:
      - se há texto: ele aparece em #main (área de mensagens) e NÃO no compose (rodapé);
      - sem texto (anexo sem legenda): presença de bolha de saída (`data-icon=tail-out`).
    """
    # Normaliza removendo EMOJIS/pontuação (o WhatsApp renderiza emoji como <img>, que
    # some do innerText → comparar com o texto cru dava falso-negativo, ex.: mensagem
    # iniciada por "⚠️"). Mantém letras (c/ acento), números e espaço, nos DOIS lados.
    snip = re.sub(r"\s+", " ", re.sub(r"[^0-9A-Za-zÀ-ÿ ]+", " ", texto or "")).strip()[:24]
    js_texto = r"""(snip) => {
        const main = document.querySelector('#main'); if (!main) return false;
        const clean = s => (s||'').replace(/[^0-9A-Za-zÀ-ÿ ]+/g, ' ').replace(/\s+/g, ' ').trim();
        const foot = main.querySelector('footer');
        const footTxt = clean(foot ? (foot.innerText || '') : '');
        const mainTxt = clean(main.innerText || '');
        return snip.length > 0 && mainTxt.includes(snip) && !footTxt.includes(snip);
    }"""
    js_tail = r"""() => !!document.querySelector('#main [data-icon="tail-out"]')"""
    for _ in range(max(1, timeout_seg * 2)):
        await page.wait_for_timeout(500)
        try:
            if snip:
                if await page.evaluate(js_texto, snip):
                    return True
            elif await page.evaluate(js_tail):
                return True
        except Exception:
            pass
    return False


async def _digitar_enviar(page, texto: str, timeout_seg: int = 20) -> bool:
    """Digita `texto` no compose do chat já aberto e envia (botão Enviar; Enter como
    fallback). SEGURO PARA MULTILINHA: as quebras de linha do texto são inseridas com
    Shift+Enter (senão o Enter enviaria a mensagem fragmentada). Retorna True se
    confirmou o envio. Usado para todas as mensagens digitadas (referência, texto, prazo)."""
    compose = page.locator(_SEL_COMPOSE).first
    await compose.click()
    await page.wait_for_timeout(300)
    linhas = (texto or "").split("\n")
    for i, ln in enumerate(linhas):
        if i > 0:  # nova linha DENTRO da mensagem, sem enviar
            await page.keyboard.down("Shift")
            await page.keyboard.press("Enter")
            await page.keyboard.up("Shift")
        if ln:
            await page.keyboard.type(ln)
    await page.wait_for_timeout(400)
    botao = page.locator(_SEL_SEND_FOOTER)
    if await botao.count():
        await botao.first.click()
    else:
        await page.keyboard.press("Enter")
    ok = await _confirmar_enviado(page, texto, timeout_seg=timeout_seg)
    await page.wait_for_timeout(600)
    return ok


# ---------------- Comprovante (PDF de 2 páginas, sem libs externas) ----------------
def _jpeg_dims(data: bytes) -> tuple[int, int]:
    """Largura x altura de um JPEG (marcador SOF)."""
    i = 2
    while i < len(data) - 9:
        if data[i] != 0xFF:
            i += 1
            continue
        m = data[i + 1]
        if m in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB):
            return ((data[i + 7] << 8) | data[i + 8], (data[i + 5] << 8) | data[i + 6])
        if 0xD0 <= m <= 0xD9 or m == 0x01:
            i += 2
            continue
        i += 2 + ((data[i + 2] << 8) | data[i + 3])
    return (1000, 1400)


def _jpegs_to_pdf(jpegs: list[bytes], path: str) -> None:
    """Monta um PDF (1 página por JPEG) embutindo via /DCTDecode. Sem dependências."""
    objs: dict[int, bytes] = {}
    page_ids: list[int] = []
    nid = 3
    for data in jpegs:
        w, h = _jpeg_dims(data)
        pid, iid, cid = nid, nid + 1, nid + 2
        nid += 3
        page_ids.append(pid)
        content = f"q\n{w} 0 0 {h} 0 0 cm\n/Im0 Do\nQ".encode()
        objs[pid] = (f"{pid} 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w} {h}] "
                     f"/Resources << /XObject << /Im0 {iid} 0 R >> >> /Contents {cid} 0 R >>\nendobj\n").encode()
        objs[iid] = (f"{iid} 0 obj\n<< /Type /XObject /Subtype /Image /Width {w} /Height {h} "
                     f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /DCTDecode /Length {len(data)} >>\n"
                     .encode() + b"stream\n" + data + b"\nendstream\nendobj\n")
        objs[cid] = (f"{cid} 0 obj\n<< /Length {len(content)} >>\n".encode()
                     + b"stream\n" + content + b"\nendstream\nendobj\n")
    objs[1] = b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objs[2] = f"2 0 obj\n<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>\nendobj\n".encode()
    pdf, offs = bytearray(b"%PDF-1.4\n"), {}
    for i in range(1, nid):
        offs[i] = len(pdf)
        pdf.extend(objs[i])
    xref = len(pdf)
    pdf.extend(f"xref\n0 {nid}\n0000000000 65535 f \n".encode())
    for i in range(1, nid):
        pdf.extend(f"{offs[i]:010d} 00000 n \n".encode())
    pdf.extend(f"trailer\n<< /Size {nid} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
    Path(path).write_bytes(pdf)


_JS_DRAWER_CONTATO = r"""
() => {
    const vw = innerWidth; let best = null, area = 0;
    for (const el of document.querySelectorAll('div,section')) {
        const r = el.getBoundingClientRect();
        if (r.left > vw * 0.35 && r.width > 300 && r.height > 500 &&
            /Dados do contato|Dados da empresa/i.test(el.textContent || '')) {
            const a = r.width * r.height;
            if (a > area) { area = a; best = el; }
        }
    }
    return best;
}
"""


# Localiza o container ROLÁVEL da conversa (dentro de #main) — o de maior scrollHeight.
_JS_FIND_SCROLLER = r"""
  const _main = document.querySelector('#main');
  let sc = null;
  if (_main) {
    const cands = [..._main.querySelectorAll('div')].filter(d => {
      const s = getComputedStyle(d);
      return (s.overflowY === 'scroll' || s.overflowY === 'auto') && d.scrollHeight > d.clientHeight + 20;
    });
    cands.sort((a, b) => b.scrollHeight - a.scrollHeight);
    sc = cands[0] || null;
  }
"""
_JS_CONV_SCROLLHEIGHT = "() => {" + _JS_FIND_SCROLLER + " return sc ? sc.scrollHeight : 0; }"
_JS_CONV_INFO = "() => {" + _JS_FIND_SCROLLER + r"""
  if (!sc) return null;
  const r = sc.getBoundingClientRect();
  return { scrollHeight: sc.scrollHeight, clientHeight: sc.clientHeight,
           rect: { x: r.x, y: r.y, width: r.width, height: r.height } };
}"""
_JS_CONV_SET_SCROLL = "(pos) => {" + _JS_FIND_SCROLLER + " if (sc) sc.scrollTop = pos; return sc ? sc.scrollTop : -1; }"

WA_COMPROVANTE_MAX_PAGINAS = 6   # teto de fatias (páginas) da conversa no comprovante
WA_COMPROVANTE_ZOOM = 0.6         # CSS zoom na captura — cabe mais conteúdo por página, texto legível


async def _capturar_conversa_fatias(page, baseline_h) -> list[bytes]:
    """Captura a conversa em FATIAS (uma por página do PDF), rolando do INÍCIO do bloco
    ENVIADO (baseline_h = altura da conversa ANTES do envio) até o fim. Assim o
    comprovante contempla TUDO o que foi enviado (mensagem longa + anexos) — e não só a
    parte final, que era o que o print de #main pegava (altura fixa = viewport)."""
    info0 = await page.evaluate(_JS_CONV_INFO)              # ANTES do zoom (converter baseline)
    sh0 = (info0 or {}).get("scrollHeight") or 0
    fatias: list[bytes] = []
    try:
        # CSS zoom: reduz o tamanho do conteúdo → cabe MUITO mais mensagem por print,
        # texto ainda legível (validado a 60%). Cada fatia = screenshot do #main.
        await page.evaluate("(z)=>{document.body.style.zoom=z;}", WA_COMPROVANTE_ZOOM)
        await page.wait_for_timeout(900)
        info = await page.evaluate(_JS_CONV_INFO)
        if not info or not info.get("clientHeight"):
            return [bytes(await page.locator("#main").first.screenshot(type="jpeg", quality=85))]
        total = info["scrollHeight"]; viewH = info["clientHeight"]
        ratio = (total / sh0) if sh0 else WA_COMPROVANTE_ZOOM   # baseline (não-zoom) → coords do zoom
        bottom = max(0, total - viewH)
        if baseline_h and float(baseline_h) > 0:
            region_top = max(0.0, min(float(baseline_h) * ratio - 40, bottom))  # topo do bloco enviado
        else:
            region_top = max(0.0, bottom - viewH)  # sem baseline: as 2 últimas telas
        posicoes, pos = [], region_top
        for _ in range(WA_COMPROVANTE_MAX_PAGINAS):
            posicoes.append(pos)
            if pos >= bottom:
                break
            prox = min(pos + viewH, bottom)
            if prox <= pos:
                break
            pos = prox
        for i, pos in enumerate(posicoes):
            if i == len(posicoes) - 1:
                # ÚLTIMA fatia: é onde está a resposta/mensagem mais recente — o que
                # importa de verdade para quem vai ler o PDF depois. NÃO confia no
                # "bottom" calculado acima (reflow do zoom pode deixá-lo impreciso em
                # alguns pixels, cortando a última linha); força o scroll para o
                # MÁXIMO de verdade (o navegador satura o valor), duas vezes, com
                # espera maior — garante a mensagem mais recente sempre visível por
                # inteiro. Bug relatado pelo usuário 2026-10-28 (resposta cortada).
                await page.evaluate(_JS_CONV_SET_SCROLL, 10_000_000)
                await page.wait_for_timeout(700)
                await page.evaluate(_JS_CONV_SET_SCROLL, 10_000_000)
                await page.wait_for_timeout(600)
            else:
                await page.evaluate(_JS_CONV_SET_SCROLL, pos)
                await page.wait_for_timeout(500)  # deixa o WhatsApp renderizar (lista virtualizada)
            with contextlib.suppress(Exception):
                fatias.append(bytes(await page.locator("#main").first.screenshot(type="jpeg", quality=85)))
    finally:
        with contextlib.suppress(Exception):
            await page.evaluate("()=>{document.body.style.zoom='';}")  # restaura o zoom
    return fatias or [bytes(await page.locator("#main").first.screenshot(type="jpeg", quality=85))]


async def _capturar_contato_jpeg(page) -> dict:
    """Abre o painel "Dados do contato" (clica, espera, fecha) e captura DUAS coisas
    de uma vez, já que a abertura/fechamento do painel é o passo caro/frágil:
      - `jpeg`: screenshot do painel — página 1 dos PDFs gerados a partir da conversa
        (comprovante de envio e PDF da resposta);
      - `numero`: o número de telefone no formato RECONHECIDO PELO WHATSAPP (ground
        truth — inclui qualquer ajuste de DDI/9º dígito que o WhatsApp aplicou ao
        abrir a conversa), extraído do texto do próprio painel. Mais confiável que
        reconstruir o número a partir do que foi digitado/cadastrado no contato —
        é esse número que deve ser usado para reabrir a conversa depois (pedido do
        usuário, 2026-10-28: viu o PDF do comprovante mostrar o número num formato
        diferente do cadastrado, e só a busca por ESSE número — não pelo cadastrado
        nem por texto do PAJ — achou a conversa de forma 100% confiável).
    Retorna {"jpeg": bytes|None, "numero": str}."""
    jpeg, numero = None, ""
    with contextlib.suppress(Exception):
        await page.locator("#main header").first.click()
        await page.wait_for_function(
            r"""() => /Dados do contato|Dados da empresa/i.test(document.body.innerText||'')""",
            timeout=6000)
        await page.wait_for_timeout(1200)
        h = await page.evaluate_handle(_JS_DRAWER_CONTATO)
        el = h.as_element()
        if el:
            jpeg = await el.screenshot(type="jpeg", quality=85)
            texto = await el.inner_text()
            m = _RE_TEL_BR.search(texto or "")
            if m:
                numero = normalizar_numero(m.group(0))
        await page.keyboard.press("Escape")  # fecha o painel
    return {"jpeg": bytes(jpeg) if jpeg else None, "numero": numero}


async def _capturar_comprovante(page, comprovante_dir: str, paj_ref: str = "",
                                baseline_h=None) -> dict:
    """Gera o comprovante PDF: página 1 = Dados do contato (foto+nome+número do
    destinatário — SÓ o painel, sem barra lateral); páginas seguintes = a CONVERSA em
    fatias (todo o bloco enviado, com CSS zoom p/ caber mais por página — ver
    WA_COMPROVANTE_ZOOM). Nome: Envio_Whats_Painel_[PAJ_]AAAA-MM-DD_HH-MM-SS.pdf.
    Retorna {"pdf": caminho|None, "numero_confirmado": str} — o número é o mesmo
    extraído do painel "Dados do contato" (ver `_capturar_contato_jpeg`)."""
    destino, numero_confirmado = None, ""
    try:
        conv = await _capturar_conversa_fatias(page, baseline_h)
        contato = await _capturar_contato_jpeg(page)
        numero_confirmado = contato.get("numero", "")
        jpegs = ([contato["jpeg"]] if contato.get("jpeg") else []) + list(conv)
        if jpegs:
            ref = re.sub(r"[^\w.-]", "_", paj_ref or "").strip("_")
            ts = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            nome = f"Envio_Whats_Painel_{ref + '_' if ref else ''}{ts}.pdf"
            Path(comprovante_dir).mkdir(parents=True, exist_ok=True)
            destino = str(Path(comprovante_dir) / nome)
            _jpegs_to_pdf(jpegs, destino)
    except Exception:  # noqa: BLE001
        destino = None
    return {"pdf": destino, "numero_confirmado": numero_confirmado}


async def _capturar_resposta(page, destino_dir: str, paj_ref: str = "") -> str | None:
    """Gera o PDF da RESPOSTA do assistido (mesmo formato do comprovante de envio):
    página 1 = Dados do contato; páginas seguintes = as últimas telas da conversa
    (onde está a resposta mais recente — sem baseline de envio, então
    `_capturar_conversa_fatias` cai no modo "últimas 2 telas"). Para anexar à
    movimentação que registra a resposta no PAJ (pedido do usuário, 2026-10-28).
    Nome: Resposta_Whats_Painel_[PAJ_]AAAA-MM-DD_HH-MM-SS.pdf."""
    destino = None
    try:
        conv = await _capturar_conversa_fatias(page, None)
        contato = await _capturar_contato_jpeg(page)
        jpegs = ([contato["jpeg"]] if contato.get("jpeg") else []) + list(conv)
        if jpegs:
            ref = re.sub(r"[^\w.-]", "_", paj_ref or "").strip("_")
            ts = _dt.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            nome = f"Resposta_Whats_Painel_{ref + '_' if ref else ''}{ts}.pdf"
            Path(destino_dir).mkdir(parents=True, exist_ok=True)
            destino = str(Path(destino_dir) / nome)
            _jpegs_to_pdf(jpegs, destino)
    except Exception:  # noqa: BLE001
        destino = None
    return destino


async def capturar_pdf_resposta(numero: str, destino_dir: str, paj_ref: str = "",
                                 headless: bool = True, numeros_registrados: list[str] | None = None) -> str | None:
    """Encontra, entre TODAS as conversas relacionadas ao PAJ (o(s) número(s)
    cadastrado(s) MAIS o(s) descoberto(s) pela busca "PAJ <numero>" — ver
    `_ler_conversas_do_paj`), a que tem resposta do assistido, e gera o PDF dela (ver
    `_capturar_resposta`) — usado por `confirmar_recebimento` para anexar a resposta
    à movimentação que a registra no PAJ. Operação SOMENTE LEITURA (não digita nem
    envia nada). Retorna o caminho do PDF, ou None se não conseguir — nunca levanta;
    a movimentação segue normalmente sem o anexo."""
    candidatos = [numero] + list(numeros_registrados or [])
    async with async_playwright() as p:
        ctx = await abrir_contexto(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            leitura = await _ler_conversas_do_paj(page, candidatos, paj_ref, 60)
            if not leitura.get("ok"):
                return None
            dumps = leitura["dumps"]
            numeros_verificados = leitura["numeros_verificados"]
            com_resposta = [n for n in numeros_verificados if _processar_dump_resposta(dumps[n])["tem_resposta"]]
            alvo = com_resposta[0] if com_resposta else numeros_verificados[0]
            # Reabre a conversa-alvo (a última aberta pelo loop acima pode ser outra) —
            # _capturar_resposta precisa da página posicionada nela para o print.
            if not await _abrir_numero_direto(page, alvo):
                return None
            await page.wait_for_timeout(800)
            return await _capturar_resposta(page, destino_dir, paj_ref)
        except Exception:  # noqa: BLE001
            return None
        finally:
            with contextlib.suppress(Exception):
                await ctx.close()


async def enviar_mensagem(
    numero: str,
    mensagem: str,
    anexos: list[dict] | None = None,
    headless: bool = True,
    comprovante_dir: str | None = None,
    paj_ref: str = "",
    mensagem_prazo: str | None = None,
    mensagem_referencia: str | None = None,
) -> dict:
    """Envia uma mensagem de WhatsApp pelo perfil dedicado, em SEQUÊNCIA:
      1) REFERÊNCIA do PAJ (`mensagem_referencia`, ex.: "Contato referente ao PAJ ...");
      2) TEXTO (mensagem própria, pode ser multilinha); 3) PRAZO (`mensagem_prazo`);
      4) ANEXOS (lote único via "Documento", sem legenda). Todas as mensagens são
      DIGITADAS no chat (multilinha via Shift+Enter). Modelo: 1 clique no painel → envia tudo.
    Se `comprovante_dir` for dado, gera o comprovante PDF (dados do contato + conversa)
    nomeado com o PAJ (`paj_ref`) e o retorna em `comprovante`.
    Retorna {ok, status, detalhe, comprovante}. status: enviado|parcial|falha.
    """
    import mimetypes
    from urllib.parse import quote

    from services.texto_util import remover_emojis

    anexos = anexos or []
    intl = normalizar_numero(numero)
    if not intl:
        return {"ok": False, "status": "falha", "detalhe": "Número vazio/inválido."}
    # Só TEXTO nas mensagens — remove emojis (garantia; WhatsApp renderiza emoji como
    # imagem e isso também quebrava a confirmação de envio).
    texto = remover_emojis((mensagem or "").strip())
    msg_prazo = remover_emojis((mensagem_prazo or "").strip())
    msg_ref = remover_emojis((mensagem_referencia or "").strip())
    validos = [a for a in anexos if a.get("caminho") and Path(a["caminho"]).exists()]
    ausentes = [a for a in anexos if not (a.get("caminho") and Path(a["caminho"]).exists())]
    if not texto and not validos and not msg_prazo and not msg_ref:
        return {"ok": False, "status": "falha",
                "detalhe": "Nada a enviar (sem texto e sem anexo válido)."}

    async with async_playwright() as p:
        ctx = await abrir_contexto(p, headless=headless)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        try:
            # Abre o chat SEM pré-preencher texto — todas as mensagens são digitadas na
            # ordem desejada (referência → texto → prazo), o que permite a referência 1ª.
            url = f"{WA_URL}/send?phone={intl}"
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

            # aguarda: chat abrir (compose) OU diálogo de número inválido
            aberto = False
            for _ in range(30):
                await page.wait_for_timeout(1000)
                if await _numero_invalido(page):
                    return {"ok": False, "status": "falha",
                            "detalhe": f"Número {intl} não está no WhatsApp."}
                if await page.locator(_SEL_COMPOSE).count():
                    aberto = True
                    break
            if not aberto:
                return {"ok": False, "status": "falha", "detalhe": "Conversa não abriu (timeout)."}

            # Baseline: altura da conversa ANTES de enviar — o comprovante usa isso para
            # capturar EXATAMENTE o bloco enviado (mensagem + prazo + anexos), do começo
            # ao fim, e não só a parte final da tela.
            base_conv_h = 0
            with contextlib.suppress(Exception):
                base_conv_h = await page.evaluate(_JS_CONV_SCROLLHEIGHT)

            etapas: list[tuple[str, bool]] = []

            # ORDEM: 1) referência do PAJ → 2) mensagem → 3) prazo → 4) anexos.
            # 1) REFERÊNCIA do PAJ (mensagem padrão) — enviada primeiro
            if msg_ref:
                etapas.append(("referência", await _digitar_enviar(page, msg_ref, timeout_seg=20)))

            # 2) MENSAGEM (texto próprio, pode ser multilinha)
            if texto:
                etapas.append(("texto", await _digitar_enviar(page, texto, timeout_seg=25)))

            # 3) MENSAGEM DE PRAZO (condicional)
            if msg_prazo:
                etapas.append(("prazo", await _digitar_enviar(page, msg_prazo, timeout_seg=20)))

            # 4) ANEXOS (por último; lote único via "Documento", sem legenda)
            if validos:
                payloads = []
                for a in validos:
                    nome = a.get("nome") or Path(a["caminho"]).name
                    data = Path(a["caminho"]).read_bytes()
                    mime = mimetypes.guess_type(nome)[0] or "application/octet-stream"
                    payloads.append({"name": nome, "mimeType": mime, "buffer": data})
                await page.locator(_SEL_ATTACH).first.click()
                await page.wait_for_timeout(700)
                async with page.expect_file_chooser() as fc:
                    await page.get_by_text("Documento", exact=True).click()
                chooser = await fc.value
                await chooser.set_files(payloads)
                await page.wait_for_timeout(2500)
                await page.locator(_SEL_SEND_PREVIEW).first.click()
                # confirma pelo NOME do 1º arquivo (aparece na bolha do documento)
                ok_anx = await _confirmar_enviado(page, payloads[0]["name"], timeout_seg=30)
                etapas.append((f"{len(payloads)} anexo(s)", ok_anx))

            todos_ok = bool(etapas) and all(ok for _, ok in etapas)
            algum_ok = any(ok for _, ok in etapas)
            comprovante = None
            numero_confirmado = intl  # fallback: nosso número normalizado, se não der p/ extrair do painel
            if comprovante_dir and algum_ok:
                await page.wait_for_timeout(800)
                cap = await _capturar_comprovante(page, comprovante_dir, paj_ref, baseline_h=base_conv_h)
                comprovante = cap.get("pdf")
                numero_confirmado = cap.get("numero_confirmado") or intl
            desc = ", ".join(f"{n}{'' if ok else ' (não confirmado)'}" for n, ok in etapas)
            nota_aus = f" · {len(ausentes)} anexo(s) ausente(s)" if ausentes else ""
            return {"ok": todos_ok,
                    "status": "enviado" if todos_ok else ("parcial" if algum_ok else "falha"),
                    "detalhe": f"WhatsApp p/ {intl}: {desc}{nota_aus}",
                    "comprovante": comprovante,
                    "numero_confirmado": numero_confirmado}
        finally:
            await ctx.close()


# ------------------------------------------------------------------
# TODO próximas etapas:
#   - modo ASSISTIDO em produção (headed, na máquina do servidor) integrado ao painel.
#   - 2ª automação SISDPU: registrar movimentação + anexar o print (tela a MAPEAR).
# ------------------------------------------------------------------


async def diagnostico() -> None:
    """Testa, em HEADED, qual motor consegue acessar a rede nesta máquina:
    Chromium embutido x Chrome instalado x Edge instalado (com/sem flags de sandbox).
    Rodar NA MÁQUINA DO SERVIDOR."""
    url = "https://example.com"
    flags = ["--no-sandbox", "--disable-features=NetworkServiceSandbox"]
    combos = [
        ("chromium embutido | sem flags", {}, []),
        ("chromium embutido | com flags", {}, flags),
        ("channel=chrome | sem flags", {"channel": "chrome"}, []),
        ("channel=chrome | com flags", {"channel": "chrome"}, flags),
        ("channel=msedge | sem flags", {"channel": "msedge"}, []),
        ("channel=msedge | com flags", {"channel": "msedge"}, flags),
    ]
    async with async_playwright() as p:
        for nome, extra, args in combos:
            try:
                br = await p.chromium.launch(headless=False, args=args, **extra)
                page = await br.new_page()
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                    print(f"OK    | {nome}", flush=True)
                finally:
                    await br.close()
            except Exception as e:
                print(f"FALHA | {nome} -> {str(e).splitlines()[0][:90]}", flush=True)


def _main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "login"
    if cmd == "login":
        ok = asyncio.run(login_interativo())
        sys.exit(0 if ok else 1)
    if cmd == "diag":
        asyncio.run(diagnostico())
        sys.exit(0)
    print(f"comando desconhecido: {cmd} (use: login | diag)", flush=True)
    sys.exit(2)


if __name__ == "__main__":
    _main()
