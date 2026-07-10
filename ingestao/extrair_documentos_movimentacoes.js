/**
 * EXTRAÇÃO DE DOCUMENTOS DE MOVIMENTAÇÕES DE UM PAJ (SISDPU)
 *
 * Cópia adaptada de C:\Claude\Scripts\SISDPU\extrair_documentos_movimentacoes.js
 * para uso automatizado via Playwright (sisdpu_client.extrair_documentos_por_seq).
 * NÃO editar o script original — esta cópia é a única usada pelo painel.
 *
 * Diferença em relação ao original: `window.__extracaoInit` aceita uma lista
 * de seqs já conhecidas (que já têm `documentos` no metadata da sincronização
 * anterior) e as exclui da fila — só processa seqs novas, mantendo o custo de
 * cada sincronização proporcional ao que mudou desde a última.
 *
 * MODO DE USO (via page.evaluate, uma seq por vez — prioriza precisão sobre
 * velocidade; não avança para a próxima seq até confirmar que os dados da
 * anterior foram obtidos e o popup foi fechado):
 *
 *   1) Avaliar o conteúdo deste arquivo uma vez (define as 3 funções abaixo
 *      em `window`, não executa nada ainda).
 *   2) Chamar `window.__extracaoInit(seqsConhecidos)` — monta a fila de seqs
 *      com anexo (excluindo as já conhecidas) e retorna o total.
 *   3) Chamar `window.__extracaoProximo()` repetidas vezes (uma por seq da
 *      fila). Cada chamada processa a próxima seq, confirma a extração e só
 *      então fecha o popup — retorna o resultado daquela seq + progresso.
 *   4) Quando `window.__extracaoProximo()` retornar `{fim: true}`, chamar
 *      `window.__extracaoFinalizar()` para obter o objeto consolidado.
 *
 * Não usa índices fixos de tabela nem números de seq fixos — localiza tudo
 * dinamicamente, então funciona em qualquer PAJ sem precisar redescobrir nada.
 */

// ═══════════════════════════════ INIT ═══════════════════════════════
window.__extracaoInit = function (seqsConhecidos) {
  const conhecidos = new Set((seqsConhecidos || []).map(String));

  const pageText = document.body.innerText;
  window.__extracaoEstado = {
    paj: (pageText.match(/Número\s+do\s+PAJ\s+(\d{4}\/\d{3}-\d+)/) || [])[1] || 'Desconhecido',
    assistido: (pageText.match(/Assistido\(s\)\s+([A-Z\sÀ-ÿ]+?)(?:\n|Data|Decurso|CPF)/) || [])[1]?.trim() || 'Desconhecido',
    movimentacoes_com_documentos: [],
    total_documentos: 0,
    indice: 0,
    seqsPuladas: []
  };

  const tables = document.querySelectorAll('table');
  let tableMovimentacoes = null;
  for (const t of tables) {
    const headers = Array.from(t.querySelectorAll('thead th, tbody tr:first-child td'))
      .map(h => h.textContent.trim()).filter(Boolean);
    if (headers.includes('Seq.') && headers.includes('Data/Hora')) { tableMovimentacoes = t; break; }
  }
  if (!tableMovimentacoes) {
    window.__extracaoEstado.erro = 'Tabela de movimentações não encontrada';
    return window.__extracaoEstado;
  }

  const rowsMovimentacoes = tableMovimentacoes.querySelectorAll('tbody tr');
  const fila = [];
  for (let i = 0; i < rowsMovimentacoes.length; i++) {
    const cells = rowsMovimentacoes[i].querySelectorAll('td');
    if (cells.length < 3) continue;
    const seq = cells[0]?.textContent.trim();
    const dataHora = cells[2]?.textContent.trim();
    const movimentacao = cells[3]?.textContent.trim();
    const temClicavel = !!cells[1].querySelector('a, button, [role="button"], [onclick], img[onclick], span[onclick]');
    if (!temClicavel || !seq || !/^\d+$/.test(seq)) continue;
    if (conhecidos.has(seq)) {
      window.__extracaoEstado.seqsPuladas.push(seq);
      continue;
    }
    fila.push({ seq, dataHora, movimentacao, rowIndex: i });
  }

  window.__extracaoFila = fila;
  return {
    paj: window.__extracaoEstado.paj,
    assistido: window.__extracaoEstado.assistido,
    totalFila: fila.length,
    seqs: fila.map(f => f.seq),
    puladas: window.__extracaoEstado.seqsPuladas.length
  };
};

// ═══════════════════════════ PROCESSAR PRÓXIMA SEQ ═══════════════════════════
window.__extracaoProximo = async function () {
  const estado = window.__extracaoEstado;
  const fila = window.__extracaoFila;

  if (estado.indice >= fila.length) return { fim: true };

  const mov = fila[estado.indice];
  estado.indice++;

  // Reobter a linha da tabela de movimentações a cada chamada (não reaproveitar
  // referência antiga — evita erro se o DOM for atualizado entre chamadas)
  const tables = document.querySelectorAll('table');
  let tableMovimentacoes = null;
  for (const t of tables) {
    const headers = Array.from(t.querySelectorAll('thead th, tbody tr:first-child td'))
      .map(h => h.textContent.trim()).filter(Boolean);
    if (headers.includes('Seq.') && headers.includes('Data/Hora')) { tableMovimentacoes = t; break; }
  }
  const row = tableMovimentacoes.querySelectorAll('tbody tr')[mov.rowIndex];
  const anexoCell = row.querySelectorAll('td')[1];
  const clickable = anexoCell.querySelector('a, button, [role="button"], [onclick], img[onclick], span[onclick]');

  if (!clickable) {
    return { seq: mov.seq, progresso: `${estado.indice}/${fila.length}`, erro: 'elemento clicável não encontrado', documentos: [] };
  }

  // O SISDPU REAPROVEITA o mesmo dialogo/tabela ("detalhamentoForm:arquivoFase")
  // entre cliques em seqs diferentes — so o CONTEUDO e trocado via AJAX. Se
  // lermos a tabela cedo demais, pegamos o conteudo do seq ANTERIOR ainda nao
  // substituido (documentos ficam "misturados" entre seqs vizinhos, mesmo com
  // o total geral batendo). Por isso capturamos uma assinatura do conteudo
  // ANTES do clique e so aceitamos a tabela depois do clique quando essa
  // assinatura mudar — confirma que a atualizacao do seq atual ja chegou.
  const acharTabelaDoc = () => {
    const allTables = document.querySelectorAll('table');
    for (let j = allTables.length - 1; j >= Math.max(0, allTables.length - 40); j--) {
      const headers = Array.from(allTables[j].querySelectorAll('thead th, tbody tr:first-child td'))
        .map(h => h.textContent.trim()).filter(Boolean);
      if (headers.includes('Arquivo') && allTables[j].offsetParent !== null) return allTables[j];
    }
    return null;
  };
  const assinatura = (t) => t?.querySelector('tbody')?.innerHTML || '';

  const tabelaAntesDoClick = acharTabelaDoc();
  const assinaturaAntes = assinatura(tabelaAntesDoClick);

  clickable.click();

  // Poll (em vez de sleep fixo) até a tabela de documentos VISÍVEL aparecer
  // COM CONTEUDO DIFERENTE do que havia antes do clique, com timeout generoso
  // — prioriza confirmar que carregou de fato o seq atual, nao um resíduo.
  let tableDoc = null;
  const inicio = Date.now();
  while (Date.now() - inicio < 8000) {
    await new Promise(r => setTimeout(r, 300));
    const candidato = acharTabelaDoc();
    if (candidato && assinatura(candidato) !== assinaturaAntes) { tableDoc = candidato; break; }
  }

  if (!tableDoc) {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await new Promise(r => setTimeout(r, 500));
    return { seq: mov.seq, progresso: `${estado.indice}/${fila.length}`, erro: 'tabela de documentos não encontrada (timeout)', documentos: [] };
  }

  // Confirmar estabilidade: reler a tabela após uma pequena pausa e comparar
  // a contagem de linhas — evita capturar um estado intermediário do popup
  // ainda carregando via AJAX
  const contarLinhasValidas = () => tableDoc.querySelectorAll('tbody tr').length;
  let contagemAnterior = contarLinhasValidas();
  await new Promise(r => setTimeout(r, 500));
  let contagemAtual = contarLinhasValidas();
  let tentativasEstabilidade = 0;
  while (contagemAtual !== contagemAnterior && tentativasEstabilidade < 6) {
    contagemAnterior = contagemAtual;
    await new Promise(r => setTimeout(r, 500));
    contagemAtual = contarLinhasValidas();
    tentativasEstabilidade++;
  }

  // Mapear colunas pelo cabeçalho REAL da tabela (thead th), não por
  // heurística de conteúdo — "Documentos do Assistido" é um valor de TIPO
  // que contém a palavra "documento", o que quebra detecção por texto.
  const theadCells = Array.from(tableDoc.querySelectorAll('thead th')).map(h => h.textContent.trim());
  const idxTipo = theadCells.findIndex(h => h.toLowerCase().includes('tipo'));
  const idxDescricao = theadCells.findIndex(h => h.toLowerCase().includes('descri'));
  const idxArquivo = theadCells.findIndex(h => h.toLowerCase() === 'arquivo');
  const useHeader = idxTipo !== -1 && idxDescricao !== -1 && idxArquivo !== -1;

  const documentos = [];
  const docRows = tableDoc.querySelectorAll('tbody tr');
  for (const docRow of docRows) {
    const docCells = docRow.querySelectorAll('td');
    if (docCells.length < 3) continue;

    let tipoArquivo, descricao, nomeArquivo;
    if (useHeader) {
      tipoArquivo = docCells[idxTipo]?.textContent.trim() || '';
      descricao = docCells[idxDescricao]?.textContent.trim() || '';
      nomeArquivo = docCells[idxArquivo]?.textContent.trim() || '';
    } else {
      tipoArquivo = docCells[0]?.textContent.trim() || '';
      descricao = docCells[1]?.textContent.trim() || '';
      nomeArquivo = docCells[2]?.textContent.trim() || '';
    }
    const urlDownload = docRow.querySelector('a')?.href || '';

    if (nomeArquivo && nomeArquivo !== 'Nenhum arquivo encontrado.') {
      documentos.push({ tipoArquivo, descricao, nomeArquivo, urlDownload });
    }
  }

  if (documentos.length > 0) {
    estado.movimentacoes_com_documentos.push({ seq: mov.seq, dataHora: mov.dataHora, movimentacao: mov.movimentacao, documentos });
    estado.total_documentos += documentos.length;
  }

  // Fechar e CONFIRMAR que fechou antes de retornar (só então a próxima
  // chamada pode avançar com segurança). ESC nem sempre é suficiente —
  // usar também o botão de fechar do PRÓPRIO diálogo como reforço (restrito
  // ao diálogo que contém tableDoc — nunca buscar "fechar/close" na página
  // inteira, que poderia clicar em alguma ação não relacionada ao popup).
  const tentarFechar = () => {
    document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    const dialog = tableDoc.closest('.ui-dialog') ||
      Array.from(document.querySelectorAll('.ui-dialog')).find(d =>
        d.getAttribute('aria-hidden') !== 'true' && !d.classList.contains('ui-overlay-hidden'));
    const botaoFechar = dialog?.querySelector('.ui-dialog-titlebar-close');
    if (botaoFechar) botaoFechar.click();
  };

  tentarFechar();
  const inicioFechamento = Date.now();
  while (tableDoc.offsetParent !== null && Date.now() - inicioFechamento < 10000) {
    await new Promise(r => setTimeout(r, 400));
    if ((Date.now() - inicioFechamento) % 2000 < 400) tentarFechar(); // reforçar a cada ~2s
  }

  return {
    seq: mov.seq,
    dataHora: mov.dataHora,
    movimentacao: mov.movimentacao,
    progresso: `${estado.indice}/${fila.length}`,
    popupFechado: tableDoc.offsetParent === null,
    releituraEstavel: tentativasEstabilidade < 6,
    documentos
  };
};

// ═══════════════════════════════ FINALIZAR ═══════════════════════════════
window.__extracaoFinalizar = function () {
  const resultado = window.__extracaoEstado;

  let md = `PAJ: ${resultado.paj} | Assistido: ${resultado.assistido}\n`;
  for (const mov of resultado.movimentacoes_com_documentos) {
    md += `\n## SEQ ${mov.seq} — ${mov.dataHora}\n\n`;
    md += `| Descrição | Tipo | Arquivo | Link |\n|---|---|---|---|\n`;
    for (const d of mov.documentos) md += `| ${d.descricao} | ${d.tipoArquivo} | ${d.nomeArquivo} | ${d.urlDownload} |\n`;
  }
  md += `\nTotal: ${resultado.total_documentos} documentos em ${resultado.movimentacoes_com_documentos.length} movimentações com anexo (${resultado.seqsPuladas.length} seq(s) já conhecida(s) pulada(s)).`;
  resultado.tabela_markdown = md;

  return resultado;
};
