/* Profile editor.
 *
 * The whole form is rendered from a JavaScript state object, so adding and
 * removing plots or series never has to reconcile hand-written DOM with the
 * document that eventually gets posted.
 */
'use strict';

(function () {
  const state = JSON.parse(document.getElementById('editor-profile').textContent);
  const meta = JSON.parse(document.getElementById('editor-config').textContent);
  const options = meta.options;

  const container = document.getElementById('plots-container');
  const previewGrid = document.getElementById('preview-grid');
  const escapeHtml = SubPlot.escapeHtml;

  let variables = [];
  let functions = [];
  let previewPlots = [];
  let previewTimer = null;

  /* ---------- helpers ---------- */
  const el = (tag, className, html) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (html != null) node.innerHTML = html;
    return node;
  };

  /** Trim an ISO string to what <input type="datetime-local"> accepts. */
  function toInputDateTime(value) {
    if (!value) return '';
    const text = String(value).replace(' ', 'T');
    return text.length >= 16 ? text.slice(0, 19) : text;
  }

  function localNowMinus(seconds) {
    const now = new Date(Date.now() - seconds * 1000);
    const pad = (n) => String(n).padStart(2, '0');
    return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}T` +
           `${pad(now.getHours())}:${pad(now.getMinutes())}:${pad(now.getSeconds())}`;
  }

  // Matches the relative expressions the server accepts: now, -1h, now-30m, -7d.
  const RELATIVE_RE = /^\s*(?:now)?\s*(?:[-+]\s*\d+(?:\.\d+)?\s*[smhdw])?\s*$/i;

  function functionKey(spec) {
    if (!spec || !spec.module || !spec.name) return '';
    return `${spec.scope || 'user'}::${spec.module}::${spec.name}`;
  }

  /* ---------- rendering ---------- */
  function render() {
    document.getElementById('profile-name').value = state.name || '';
    document.getElementById('profile-description').value = state.description || '';
    document.getElementById('profile-layout').value = state.layout || '2x2';
    document.getElementById('profile-refresh').value = state.refresh_ms || 1000;

    container.innerHTML = '';
    state.plots.forEach((plot, index) => container.appendChild(renderPlot(plot, index)));

    const count = state.plots.length;
    document.getElementById('plot-count').textContent = `${count} of ${options.max_plots} plots used`;
    document.getElementById('add-plot').disabled = count >= options.max_plots;
  }

  function renderPlot(plot, index) {
    const card = el('section', 'editor-plot');
    const header = el('header');
    header.appendChild(el('span', 'grip', `Plot ${index + 1}`));
    const controls = el('div', 'btn-row');
    if (state.plots.length > 1) {
      const remove = el('button', 'btn small danger', 'Remove');
      remove.type = 'button';
      remove.addEventListener('click', () => {
        state.plots.splice(index, 1);
        render();
      });
      controls.appendChild(remove);
    }
    header.appendChild(controls);
    card.appendChild(header);

    const body = el('div', 'body');
    body.innerHTML = `
      <div class="field-row">
        <div class="field" style="flex:2 1 220px">
          <label>Plot title</label>
          <input type="text" data-bind="title" maxlength="120" value="${escapeHtml(plot.title || '')}">
        </div>
        <div class="field" style="flex:1 1 160px">
          <label>Y-axis name</label>
          <input type="text" data-bind="y_label" maxlength="120" value="${escapeHtml(plot.y_label || '')}"
                 placeholder="e.g. Kelvin">
        </div>
        <div class="field" style="flex:0 0 140px">
          <label>Data</label>
          <select data-bind="mode">
            <option value="live"${plot.mode === 'live' ? ' selected' : ''}>Live</option>
            <option value="static"${plot.mode === 'static' ? ' selected' : ''}>Static</option>
          </select>
        </div>
        <div class="field" style="flex:0 0 150px">
          <label>Default plot type</label>
          <select data-bind="kind">
            ${options.kinds.map((k) =>
              `<option value="${k}"${plot.kind === k ? ' selected' : ''}>${k}</option>`).join('')}
          </select>
        </div>
      </div>

      <div class="field-row">
        <div class="field" style="flex:0 0 150px">
          <label>Start</label>
          <select data-role="start-mode">
            <option value="fixed">Fixed date</option>
            <option value="relative">Relative to now</option>
          </select>
        </div>
        <div class="field" data-role="start-fixed">
          <label>Start date &amp; time</label>
          <input type="datetime-local" step="1" data-role="start-date" value="${toInputDateTime(plot.start)}">
          <div class="hint" data-role="start-hint"></div>
        </div>
        <div class="field" data-role="start-relative">
          <label>Start, relative to now</label>
          <input type="text" class="mono" data-role="start-rel" value="${escapeHtml(plot.start || '')}"
                 placeholder="-1h">
          <div class="hint">e.g. <code>-30m</code>, <code>-2h</code>, <code>-7d</code>. Follows the clock as time passes.</div>
        </div>
        <div class="field" data-role="end-field">
          <label>End date &amp; time</label>
          <input type="datetime-local" step="1" data-bind="end" value="${toInputDateTime(plot.end)}">
        </div>
        <div class="field" style="flex:0 0 120px">
          <label>Y min</label>
          <input type="number" step="any" data-bind="y_min" value="${plot.y_min == null ? '' : plot.y_min}">
        </div>
        <div class="field" style="flex:0 0 120px">
          <label>Y max</label>
          <input type="number" step="any" data-bind="y_max" value="${plot.y_max == null ? '' : plot.y_max}">
        </div>
      </div>

      <div class="btn-row" style="margin-bottom:.75rem">
        <label class="inline-check">
          <input type="checkbox" data-bind="show_legend"${plot.show_legend !== false ? ' checked' : ''}> Legend
        </label>
        <label class="inline-check">
          <input type="checkbox" data-bind="show_grid"${plot.show_grid !== false ? ' checked' : ''}> Grid
        </label>
      </div>

      <h3>Variables</h3>
      <div data-role="series"></div>
      <div class="btn-row">
        <button type="button" class="btn small" data-role="add-series">+ Add variable</button>
        <span class="hint" data-role="series-count"></span>
      </div>
      <div class="function-box" data-role="function"></div>
    `;
    card.appendChild(body);

    // Bind the simple scalar fields.
    body.querySelectorAll('[data-bind]').forEach((input) => {
      const key = input.dataset.bind;
      const handler = () => {
        if (input.type === 'checkbox') plot[key] = input.checked;
        else if (input.type === 'number') plot[key] = input.value === '' ? null : parseFloat(input.value);
        else plot[key] = input.value;
        if (key === 'mode') applyModeVisibility(body, plot);
        if (key === 'kind') renderSeriesRows(body, plot);
      };
      input.addEventListener(input.tagName === 'SELECT' || input.type === 'checkbox' ? 'change' : 'input', handler);
    });

    const startMode = body.querySelector('[data-role="start-mode"]');
    startMode.value = RELATIVE_RE.test(String(plot.start || '')) ? 'relative' : 'fixed';
    startMode.addEventListener('change', () => applyStartMode(body, plot));
    body.querySelector('[data-role="start-date"]').addEventListener('input', () => applyStartMode(body, plot));
    body.querySelector('[data-role="start-rel"]').addEventListener('input', () => applyStartMode(body, plot));
    body.querySelector('[data-role="start-fixed"]').style.display =
      startMode.value === 'relative' ? 'none' : '';
    body.querySelector('[data-role="start-relative"]').style.display =
      startMode.value === 'relative' ? '' : 'none';

    applyModeVisibility(body, plot);
    renderSeriesRows(body, plot);
    renderFunctionBox(body, plot);

    body.querySelector('[data-role="add-series"]').addEventListener('click', () => {
      plot.series = plot.series || [];
      if (plot.series.length >= options.max_series) {
        Sub.toast(`A plot can show at most ${options.max_series} variables.`, 'error');
        return;
      }
      plot.series.push({
        variable: '',
        label: '',
        color: options.palette[plot.series.length % options.palette.length],
        kind: plot.kind || 'line',
        point_size: 2.5,
        width: 1.8
      });
      renderSeriesRows(body, plot);
    });

    return card;
  }

  /** Show the date picker or the relative-offset box, whichever the plot uses. */
  function applyStartMode(body, plot) {
    const select = body.querySelector('[data-role="start-mode"]');
    const relative = select.value === 'relative';
    body.querySelector('[data-role="start-fixed"]').style.display = relative ? 'none' : '';
    body.querySelector('[data-role="start-relative"]').style.display = relative ? '' : 'none';
    plot.start = relative
      ? body.querySelector('[data-role="start-rel"]').value.trim()
      : body.querySelector('[data-role="start-date"]').value;
  }

  function applyModeVisibility(body, plot) {
    const endField = body.querySelector('[data-role="end-field"]');
    const isLive = plot.mode !== 'static';
    endField.style.display = isLive ? 'none' : '';
    body.querySelector('[data-role="start-hint"]').textContent = isLive
      ? 'Plots from this time forward, updating as data arrives.'
      : 'Plots a fixed window between the two times.';
    if (!plot.start) {
      plot.start = isLive ? '-1h' : localNowMinus(3600);
      const select = body.querySelector('[data-role="start-mode"]');
      select.value = isLive ? 'relative' : 'fixed';
      body.querySelector('[data-role="start-rel"]').value = isLive ? '-1h' : '';
      body.querySelector('[data-role="start-date"]').value = isLive ? '' : plot.start;
      applyStartMode(body, plot);
    }
  }

  function renderSeriesRows(body, plot) {
    const host = body.querySelector('[data-role="series"]');
    host.innerHTML = '';
    plot.series = plot.series || [];
    plot.series.forEach((series, index) => host.appendChild(renderSeriesRow(plot, series, index, body)));
    body.querySelector('[data-role="series-count"]').textContent =
      plot.series.length ? `${plot.series.length} selected` : 'No variables selected yet';
  }

  function renderSeriesRow(plot, series, index, body) {
    const row = el('div', 'series-row');
    row.innerHTML = `
      <div class="field variable-picker">
        <label>Variable</label>
        <input type="text" class="mono" data-field="variable" autocomplete="off" spellcheck="false"
               value="${escapeHtml(series.variable || '')}" placeholder="reactor.core.temp_a">
        <div class="variable-suggestions"></div>
      </div>
      <div class="field">
        <label>Label</label>
        <input type="text" data-field="label" maxlength="120" value="${escapeHtml(series.label || '')}"
               placeholder="(variable name)">
      </div>
      <div class="field">
        <label>Type</label>
        <select data-field="kind">
          ${options.kinds.map((k) =>
            `<option value="${k}"${(series.kind || plot.kind) === k ? ' selected' : ''}>${k}</option>`).join('')}
        </select>
      </div>
      <div class="field">
        <label>Axis</label>
        <select data-field="y_axis">
          <option value="left"${(series.y_axis || 'left') === 'left' ? ' selected' : ''}>Left</option>
          <option value="right"${series.y_axis === 'right' ? ' selected' : ''}>Right</option>
        </select>
      </div>
      <div class="field">
        <label>Colour</label>
        <input type="color" data-field="color" value="${escapeHtml(series.color || '#4fc3f7')}">
      </div>
      <div class="field">
        <label>&nbsp;</label>
        <button type="button" class="btn small danger" data-role="remove">Remove</button>
      </div>
    `;

    row.querySelectorAll('[data-field]').forEach((input) => {
      const key = input.dataset.field;
      input.addEventListener(input.tagName === 'SELECT' ? 'change' : 'input', () => {
        series[key] = input.value;
      });
    });
    row.querySelector('[data-role="remove"]').addEventListener('click', () => {
      plot.series.splice(index, 1);
      renderSeriesRows(body, plot);
    });

    attachVariableAutocomplete(row.querySelector('[data-field="variable"]'),
                               row.querySelector('.variable-suggestions'),
                               (value) => { series.variable = value; });
    return row;
  }

  function attachVariableAutocomplete(input, panel, onPick) {
    let highlighted = -1;

    function close() { panel.classList.remove('open'); highlighted = -1; }

    function open() {
      const needle = input.value.trim().toLowerCase();
      const matches = variables
        .filter((name) => !needle || name.toLowerCase().includes(needle))
        .slice(0, 60);
      if (!matches.length) { close(); return; }
      panel.innerHTML = matches.map((name) =>
        `<button type="button" data-value="${escapeHtml(name)}">${escapeHtml(name)}</button>`).join('');
      panel.querySelectorAll('button').forEach((button) => {
        button.addEventListener('mousedown', (event) => {
          event.preventDefault();   // keep focus so blur does not close first
          input.value = button.dataset.value;
          onPick(button.dataset.value);
          close();
        });
      });
      panel.classList.add('open');
      highlighted = -1;
    }

    input.addEventListener('focus', open);
    input.addEventListener('input', open);
    input.addEventListener('blur', () => setTimeout(close, 120));
    input.addEventListener('keydown', (event) => {
      const buttons = Array.from(panel.querySelectorAll('button'));
      if (!buttons.length) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        highlighted += event.key === 'ArrowDown' ? 1 : -1;
        highlighted = Math.max(0, Math.min(buttons.length - 1, highlighted));
        buttons.forEach((b, i) => b.classList.toggle('highlight', i === highlighted));
        buttons[highlighted].scrollIntoView({ block: 'nearest' });
      } else if (event.key === 'Enter' && highlighted >= 0) {
        event.preventDefault();
        input.value = buttons[highlighted].dataset.value;
        onPick(input.value);
        close();
      } else if (event.key === 'Escape') {
        close();
      }
    });
  }

  function renderFunctionBox(body, plot) {
    const host = body.querySelector('[data-role="function"]');
    if (!options.functions_enabled) {
      host.innerHTML = '<span class="hint">User functions are disabled on this server.</span>';
      return;
    }
    const selected = functionKey(plot.function);
    const grouped = functions.filter((f) => !f.error);
    host.innerHTML = `
      <div class="field-row" style="align-items:flex-end">
        <div class="field" style="flex:2 1 240px">
          <label>Transform function <span class="hint" style="display:inline">(optional)</span></label>
          <select data-role="fn-select">
            <option value="">— none —</option>
            ${grouped.map((f) => {
              const key = functionKey({ scope: f.scope, module: f.module, name: f.name });
              const owner = f.scope === 'examples' ? 'examples' : 'yours';
              return `<option value="${escapeHtml(key)}"${key === selected ? ' selected' : ''}>` +
                     `${escapeHtml(f.module)}.${escapeHtml(f.name)} (${owner})</option>`;
            }).join('')}
          </select>
        </div>
        <div class="field" style="flex:2 1 240px">
          <label>Arguments</label>
          <input type="text" data-role="fn-args" class="mono" placeholder="window=30, per_seconds=60"
                 value="${escapeHtml(argsToText(plot.function && plot.function.args))}">
          <div class="hint">Comma-separated <code>name=value</code> pairs.</div>
        </div>
        <div class="field" style="flex:0 0 auto">
          <label class="inline-check">
            <input type="checkbox" data-role="fn-replace"
                   ${!plot.function || plot.function.replace_series !== false ? 'checked' : ''}>
            Hide the raw variables
          </label>
        </div>
      </div>
      <div class="function-doc" data-role="fn-doc"></div>
    `;

    const select = host.querySelector('[data-role="fn-select"]');
    const argsInput = host.querySelector('[data-role="fn-args"]');
    const replace = host.querySelector('[data-role="fn-replace"]');
    const doc = host.querySelector('[data-role="fn-doc"]');

    function showDoc() {
      const entry = grouped.find((f) => functionKey({ scope: f.scope, module: f.module, name: f.name }) === select.value);
      if (!entry) { doc.textContent = ''; return; }
      doc.textContent = (entry.signature ? `arguments: ${entry.signature}\n` : '') + (entry.doc || '');
    }

    function sync() {
      if (!select.value) { delete plot.function; doc.textContent = ''; return; }
      const [scope, module, name] = select.value.split('::');
      plot.function = {
        scope,
        module,
        name,
        owner: scope === 'examples' ? 'examples' : meta.user,
        args: textToArgs(argsInput.value),
        replace_series: replace.checked
      };
      showDoc();
    }

    select.addEventListener('change', sync);
    argsInput.addEventListener('input', sync);
    replace.addEventListener('change', sync);
    showDoc();
  }

  function argsToText(args) {
    if (!args) return '';
    return Object.entries(args).map(([key, value]) => `${key}=${value}`).join(', ');
  }

  /** Parse "window=30, mode=fast" into {window: 30, mode: "fast"}. */
  function textToArgs(text) {
    const args = {};
    String(text || '').split(',').forEach((pair) => {
      const at = pair.indexOf('=');
      if (at < 0) return;
      const key = pair.slice(0, at).trim();
      const raw = pair.slice(at + 1).trim();
      if (!key) return;
      if (raw === 'true' || raw === 'false') args[key] = raw === 'true';
      else if (raw !== '' && !isNaN(Number(raw))) args[key] = Number(raw);
      else args[key] = raw;
    });
    return args;
  }

  /* ---------- document assembly ---------- */
  function collect() {
    state.name = document.getElementById('profile-name').value.trim();
    state.description = document.getElementById('profile-description').value.trim();
    state.layout = document.getElementById('profile-layout').value;
    state.refresh_ms = parseInt(document.getElementById('profile-refresh').value, 10) || 1000;
    const document_ = JSON.parse(JSON.stringify(state));
    document_.plots.forEach((plot) => {
      if (plot.mode !== 'static') plot.end = '';
      ['y_min', 'y_max'].forEach((key) => {
        if (plot[key] === null || plot[key] === '' || Number.isNaN(plot[key])) delete plot[key];
      });
      plot.series = (plot.series || []).filter((s) => (s.variable || '').trim());
    });
    delete document_._ref;
    return document_;
  }

  /* ---------- preview ---------- */
  function ensurePreviewPanels(count, layout) {
    previewGrid.className = `plot-grid layout-${layout}`;
    // Drop the placeholder text left behind when the preview was empty.
    previewGrid.querySelectorAll(':scope > p.empty').forEach((node) => node.remove());
    while (previewPlots.length > count) {
      const panel = previewPlots.pop();
      panel.plot.destroy();
      panel.card.remove();
    }
    while (previewPlots.length < count) {
      const card = el('article', 'plot-card');
      card.innerHTML = `
        <div class="plot-head"><span class="plot-title"></span><span class="plot-mode"></span></div>
        <div class="plot-canvas-wrap"><canvas></canvas><div class="plot-tooltip"></div></div>
        <div class="plot-legend"></div>
        <div class="plot-note" style="display:none"></div>
        <div class="plot-error" style="display:none"></div>`;
      previewGrid.appendChild(card);
      previewPlots.push({
        card,
        title: card.querySelector('.plot-title'),
        mode: card.querySelector('.plot-mode'),
        wrap: card.querySelector('.plot-canvas-wrap'),
        canvas: card.querySelector('canvas'),
        legend: card.querySelector('.plot-legend'),
        note: card.querySelector('.plot-note'),
        error: card.querySelector('.plot-error'),
        image: null,
        plot: new SubPlot.Plot(card.querySelector('canvas'), card.querySelector('.plot-tooltip'))
      });
    }
  }

  async function refreshPreview(quiet) {
    const draft = collect();
    if (!draft.plots.some((p) => (p.series && p.series.length) || p.function)) {
      previewGrid.innerHTML = '<p class="empty">Add a variable or a function to see a preview.</p>';
      previewPlots.forEach((panel) => panel.plot.destroy());
      previewPlots = [];
      return;
    }
    let result;
    try {
      result = await Sub.api('/api/data', { body: { profile: draft, cursors: {} } });
    } catch (err) {
      if (!quiet) Sub.toast(err.message, 'error');
      return;
    }
    ensurePreviewPanels(result.plots.length, result.layout);
    result.plots.forEach((payload) => {
      const panel = previewPlots[payload.index];
      if (!panel) return;
      panel.title.textContent = payload.title;
      panel.mode.textContent = payload.mode;
      panel.mode.className = `plot-mode ${payload.mode}`;
      panel.error.textContent = payload.error || '';
      panel.error.style.display = payload.error ? '' : 'none';
      panel.note.textContent = payload.notice || '';
      panel.note.style.display = payload.notice ? '' : 'none';

      if (payload.image) {
        if (!panel.image) { panel.image = document.createElement('img'); panel.wrap.appendChild(panel.image); }
        panel.image.src = `data:image/png;base64,${payload.image}`;
        panel.image.style.display = '';
        panel.canvas.style.display = 'none';
        panel.legend.innerHTML = '';
        return;
      }
      if (panel.image) panel.image.style.display = 'none';
      panel.canvas.style.display = '';

      panel.plot.setSpec({
        showGrid: payload.show_grid !== false,
        yLabel: payload.y_label || '',
        yMin: payload.y_min == null ? null : payload.y_min,
        yMax: payload.y_max == null ? null : payload.y_max
      });
      panel.plot.setSeries(payload.series);
      panel.plot.setLines(payload.lines || []);
      panel.plot.setXRange(payload.start_ts, payload.end_ts);
      panel.plot.emptyMessage = 'No data in this time range yet…';
      panel.plot.draw();
      panel.legend.innerHTML = (payload.show_legend === false ? [] : payload.series.map((s) =>
        `<span class="key"><span class="swatch${s.kind === 'scatter' ? ' dot' : ''}" ` +
        `style="background:${s.color}"></span>${escapeHtml(s.label)}</span>`)).join('');
    });
  }

  /* ---------- saving ---------- */
  async function loadFolders(selectId, scope) {
    const select = document.getElementById(selectId);
    const previous = select.value;
    try {
      const result = await Sub.api(`/api/folders?scope=${encodeURIComponent(scope)}`);
      select.innerHTML = result.folders
        .map((f) => `<option value="${escapeHtml(f)}">${escapeHtml(f || '/ (top level)')}</option>`).join('');
      if (previous) select.value = previous;
    } catch (err) {
      select.innerHTML = '<option value="">/ (top level)</option>';
    }
  }

  async function save() {
    const draft = collect();
    if (!draft.name) { Sub.toast('Give the profile a name first.', 'error'); return; }
    const button = document.getElementById('save-btn');
    button.disabled = true;
    try {
      const result = await Sub.api('/api/profile/save', {
        body: { profile: draft, name: draft.name, folder: document.getElementById('save-folder').value }
      });
      Sub.toast(result.message, 'success');
      const params = new URLSearchParams(result.ref);
      window.history.replaceState({}, '', `/profiles/edit?${params.toString()}`);
    } catch (err) {
      Sub.toast(err.message, 'error');
    } finally {
      button.disabled = false;
    }
  }

  async function publish() {
    const draft = collect();
    const name = document.getElementById('publish-name').value.trim() || draft.name;
    if (!name) { Sub.toast('Give the profile a name first.', 'error'); return; }
    const button = document.getElementById('publish-confirm');
    button.disabled = true;
    try {
      const result = await Sub.api('/api/profile/publish', {
        body: { profile: draft, name, folder: document.getElementById('publish-folder').value }
      });
      Sub.toast(result.message, result.renamed ? 'info' : 'success', 9000);
      document.getElementById('publish-dialog').close();
    } catch (err) {
      Sub.toast(err.message, 'error');
    } finally {
      button.disabled = false;
    }
  }

  /* ---------- wiring ---------- */
  document.getElementById('add-plot').addEventListener('click', () => {
    if (state.plots.length >= options.max_plots) return;
    state.plots.push({
      title: `Plot ${state.plots.length + 1}`,
      y_label: '',
      mode: 'live',
      kind: 'line',
      start: '-1h',
      end: '',
      series: [],
      show_legend: true,
      show_grid: true
    });
    // Widen the layout automatically as plots are added.
    const auto = { 1: '1x1', 2: '2x1', 3: '2x2', 4: '2x2' }[state.plots.length];
    if (auto) state.layout = auto;
    render();
  });

  document.getElementById('preview-btn').addEventListener('click', () => refreshPreview(false));
  document.getElementById('preview-live').addEventListener('change', (event) => {
    if (previewTimer) { clearInterval(previewTimer); previewTimer = null; }
    if (event.target.checked) {
      refreshPreview(true);
      previewTimer = setInterval(() => { if (!document.hidden) refreshPreview(true); },
                                 Math.max(1000, state.refresh_ms || 1000));
    }
  });

  document.getElementById('save-btn').addEventListener('click', save);

  document.getElementById('publish-btn').addEventListener('click', async () => {
    const draft = collect();
    document.getElementById('publish-name').value = draft.name;
    await loadFolders('publish-folder', 'shared');
    document.getElementById('publish-dialog').showModal();
  });
  document.getElementById('publish-confirm').addEventListener('click', publish);

  document.getElementById('new-folder-btn').addEventListener('click', () => {
    document.getElementById('folder-dialog').showModal();
  });
  document.getElementById('dialog-folder-create').addEventListener('click', async () => {
    const scope = document.getElementById('dialog-folder-scope').value;
    const folder = document.getElementById('dialog-folder-path').value.trim();
    if (!folder) return;
    try {
      const result = await Sub.api('/api/folders', { body: { scope, folder } });
      Sub.toast(`Created ${scope}/${result.folder}`, 'success');
      document.getElementById('dialog-folder-path').value = '';
      document.getElementById('folder-dialog').close();
      await loadFolders('save-folder', 'private');
      if (scope === 'private') document.getElementById('save-folder').value = result.folder;
    } catch (err) {
      Sub.toast(err.message, 'error');
    }
  });

  document.querySelectorAll('dialog [data-close]').forEach((button) => {
    button.addEventListener('click', () => button.closest('dialog').close());
  });

  /* ---------- start-up ---------- */
  (async function init() {
    render();
    try {
      const [vars, fns] = await Promise.all([
        Sub.api('/api/variables'),
        options.functions_enabled ? Sub.api('/api/functions') : Promise.resolve({ functions: [] })
      ]);
      variables = vars.variables || [];
      functions = fns.functions || [];
    } catch (err) {
      Sub.toast(`Could not load the variable list: ${err.message}`, 'error');
    }
    render();
    await loadFolders('save-folder', 'private');
    if (meta.ref && meta.ref.folder) document.getElementById('save-folder').value = meta.ref.folder;
    refreshPreview(true);
  })();
})();
