// Drive actions live in the conversation; mutations require a visible review step.
function serviceIcon(kind) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '20'); svg.setAttribute('height', '20');
  svg.setAttribute('role', 'img'); svg.setAttribute('aria-label', kind === 'gmail' ? 'Gmail' : 'Google Drive');
  const paths = kind === 'gmail' ? [
    ['M2 5v14h4V9.1L12 13l6-3.9V19h4V5l-10 6.4Z', '#EA4335'],
    ['M2 5v14h4V9.1L2 6.5Z', '#4285F4'],
    ['M18 9.1V19h4V5l-4 2.6Z', '#34A853']
  ] : [
    ['M8.2 2h5.1l8.2 14.2h-5.1Z', '#FFBA00'],
    ['M8.2 2h5.1L5.1 16.2H0Z', '#00875A'],
    ['M5.1 16.2h16.4L18.9 21H2.5Z', '#4285F4']
  ];
  for (const [d, fill] of paths) { const path = document.createElementNS('http://www.w3.org/2000/svg', 'path'); path.setAttribute('d', d); path.setAttribute('fill', fill); svg.append(path); }
  return svg;
}

const drivePromptHost = document.querySelector('#intro .prompts');
if (drivePromptHost) {
  const drivePrompt = make('button', '', '△   Buscar en Drive');
  drivePrompt.dataset.prompt = 'Busca mis informes en Google Drive';
  drivePrompt.onclick = () => { workspaceInput.value = drivePrompt.dataset.prompt; workspaceInput.focus(); };
  drivePromptHost.append(drivePrompt);
}

async function showDriveAction(card, data) {
  const drive = data.drive || {}, plan = data.plan || {};
  const box = make('section', 'review-card drive-chat-card');
  box.append(make('h3', '', 'Google Drive'));
  card.append(box);
  if (!drive.connected) {
    box.append(make('p', 'email-hint', 'Conecta Drive con la misma cuenta con la que has entrado en Oribay. Después vuelve a pedir esta acción.'));
    const link = make('a', 'library-action primary-action', 'Conectar Google Drive');
    link.href = '/auth/google/bigquery/start'; box.append(link); return;
  }
  const notice = message => box.append(make('p', 'notice', message));
  async function call(url, options = {}) {
    const response = await fetch(url, options);
    const body = await response.json();
    if (!response.ok) throw Error(body.error || 'No se pudo completar la acción en Drive.');
    return body;
  }
  const json = value => ({method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({...value,confirmed:true})});
  function field(label, value = '') { const wrapper = make('label', 'email-field', label), input = make('input'); input.value = value; wrapper.append(input); box.append(wrapper); return input; }
  const action = drive.action || plan.drive_action || 'search';
  if (action === 'create_folder') {
    const name = field('Nombre de la carpeta', plan.drive_file || plan.title || '');
    const button = make('button', 'library-action primary-action', 'Crear carpeta');
    button.onclick = async () => {
      if (!name.value.trim() || !confirm(`¿Crear la carpeta «${name.value.trim()}» en tu Drive?`)) return;
      try { const result = await call('/api/drive/folders', json({name:name.value.trim()})); notice(`Carpeta creada: ${result.name}`); button.disabled = true; }
      catch (error) { notice(error.message); }
    };
    box.append(button); return;
  }
  if (action === 'save_pdf') {
    if (!drive.documents?.length) { notice('Aún no hay informes PDF guardados. Genera y descarga uno desde el chat antes de pedir que se guarde en Drive.'); return; }
    const selector = make('select');
    for (const item of drive.documents) { const option = make('option', '', item.title); option.value = item.id; selector.append(option); }
    const label = make('label', 'email-field', 'Informe PDF'); label.append(selector); box.append(label);
    const filename = field('Nombre en Drive', 'informe-oribay.pdf');
    const button = make('button', 'library-action primary-action', 'Guardar en Drive');
    button.onclick = async () => {
      const selected = drive.documents.find(item => item.id === selector.value);
      if (!selected || !confirm(`¿Guardar «${selected.title}» como «${filename.value.trim()}» en tu Drive?`)) return;
      try { const result = await call('/api/library/' + encodeURIComponent(selected.id) + '/drive', json({name:filename.value.trim()}));
        const link = make('a', 'library-action', 'Abrir PDF en Drive'); link.href = result.webViewLink; link.target = '_blank'; link.rel = 'noopener noreferrer'; box.append(link); button.disabled = true;
      } catch (error) { notice(error.message); }
    };
    box.append(button); return;
  }
  const query = field('Buscar en Drive', plan.drive_query || plan.drive_file || '');
  const results = make('div', 'drive-chat-results'); box.append(results);
  async function render(files) {
    results.replaceChildren();
    if (!files.length) { results.append(make('p', 'notice', 'No se encontraron archivos. Prueba con otro nombre.')); return; }
    for (const file of files) {
      const row = make('div', 'drive-chat-row');
      row.append(make('strong', '', file.name || 'Sin nombre'));
      const controls = make('div', 'actions'); row.append(controls);
      if (file.webViewLink) { const link = make('a', 'library-action', 'Abrir en Drive'); link.href = file.webViewLink; link.target = '_blank'; link.rel = 'noopener noreferrer'; controls.append(link); }
      if (file.mimeType !== 'application/vnd.google-apps.folder') {
        const read = make('button', 'library-action', 'Leer'); controls.append(read);
        read.onclick = async () => { try { const content = await call('/api/drive/files/' + encodeURIComponent(file.id) + '/content');
          const preview = make('pre', 'drive-chat-preview', content.text); row.append(preview);
          const use = make('button', 'library-action primary-action', 'Usar en el chat'); row.append(use);
          use.onclick = () => { workspaceInput.value = `Trabaja con este contenido de Drive (${file.name}):\n\n${content.text.slice(0, 1600)}`; workspaceInput.focus(); };
          read.disabled = true;
        } catch (error) { notice(error.message); } };
      }
      const rename = make('button', 'library-action', 'Renombrar'); controls.append(rename);
      rename.onclick = async () => { const name = prompt('Nuevo nombre', file.name);
        if (!name?.trim() || name.trim() === file.name || !confirm(`¿Renombrar «${file.name}» como «${name.trim()}»?`)) return;
        try { await call('/api/drive/files/' + encodeURIComponent(file.id), {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.trim(),confirmed:true})}); file.name = name.trim(); row.querySelector('strong').textContent = file.name; }
        catch (error) { notice(error.message); } };
      const move = make('button', 'library-action', 'Mover'); controls.append(move);
      move.onclick = async () => { try { const all = await call('/api/drive/files');
        const folders = (all.files || []).filter(item => item.mimeType === 'application/vnd.google-apps.folder' && item.id !== file.id);
        if (!folders.length) { notice('Crea primero una carpeta de destino.'); return; }
        const selected = prompt('Elige la carpeta por número:\n' + folders.map((item, i) => `${i + 1}. ${item.name}`).join('\n'));
        const destination = folders[Number(selected) - 1]; if (!selected || !destination || !confirm(`¿Mover «${file.name}» a «${destination.name}»?`)) return;
        await call('/api/drive/files/' + encodeURIComponent(file.id), {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({destination:destination.id,confirmed:true})}); row.remove(); notice('Archivo movido a «' + destination.name + '».');
      } catch (error) { notice(error.message); } };
      results.append(row);
    }
  }
  const search = make('button', 'library-action primary-action', 'Buscar archivos'); box.append(search);
  search.onclick = async () => { try { const params = new URLSearchParams({q:query.value.trim()}); const found = await call('/api/drive/files?' + params); render(found.files || []); }
    catch (error) { notice(error.message); } };
  await render(drive.files || []);
}
