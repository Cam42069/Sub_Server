/* Home page: keep the subscription status tiles current.
 *
 * The polling itself lives in app.js, which owns the banner indicator; this
 * only fills in the numbers when a status arrives. */
'use strict';

(function () {
  function setStat(name, text) {
    const node = document.querySelector(`[data-stat="${name}"]`);
    if (node) node.textContent = text;
  }

  function humanBytes(value) {
    const units = ['B', 'KB', 'MB', 'GB', 'TB'];
    let index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return index === 0 ? `${Math.round(value)} B` : `${value.toFixed(1)} ${units[index]}`;
  }

  document.addEventListener('sub:status', (event) => {
    const status = event.detail;
    if (!status) return;

    const stateNode = document.querySelector('[data-stat="node-state"]');
    if (stateNode) {
      stateNode.textContent = status.node.connected ? 'Connected' : 'Offline';
      stateNode.className = `value ${status.node.connected ? 'ok' : 'bad'}`;
    }
    setStat('variables', status.store.variables.toLocaleString());
    setStat('samples', status.store.samples.toLocaleString());
    setStat('memory', humanBytes(status.store.bytes));
    setStat('span', status.store.span_text || 'no data yet');

    const bar = document.querySelector('[data-stat="memory-bar"]');
    if (bar) {
      const percent = Math.min(100, status.store.usage_fraction * 100);
      bar.style.width = `${percent.toFixed(1)}%`;
      bar.className = percent > 85 ? 'warn' : '';
    }
    const error = document.querySelector('[data-stat="node-error"]');
    if (error) {
      error.textContent = status.node.last_error || '';
      error.style.display = status.node.last_error ? '' : 'none';
    }
  });
})();
