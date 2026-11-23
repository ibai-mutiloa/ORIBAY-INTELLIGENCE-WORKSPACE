// Persistent, private chat history for the Oribay workspace.
(() => {
  const list = document.querySelector('#conversation-list');
  const newChatButton = document.querySelector('#new-chat');
  const refreshButton = document.querySelector('#history-refresh');
  const titleElement = document.querySelector('#chat-title');
  const introTemplate = document.querySelector('#intro')?.cloneNode(true);
  const backdrop = document.querySelector('#sidebar-backdrop');
  let activeId = null;
  let activeTitle = '';
  let salesHistory = [];

  function formattedAnswer(value) {
    const root = make('div', 'answer');
    for (const block of String(value || '').trim().split(/\n\s*\n/)) {
      if (!block.trim()) continue;
      const paragraph = make('p', 'answer-paragraph');
      const cleaned = block.replace(/^#{1,4}\s+/gm, '');
      const pieces = cleaned.split(/(\*\*[^*]+\*\*)/g);
      for (const piece of pieces) {
        if (piece.startsWith('**') && piece.endsWith('**')) paragraph.append(make('strong', '', piece.slice(2, -2)));
        else paragraph.append(document.createTextNode(piece));
      }
      root.append(paragraph);
    }
    return root;
  }

  async function api(url, options = {}) {
    const response = await fetch(url, {
      ...options,
      headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || 'No se pudo completar la acción.');
    return body;
  }

  function closeSidebar() {
    document.querySelector('.layout').classList.remove('sidebar-open');
  }

  function showWelcome() {
    const intro = introTemplate?.cloneNode(true);
    if (!intro) return;
    intro.querySelectorAll('[data-prompt]').forEach(button => {
      button.addEventListener('click', () => {
        workspaceInput.value = button.dataset.prompt;
        workspaceInput.focus();
      });
    });
    workspaceChat.replaceChildren(intro);
  }

  function dateLabel(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.valueOf())) return '';
    return date.toLocaleDateString('es-ES', { day: 'numeric', month: 'short' });
  }

  function displayLabel(value) {
    const labels = {
      Mes: 'Mes', Fecha_Mes: 'Fecha', Unidades_Piezas: 'Unidades', Unidades: 'Unidades',
      Importe_Piezas_EUR: 'Facturación (€)', Facturacion_Piezas_EUR: 'Facturación (€)',
      Facturacion_EUR: 'Facturación (€)', Importe_EUR: 'Facturación (€)',
      Total_Facturado_EUR: 'Total facturado (€)', Tipo_Cargo: 'Concepto', Comercial: 'Comercial',
      Cliente: 'Cliente', Pais: 'País', Familia: 'Familia', Producto: 'Producto', Num_Mes: 'Mes'
    };
    const clean = String(value || '').replace(/\\_/g, '_').replace(/`/g, '').trim();
    return labels[clean] || clean.replace(/_/g, ' ');
  }

  function appendTable(host, raw) {
    const lines = String(raw || '').split(/\r?\n/).map(line => line.trim()).filter(line => line.includes('|'));
    const rows = lines.map(line => line.replace(/^\|/, '').replace(/\|$/, '').split('|').map(cell => cell.trim().replace(/\\_/g, '_').replace(/`/g, '')))
      .filter(row => row.some(Boolean) && !row.every(cell => /^:?-{2,}:?$/.test(cell.replace(/\s/g, ''))));
    if (rows.length < 2) { host.append(make('pre', 'table-raw', String(raw))); return; }

    const headers = rows[0];
    const normalized = headers.map(header => header.replace(/\s/g, '').toLowerCase());
    const amountNames = ['total_facturado_eur', 'facturacion_eur', 'importe_eur', 'facturacion_piezas_eur', 'importe_piezas_eur'];
    const amountIndex = amountNames.map(name => normalized.indexOf(name)).find(index => index >= 0);
    const unitIndex = normalized.findIndex(name => ['unidades_piezas', 'unidades'].includes(name));
    const monthIndex = normalized.findIndex(name => ['mes', 'fecha_mes', 'periodo'].includes(name));
    let indexes = headers.map((_, index) => index).filter(index => !/portes|freight|transport|^(mesnumero|mesnum|nummes|monthnumber|nombremes|monthname)$/i.test(normalized[index]));
    if (amountIndex !== undefined) {
      indexes = indexes.filter(index => !(/^(total_facturado_eur|facturacion_eur|importe_eur|facturacion_piezas_eur|importe_piezas_eur)$/.test(normalized[index]) && index !== amountIndex));
    }
    if (monthIndex >= 0 && unitIndex >= 0 && amountIndex !== undefined) indexes = [...new Set([monthIndex, unitIndex, amountIndex, ...indexes.filter(index => ![monthIndex, unitIndex].includes(index) && !/eur$/.test(normalized[index]))])];

    const table = make('table', 'data-table');
    const head = document.createElement('thead'), headRow = document.createElement('tr');
    indexes.forEach(index => { const cell = document.createElement('th'); cell.textContent = displayLabel(headers[index]); headRow.append(cell); });
    head.append(headRow); table.append(head);
    const body = document.createElement('tbody');
    for (const row of rows.slice(1)) {
      const tr = document.createElement('tr');
      indexes.forEach(index => {
        const td = document.createElement('td');
        let value = row[index] || '';
        if (/^-?\d+(?:\.\d+)?$/.test(value) && !/^\d{4}-\d{2}(?:-\d{2})?$/.test(value)) {
          const number = Number(value);
          value = new Intl.NumberFormat('es-ES', { maximumFractionDigits: Number.isInteger(number) ? 0 : 2 }).format(number);
          if (/eur|importe|facturacion|total/i.test(normalized[index])) value += ' €';
        }
        td.textContent = value;
        tr.append(td);
      });
      body.append(tr);
    }
    table.append(body);
    const wrap = make('div', 'data-table-wrap'); wrap.append(table); host.append(wrap);
  }

  async function renderChart(host, spec) {
    host.style.minHeight = '340px';
    const visual = spec?.mark || String(spec?.$schema || '').includes('vega-lite')
      ? { ...spec, width: Math.max(Number(spec.width) || 0, 620), height: Math.max(Number(spec.height) || 0, 300) }
      : spec;
    await vegaEmbed(host, visual, { actions: false, renderer: 'svg' });
  }

  async function refreshHistory() {
    try {
      const data = await api('/api/conversations');
      list.replaceChildren();
      if (!data.items.length) {
        list.append(make('p', 'history-empty', 'Tus conversaciones aparecerán aquí.'));
        return;
      }
      for (const item of data.items) {
        const row = make('div', 'conversation-row' + (item.id === activeId ? ' active' : ''));
        const open = make('button', 'conversation-open');
        open.type = 'button';
        open.setAttribute('aria-label', `Abrir conversación: ${item.title}`);
        const label = make('span', 'conversation-title', item.title || 'Nueva conversación');
        const meta = make('span', 'conversation-meta', item.preview || dateLabel(item.updated_at));
        open.append(label, meta);
        open.addEventListener('click', () => openConversation(item.id));
        const remove = make('button', 'conversation-delete', '×');
        remove.type = 'button';
        remove.title = 'Eliminar conversación';
        remove.setAttribute('aria-label', `Eliminar conversación: ${item.title}`);
        remove.addEventListener('click', async event => {
          event.stopPropagation();
          const accepted = confirm(`¿Eliminar «${item.title}» del historial?\n\nSe borrarán sus mensajes. Los informes, alertas y borradores que guardaste en tu espacio permanecerán en la biblioteca.`);
          if (!accepted) return;
          try {
            await api(`/api/conversations/${encodeURIComponent(item.id)}`, { method: 'DELETE' });
            if (activeId === item.id) startNewChat();
            await refreshHistory();
          } catch (error) {
            alert(error.message);
          }
        });
        row.append(open, remove);
        list.append(row);
      }
    } catch (error) {
      list.replaceChildren(make('p', 'history-empty', error.message));
    }
  }

  function startNewChat() {
    activeId = null;
    activeTitle = '';
    salesHistory = [];
    titleElement.textContent = 'Chat de ventas';
    workspaceChat.hidden = false;
    libraryView.hidden = true;
    document.querySelector('.composer').hidden = false;
    document.querySelectorAll('nav [data-view]').forEach(item => item.classList.toggle('active', item.dataset.view === 'chat'));
    showWelcome();
    refreshHistory();
    closeSidebar();
    workspaceInput.focus();
  }

  function makeAssistantCard(message) {
    const card = make('article', 'msg bot');
    card.append(make('div', 'eyebrow', 'ORIBAY · ASISTENTE'));
    const routeNames = { ventas: 'Consulta de ventas', pdf: 'Informe', correo: 'Gmail · mensaje', drive: 'Google Drive', alerta: 'Alerta', error: 'Solicitud' };
    const trace = make('div', 'tool-run');
    stage(trace, '✦', 'Conversación', routeNames[message.route] || 'Respuesta guardada', 'done');
    if (message.route === 'drive') stage(trace, serviceIcon('drive'), 'Google Drive', message.answer || 'Acción preparada', 'done');
    if (message.route === 'correo') stage(trace, serviceIcon('gmail'), 'Gmail', 'Borrador de correo', 'done');
    card.append(trace);
    if (message.answer) card.append(formattedAnswer(message.answer));
    if (message.email_invalid) card.append(make('p', 'notice', 'El borrador anterior se ha ocultado porque la consulta de ventas no obtuvo datos verificables. Vuelve a pedir el correo para generar uno nuevo.'));
    for (const spec of message.charts || []) {
      const chart = make('div', 'chart');
      if (spec.title) chart.setAttribute('aria-label', spec.title);
      card.append(chart);
      renderChart(chart, spec).catch(() => {
        chart.textContent = 'No se pudo mostrar este gráfico guardado.';
      });
    }
    for (const table of message.tables || []) appendTable(card, table);
    if (message.email_snapshot?.body) {
      const mail = message.email_snapshot;
      card.append(make('pre', 'email', `Para: ${mail.to || '(destinatario)'}\nAsunto: ${mail.subject || ''}\n\n${mail.body}`));
    }
    if (message.document_library_id) {
      const documentLink = make('a', 'library-action', 'Descargar PDF guardado');
      documentLink.href = `/api/library/${encodeURIComponent(message.document_library_id)}/download`;
      documentLink.setAttribute('download', '');
      card.append(documentLink);
    }
    if (message.route === 'alerta' && message.plan) {
      const p = message.plan;
      card.append(make('div', 'rule', `Métrica: ${p.alert_metric || 'Pendiente'}\nCondición: ${p.alert_operator || ''} ${p.alert_threshold ?? 'Pendiente'}\nPeriodo: ${p.alert_period || 'Pendiente'}\nFrecuencia: ${p.alert_frequency || 'Pendiente'}`));
    }
    if (message.library_id) {
      const reveal = make('button', 'library-action', 'Cargando análisis guardado…');
      reveal.type = 'button';
      reveal.addEventListener('click', async () => {
        reveal.disabled = true;
        try {
          const saved = await getLibrary(`/api/library/${encodeURIComponent(message.library_id)}`);
          const payload = saved.payload || {};
          for (const spec of payload.charts || []) {
            const chart = make('div', 'chart');
            if (spec.title) chart.setAttribute('aria-label', spec.title);
            card.append(chart);
            try { await renderChart(chart, spec); }
            catch { chart.textContent = 'No se pudo mostrar este gráfico.'; }
          }
          for (const table of payload.tables || []) appendTable(card, table);
          reveal.remove();
        } catch (error) {
          reveal.disabled = false;
          reveal.textContent = 'No se pudo abrir el análisis guardado';
          card.append(make('p', 'notice', error.message));
        }
      });
      if (!message.charts?.length && !message.tables?.length) {
        reveal.textContent = 'Mostrar gráficos y datos';
        card.append(reveal);
      }
    }
    if (message.email_library_id) {
      const openEmail = make('button', 'library-action', 'Abrir borrador de correo');
      openEmail.type = 'button';
      openEmail.addEventListener('click', async () => {
        try {
          const saved = await getLibrary(`/api/library/${encodeURIComponent(message.email_library_id)}`);
          await showEmail(card, { ...saved.payload, library_id: saved.id });
          openEmail.remove();
        } catch (error) { card.append(make('p', 'notice', error.message)); }
      });
      card.append(openEmail);
    }
    return card;
  }

  async function openConversation(id) {
    try {
      const data = await api(`/api/conversations/${encodeURIComponent(id)}`);
      activeId = data.id;
      activeTitle = data.title || 'Chat de ventas';
      titleElement.textContent = activeTitle;
      salesHistory = [];
      workspaceChat.replaceChildren();
      let previousQuestion = '';
      for (const message of data.messages || []) {
        if (message.kind === 'user') {
          previousQuestion = message.question || '';
          workspaceChat.append(make('div', 'msg user', previousQuestion));
        } else {
          workspaceChat.append(makeAssistantCard(message));
          if (['ventas', 'pdf', 'correo'].includes(message.route) && previousQuestion) {
            salesHistory.push({ route: 'ventas', question: previousQuestion, answer: message.answer || '' });
          }
          previousQuestion = '';
        }
      }
      workspaceChat.hidden = false;
      libraryView.hidden = true;
      document.querySelector('.composer').hidden = false;
      document.querySelectorAll('nav [data-view]').forEach(item => item.classList.toggle('active', item.dataset.view === 'chat'));
      await refreshHistory();
      closeSidebar();
      down();
    } catch (error) {
      workspaceChat.replaceChildren(make('p', 'notice', error.message));
    }
  }

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
    const router = stage(run, '✦', 'Entendiendo la petición', 'Eligiendo entre los agentes y herramientas del espacio', 'running');
    down();
    try {
      if (!activeId) {
        const created = await api('/api/conversations', { method: 'POST', body: '{}' });
        activeId = created.id;
        activeTitle = question.slice(0, 72);
        titleElement.textContent = activeTitle;
      }
      await refreshHistory();
      const data = await sendJson('/api/ask', { question, conversation_id: activeId, history: salesHistory.slice(-5) });
      run.replaceChildren();
      const stepIcons = { Router: '✦', Regla: '◷', 'Ventas Oribay': '▥', PDF: '▤', Gmail: serviceIcon('gmail'), Drive: serviceIcon('drive') };
      const completedSteps = data.steps?.length ? data.steps : [{ name: 'Router', detail: `Ruta elegida: ${data.route}`, status: 'done' }];
      completedSteps.forEach((item, index) => {
        const row = stage(run, stepIcons[item.name] || '✦', item.name, item.detail || 'Paso completado', item.status === 'error' ? 'error' : 'done');
        row.style.animationDelay = `${index * 90}ms`;
      });
      if (data.answer) card.append(formattedAnswer(data.answer));
      if (data.route === 'alerta') showAlert(card, data.plan);
      for (const spec of data.charts || []) {
        const chart = make('div', 'chart');
        if (spec.title) chart.setAttribute('aria-label', spec.title);
        card.append(chart);
        try { await renderChart(chart, spec); }
        catch (error) { chart.textContent = 'No se pudo dibujar el gráfico: ' + error.message; }
      }
      for (const table of data.tables || []) appendTable(card, table);
      if (data.route === 'pdf') showPdf(card, data);
      if (data.route === 'correo' && data.email) await showEmail(card, data.email);
      if (data.route === 'drive') await showDriveAction(card, data);
      if (data.sql?.length) {
        const details = make('details');
        details.append(make('summary', '', 'Ver consultas SQL'), make('pre', '', data.sql.join('\n\n')));
        card.append(details);
      }
      if (['ventas', 'pdf', 'correo'].includes(data.route)) salesHistory.push({ route: 'ventas', question, answer: data.answer || '' });
      await refreshHistory();
    } catch (error) {
      setStage(router, 'error', 'No se pudo completar la solicitud');
      card.append(make('p', 'notice', error.message));
      await refreshHistory();
    } finally {
      workspaceSend.disabled = false;
      down();
    }
  };

  newChatButton.addEventListener('click', startNewChat);
  refreshButton.addEventListener('click', refreshHistory);
  document.querySelector('#sidebar-toggle')?.addEventListener('click', () => document.querySelector('.layout').classList.toggle('sidebar-open'));
  backdrop?.addEventListener('click', closeSidebar);
  refreshHistory();
})();


