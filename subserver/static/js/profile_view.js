/* Profile view: renders the plot grid and drives live refreshes.
 *
 * The first request loads every plot in full.  After that only live plots are
 * requested, and each carries a cursor so the server sends just the samples
 * that arrived since the previous reply.
 */
'use strict';

(function () {
  const config = JSON.parse(document.getElementById('view-config').textContent);
  const grid = document.getElementById('plot-grid');
  const statusLabel = document.getElementById('refresh-status');
  const pauseButton = document.getElementById('pause-btn');

  const panels = Array.from(grid.querySelectorAll('.plot-card')).map((card) => {
    const wrap = card.querySelector('.plot-canvas-wrap');
    return {
      card,
      wrap,
      canvas: card.querySelector('canvas'),
      legend: card.querySelector('.plot-legend'),
      note: card.querySelector('.plot-note'),
      error: card.querySelector('.plot-error'),
      plot: new SubPlot.Plot(card.querySelector('canvas'), card.querySelector('.plot-tooltip')),
      image: null,
      mode: card.querySelector('.plot-mode').textContent.trim()
    };
  });

  const cursors = {};
  const liveIndices = panels.map((p, i) => (p.mode === 'live' ? i : -1)).filter((i) => i >= 0);
  let paused = false;
  let inFlight = false;
  let timer = null;
  let consecutiveErrors = 0;

  function setStatus(text, kind) {
    if (!statusLabel) return;
    statusLabel.textContent = text;
    statusLabel.style.color = kind === 'error' ? 'var(--danger)' : 'var(--text-faint)';
  }

  function renderLegend(panel, payload) {
    const keys = payload.series.map((s) => {
      const shape = s.kind === 'scatter' ? 'swatch dot' : 'swatch';
      return `<span class="key"><span class="${shape}" style="background:${s.color}"></span>` +
             `${SubPlot.escapeHtml(s.label)}</span>`;
    });
    payload.lines.forEach((line) => {
      keys.push(`<span class="key"><span class="swatch" style="background:${line.color}"></span>` +
                `${SubPlot.escapeHtml(line.label)}</span>`);
    });
    panel.legend.innerHTML = payload.show_legend === false ? '' : keys.join('');
  }

  function showImage(panel, base64) {
    if (!panel.image) {
      panel.image = document.createElement('img');
      panel.wrap.appendChild(panel.image);
    }
    panel.image.src = `data:image/png;base64,${base64}`;
    panel.image.style.display = '';
    panel.canvas.style.display = 'none';
  }

  function hideImage(panel) {
    if (panel.image) panel.image.style.display = 'none';
    panel.canvas.style.display = '';
  }

  function applyPayload(payload) {
    const panel = panels[payload.index];
    if (!panel) return;

    panel.error.textContent = payload.error || '';
    panel.error.style.display = payload.error ? '' : 'none';
    panel.note.textContent = payload.notice || '';
    panel.note.style.display = payload.notice ? '' : 'none';

    if (payload.image) {
      showImage(panel, payload.image);
      panel.legend.innerHTML = '';
      cursors[payload.index] = payload.cursor;
      return;
    }
    hideImage(panel);

    panel.plot.setSpec({
      showGrid: payload.show_grid !== false,
      yLabel: payload.y_label || '',
      yMin: payload.y_min == null ? null : payload.y_min,
      yMax: payload.y_max == null ? null : payload.y_max
    });

    if (payload.incremental) {
      panel.plot.appendSeries(payload.series);
    } else {
      panel.plot.setSeries(payload.series);
    }
    panel.plot.setLines(payload.lines || []);
    panel.plot.trimBefore(payload.start_ts);
    panel.plot.setXRange(payload.start_ts, payload.end_ts);
    panel.plot.emptyMessage = payload.error
      ? 'Could not draw this plot'
      : 'No data in this time range yet…';
    panel.plot.draw();

    if (!payload.incremental) renderLegend(panel, payload);

    // A long-running live plot accumulates raw points; once it has too many,
    // drop the cursor so the next request returns a freshly decimated window.
    if (panel.plot.needsRefresh()) {
      delete cursors[payload.index];
    } else if (payload.cursor != null) {
      cursors[payload.index] = payload.cursor;
    }
  }

  async function load(indices) {
    if (inFlight) return;
    inFlight = true;
    const body = { ref: config.ref, cursors };
    if (indices) body.plots = indices;
    try {
      const result = await Sub.api('/api/data', { body });
      result.plots.forEach(applyPayload);
      consecutiveErrors = 0;
      setStatus(indices ? `Updated ${new Date().toLocaleTimeString()}` : 'Loaded');
    } catch (err) {
      consecutiveErrors += 1;
      setStatus(err.message, 'error');
      if (consecutiveErrors === 1) Sub.toast(err.message, 'error');
    } finally {
      inFlight = false;
    }
  }

  function schedule() {
    if (timer) clearTimeout(timer);
    if (paused || !liveIndices.length) return;
    // Back off when the server is unhappy rather than hammering it.
    const interval = Math.max(250, config.refresh_ms || 1000) *
                     Math.min(8, Math.pow(2, Math.max(0, consecutiveErrors - 1)));
    timer = setTimeout(async () => {
      if (document.hidden) { schedule(); return; }   // don't poll a hidden tab
      await load(liveIndices);
      schedule();
    }, interval);
  }

  if (pauseButton) {
    if (!liveIndices.length) {
      pauseButton.disabled = true;
      pauseButton.textContent = 'No live plots';
    }
    pauseButton.addEventListener('click', () => {
      paused = !paused;
      pauseButton.dataset.paused = String(paused);
      pauseButton.textContent = paused ? 'Resume live updates' : 'Pause live updates';
      setStatus(paused ? 'Paused' : 'Resuming…');
      if (!paused) { load(liveIndices).then(schedule); }
    });
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && !paused && liveIndices.length) load(liveIndices);
  });

  window.addEventListener('beforeunload', () => { if (timer) clearTimeout(timer); });

  load(null).then(schedule);
})();
