// Personal Drive browser inside Tu espacio. All changes require a visible confirmation.
(() => {
  const space = document.querySelector('#library-view');
  if (!space) return;
  const panel = document.createElement('section');
  panel.className = 'drive-panel';
  panel.innerHTML = '<div class="drive-heading"><div><span class="eyebrow">GOOGLE DRIVE</span><h2>Tus archivos, aquí mismo</h2><p>Busca, lee y organiza con los permisos de tu cuenta.</p></div><a class="drive-connect" href="/auth/google/bigquery/start">Conectar Drive</a></div><div class="drive-controls"><input class="drive-search" type="search" placeholder="Buscar archivos en Drive…" aria-label="Buscar en Drive"><button class="drive-find" type="button">Buscar</button><button class="drive-folder" type="button">Nueva carpeta</button></div><div class="drive-results" aria-live="polite"></div>';
  space.insertBefore(panel, document.querySelector('#space-summary'));
  const results = panel.querySelector('.drive-results');
  const connect = panel.querySelector('.drive-connect');
  let connected = false;
  let currentFolder = '';
  let nextPage = '';
  const el = (tag, text, className) => { const node = document.createElement(tag); node.textContent = text; if (className) node.className = className; return node; };
  async function request(path, options) {
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'No se pudo completar la operación.');
    return data;
  }
  function report(message) { results.prepend(el('p', message, 'notice')); }
  async function search(append = false) {
    if (!connected) { report('Conecta Drive para ver tus archivos.'); return; }
    const query = panel.querySelector('.drive-search').value.trim();
    const params = new URLSearchParams({q: query, folder: currentFolder});
    if (append && nextPage) params.set('page_token', nextPage);
    try {
      const data = await request('/api/drive/files?' + params);
      if (!append) results.replaceChildren();
      nextPage = data.nextPageToken || '';
      if (!append && currentFolder) {
        const back = el('button', '← Volver a Mi unidad', 'drive-action');
        back.onclick = () => { currentFolder = ''; search(); };
        results.append(back);
      }
      if (!data.files?.length && !append) report('No se encontraron archivos.');
      for (const file of data.files || []) {
        const row = el('div', '', 'drive-row');
        const main = el('div', '', 'drive-file');
        main.append(el('strong', file.name || 'Sin nombre'), el('small', file.mimeType === 'application/vnd.google-apps.folder' ? 'Carpeta' : file.mimeType || 'Archivo'));
        row.append(main);
        if (file.mimeType === 'application/vnd.google-apps.folder') {
          const open = el('button', 'Abrir', 'drive-action');
          open.onclick = () => { currentFolder = file.id; search(); };
          row.append(open);
        } else {
          const read = el('button', 'Leer', 'drive-action');
          read.onclick = async () => {
            try {
              const data = await request('/api/drive/files/' + encodeURIComponent(file.id) + '/content');
              const viewer = el('div', '', 'drive-preview');
              viewer.append(el('strong', data.file.name));
              const pre = el('pre', data.text); viewer.append(pre);
              const use = el('button', 'Usar en el chat', 'drive-action');
              use.onclick = () => {
                const input = document.querySelector('#question');
                input.value = `Trabaja con este contenido de Drive (${data.file.name}):\n\n${data.text.slice(0, 1600)}`;
                document.querySelector('nav [data-view="chat"]')?.click();
                input.focus();
              };
              viewer.append(use); row.after(viewer);
            } catch (error) { report(error.message); }
          };
          row.append(read);
        }
        if (file.webViewLink) {
          const link = el('a', 'Drive', 'drive-action'); link.href = file.webViewLink; link.target = '_blank'; link.rel = 'noopener noreferrer'; row.append(link);
        }
        const rename = el('button', 'Renombrar', 'drive-action');
        rename.onclick = async () => {
          const name = prompt('Nuevo nombre del archivo', file.name);
          if (!name?.trim() || name.trim() === file.name || !confirm(`¿Cambiar el nombre de «${file.name}» a «${name.trim()}»?`)) return;
          try { await request('/api/drive/files/' + encodeURIComponent(file.id), {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:name.trim(),confirmed:true})}); search(); }
          catch (error) { report(error.message); }
        };
        row.append(rename);
        const move = el('button', 'Mover', 'drive-action');
        move.onclick = async () => {
          try {
            const all = await request('/api/drive/files');
            const folders = (all.files || []).filter(entry => entry.mimeType === 'application/vnd.google-apps.folder' && entry.id !== file.id);
            if (!folders.length) { report('Crea primero una carpeta de destino.'); return; }
            const choice = prompt('Elige la carpeta de destino por número:\n' + folders.map((entry, index) => `${index + 1}. ${entry.name}`).join('\n'));
            const index = Number(choice) - 1;
            if (!choice || !Number.isInteger(index) || !folders[index]) return;
            const destination = folders[index];
            if (!confirm(`¿Mover «${file.name}» a «${destination.name}»?`)) return;
            await request('/api/drive/files/' + encodeURIComponent(file.id), {method:'PATCH', headers:{'Content-Type':'application/json'}, body:JSON.stringify({destination:destination.id,confirmed:true})}); search();
          }
          catch (error) { report(error.message); }
        };
        row.append(move); results.append(row);
      }
      if (nextPage) {
        const more = el('button', 'Ver más archivos', 'drive-action');
        more.onclick = () => { more.remove(); search(true); };
        results.append(more);
      }
    } catch (error) { report(error.message); }
  }
  panel.querySelector('.drive-find').onclick = () => { currentFolder = ''; search(); };
  panel.querySelector('.drive-search').onkeydown = event => { if (event.key === 'Enter') { event.preventDefault(); currentFolder = ''; search(); } };
  panel.querySelector('.drive-folder').onclick = async () => {
    if (!connected) { report('Conecta Drive antes de crear una carpeta.'); return; }
    const name = prompt('Nombre de la nueva carpeta');
    if (!name?.trim() || !confirm(`¿Crear la carpeta «${name.trim()}» en tu Drive?`)) return;
    try { await request('/api/drive/folders', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.trim(),parent:currentFolder,confirmed:true})}); search(); }
    catch (error) { report(error.message); }
  };
  window.saveOribayPdfToDrive = async (itemId, card) => {
    if (!connected) { card.append(el('p', 'Conecta Drive desde Tu espacio y vuelve a intentarlo.', 'notice')); return; }
    const name = prompt('Nombre del PDF en Drive', 'informe-oribay.pdf');
    if (!name?.trim() || !confirm(`¿Guardar «${name.trim()}» en ${currentFolder ? 'la carpeta abierta' : 'Mi unidad'}?`)) return;
    try {
      const file = await request('/api/library/' + encodeURIComponent(itemId) + '/drive', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name.trim(),folder:currentFolder,confirmed:true})});
      const note = el('p', 'PDF guardado en tu Drive. ', 'notice');
      if (file.webViewLink) { const link = el('a', 'Abrir archivo'); link.href = file.webViewLink; link.target = '_blank'; link.rel = 'noopener noreferrer'; note.append(link); }
      card.append(note); search();
    } catch (error) { card.append(el('p', error.message, 'notice')); }
  };
  request('/api/session').then(session => {
    connected = Boolean(session.drive_connected);
    connect.textContent = connected ? 'Drive conectado' : 'Conectar Drive';
    if (connected) { connect.classList.add('connected'); search(); }
    else report('Conecta Drive con tu cuenta de Oribay para buscar y guardar archivos.');
  }).catch(() => report('No se pudo comprobar la conexión de Drive.'));
})();
