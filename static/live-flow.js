// Present each backend stage when that stage actually starts or completes.
const salesHistory = [];
workspaceForm.onsubmit = async event => {
  event.preventDefault();
  const question = workspaceInput.value.trim();
  if (!question) return;
  document.querySelector('#intro')?.remove();
  workspaceChat.append(make('div', 'msg user', question));
  workspaceInput.value = '';
  workspaceSend.disabled = true;
  const card = make('article', 'msg bot');
  const run = make('div', 'tool-run');
  card.append(make('div', 'eyebrow', 'ORIBAY · ASISTENTE'), run);
  workspaceChat.append(card);
  const router = stage(run, '✦', 'Entendiendo la petición', 'Eligiendo la herramienta adecuada', 'running');
  let active = router;
  down();
  try {
    const routing = await sendJson('/api/route', {question, history:salesHistory});
    const plan = routing.plan;
    setStage(router, 'done', `Ruta elegida: ${plan.route} · ${plan.router_mode}`);
    if (plan.context_used && plan.context_reference) {
      card.append(make('div', 'route-pill', '↳ Contexto: ' + plan.context_reference));
    }
    active = plan.route === 'alerta'
      ? stage(run, '◷', 'Preparando la regla', 'Extrayendo métrica, umbral y frecuencia', 'running')
      : stage(run, '▥', 'Consultando Ventas Oribay', 'Esperando datos y gráficos del agente', 'running');
    down();
    const data = await sendJson('/api/execute', {question, plan});
    if (data.route === 'ventas' && data.answer) {
      salesHistory.push({route:'ventas', question, answer:data.answer});
      if (salesHistory.length > 5) salesHistory.shift();
    }
    setStage(active, 'done', plan.route === 'alerta' ? 'Regla lista para revisión' : 'Respuesta analítica recibida');
    if (data.route === 'pdf') stage(run, '▤', 'Preparando documento', 'Descarga disponible', 'done');
    if (data.route === 'correo') stage(run, '✉', 'Redactando correo', 'Borrador listo para revisión', 'done');
    if (data.answer) card.append(make('div', 'answer', data.answer));
    if (data.route === 'alerta') showAlert(card, data.plan);
    for (const spec of data.charts || []) {
      const chart = make('div', 'chart'); if (spec.title) chart.setAttribute('aria-label', spec.title); card.append(chart);
      try { await vegaEmbed(chart, spec, {actions:false, renderer:'svg'}); }
      catch (error) { chart.textContent = 'No se pudo dibujar el gráfico: ' + error.message; }
    }
    for (const table of data.tables || []) card.append(make('pre', 'table', table));
    if (data.route === 'pdf') showPdf(card, data);
    if (data.route === 'correo') showEmail(card, data.email || {});
    if (data.sql?.length) {
      const details = make('details');
      details.append(make('summary', '', 'Ver consultas SQL'), make('pre', '', data.sql.join('\n\n')));
      card.append(details);
    }
  } catch (error) {
    setStage(active, 'error', 'No se pudo completar este paso');
    card.append(make('p', 'notice', error.message));
  } finally { workspaceSend.disabled = false; down(); }
};
