/* Shared UI behaviour: the toolbar menus, toasts, and the JSON fetch helper. */
'use strict';

const Sub = (() => {
  const csrfToken = document.body ? document.body.dataset.csrf || '' : '';

  /** POST/GET JSON, attaching the CSRF token and unwrapping the error shape. */
  async function api(url, options = {}) {
    const config = Object.assign({ headers: {} }, options);
    config.headers = Object.assign(
      { 'Accept': 'application/json', 'X-CSRF-Token': csrfToken },
      config.headers
    );
    if (config.body !== undefined && typeof config.body !== 'string') {
      config.headers['Content-Type'] = 'application/json';
      config.body = JSON.stringify(config.body);
      config.method = config.method || 'POST';
    }
    let response;
    try {
      response = await fetch(url, config);
    } catch (err) {
      throw new Error('Could not reach the server. Is it still running?');
    }
    if (response.status === 401) {
      window.location.href = '/login';
      throw new Error('Signed out.');
    }
    let payload;
    try {
      payload = await response.json();
    } catch (err) {
      throw new Error(`The server returned an unexpected response (HTTP ${response.status}).`);
    }
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `Request failed (HTTP ${response.status}).`);
    }
    return payload;
  }

  function toast(message, kind = 'info', timeout = 5000) {
    const stack = document.getElementById('toasts');
    if (!stack) { console.log(kind, message); return; }
    const node = document.createElement('div');
    node.className = `toast ${kind}`;
    node.textContent = message;
    stack.appendChild(node);
    setTimeout(() => node.remove(), timeout);
  }

  function relativeTime(epochSeconds) {
    const delta = Date.now() / 1000 - epochSeconds;
    if (!isFinite(delta)) return '—';
    if (delta < 60) return 'just now';
    if (delta < 3600) return `${Math.floor(delta / 60)} min ago`;
    if (delta < 86400) return `${Math.floor(delta / 3600)} h ago`;
    if (delta < 7 * 86400) return `${Math.floor(delta / 86400)} d ago`;
    return new Date(epochSeconds * 1000).toLocaleDateString();
  }

  /** Render every [data-epoch] element as a relative time. */
  function renderTimestamps(root = document) {
    root.querySelectorAll('[data-epoch]').forEach((node) => {
      const value = parseFloat(node.dataset.epoch);
      if (isFinite(value)) {
        node.textContent = relativeTime(value);
        node.title = new Date(value * 1000).toLocaleString();
      }
    });
  }

  /* ---------- toolbar dropdowns ---------- */
  function initToolbar() {
    const toolbar = document.getElementById('toolbar');
    if (!toolbar) return;
    const items = Array.from(toolbar.querySelectorAll(':scope > li'));

    function closeAll(except) {
      items.forEach((item) => {
        if (item === except) return;
        item.classList.remove('open');
        const button = item.querySelector('.toolbar-item');
        if (button && button.tagName === 'BUTTON') button.setAttribute('aria-expanded', 'false');
      });
    }

    function open(item, button) {
      closeAll(item);
      item.classList.add('open');
      button.setAttribute('aria-expanded', 'true');
    }

    items.forEach((item) => {
      const button = item.querySelector('button.toolbar-item');
      if (!button) return;
      button.addEventListener('click', (event) => {
        event.stopPropagation();
        // A hover-switch opens the menu before the click lands on it; without
        // this, that click would read as "already open" and toggle it shut.
        if (item.dataset.hoverOpened === '1') {
          delete item.dataset.hoverOpened;
          return;
        }
        if (item.classList.contains('open')) {
          closeAll(null);
        } else {
          open(item, button);
        }
      });
      // Once one menu is open, hovering across the bar switches between them,
      // which is how desktop menu bars behave.
      item.addEventListener('mouseenter', () => {
        if (items.some((other) => other !== item && other.classList.contains('open'))) {
          open(item, button);
          item.dataset.hoverOpened = '1';
        }
      });
      item.addEventListener('mouseleave', () => { delete item.dataset.hoverOpened; });
    });

    document.addEventListener('click', () => closeAll(null));
    document.addEventListener('keydown', (event) => {
      if (event.key === 'Escape') closeAll(null);
    });
  }

  /* ---------- shared status polling ---------- */
  /* One poll per page drives the banner dot and feeds any page that wants the
     numbers, so pages do not each open their own timer. */
  let statusTimer = null;

  async function pollStatus() {
    const indicator = document.getElementById('node-indicator');
    let status = null;
    try {
      status = await Sub.api('/api/status');
    } catch (err) {
      if (indicator) {
        indicator.className = 'dot down';
        indicator.title = 'Cannot reach the server';
      }
      document.dispatchEvent(new CustomEvent('sub:status', { detail: null }));
      return;
    }
    if (indicator) {
      const connected = status.node.connected;
      indicator.className = `dot ${connected ? 'live' : 'down'}`;
      indicator.title = connected
        ? `Connected to ${status.node.host}:${status.node.port}`
        : (status.node.last_error || 'Not connected to the data node');
    }
    document.dispatchEvent(new CustomEvent('sub:status', { detail: status }));
  }

  function startStatusPolling(intervalMs = 5000) {
    if (statusTimer || !document.getElementById('node-indicator')) return;
    pollStatus();
    statusTimer = setInterval(() => { if (!document.hidden) pollStatus(); }, intervalMs);
  }

  document.addEventListener('DOMContentLoaded', () => {
    initToolbar();
    renderTimestamps();
    startStatusPolling();
  });

  return { api, toast, relativeTime, renderTimestamps, csrfToken, pollStatus };
})();

window.Sub = Sub;
