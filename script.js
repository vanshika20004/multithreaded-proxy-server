const API_BASE = 'http://localhost:5000';

function showMessage(text) {
  const div = document.createElement('div');
  div.className = 'msg';
  div.textContent = text;
  document.body.appendChild(div);
  setTimeout(() => div.remove(), 2500);
}

async function api(path, options = {}) {
  try {
    const res = await fetch(`${API_BASE}${path}`, {
      headers: { 'Content-Type': 'application/json' },
      ...options,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  } catch (err) {
    console.error('API error:', err);
    showMessage(`Error: ${err.message}`);
    throw err;
  }
}

// Dashboard
async function startServer() {
  const data = await api('/control/start', { method: 'POST' });
  showMessage(data.message || 'Server started');
  await updateStatus();
}

async function stopServer() {
  const data = await api('/control/stop', { method: 'POST' });
  showMessage(data.message || 'Server stopped');
  await updateStatus();
}

async function updateStatus() {
  const s = await api('/status');
  const indicator = document.querySelector('.indicator');
  const runningText = document.getElementById('running-text');
  const threadsEl = document.getElementById('threads');
  const cacheCountEl = document.getElementById('cache-count');
  const blockedCountEl = document.getElementById('blocked-count');
  if (indicator) {
    indicator.className = 'indicator ' + (s.running ? 'on' : 'off');
  }
  if (runningText) runningText.textContent = s.running ? 'Running' : 'Stopped';
  if (threadsEl) threadsEl.textContent = s.active_threads;
  if (cacheCountEl) cacheCountEl.textContent = s.cache_entries;
  if (blockedCountEl) blockedCountEl.textContent = s.blocked_count;
}

function initDashboard() {
  updateStatus();
  setInterval(updateStatus, 3000);
  const startBtn = document.getElementById('start-btn');
  const stopBtn = document.getElementById('stop-btn');
  if (startBtn) startBtn.addEventListener('click', startServer);
  if (stopBtn) stopBtn.addEventListener('click', stopServer);
}

// Logs
async function refreshLogs() {
  const data = await api('/logs/view');
  const tbody = document.querySelector('#logs-table tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  (data.logs || []).slice().reverse().forEach(row => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${row.timestamp || ''}</td>
      <td>${row.client_ip || ''}</td>
      <td>${row.url || ''}</td>
      <td>${row.action || ''}</td>
    `;
    tbody.appendChild(tr);
  });
}

async function clearLogs() {
  await api('/logs/clear', { method: 'POST' });
  showMessage('Logs cleared');
  refreshLogs();
}

function initLogs() {
  refreshLogs();
  document.getElementById('refresh-logs')?.addEventListener('click', refreshLogs);
  document.getElementById('clear-logs')?.addEventListener('click', clearLogs);
  // Live toggle-controlled auto-refresh
  const toggle = document.getElementById('logs-live-toggle');
  let timer = null;
  const setLive = (on) => {
    if (timer) { clearInterval(timer); timer = null; }
    if (on) timer = setInterval(refreshLogs, 2000);
  };
  setLive(toggle ? toggle.checked : true);
  toggle?.addEventListener('change', (e) => setLive(e.target.checked));
}

// Cache
async function refreshCache() {
  const data = await api('/cache/view');
  const tbody = document.querySelector('#cache-table tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  (data.cache || []).slice().reverse().forEach(row => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${row.timestamp || ''}</td>
      <td>${row.url || ''}</td>
      <td>${row.size_bytes || 0}</td>
    `;
    tbody.appendChild(tr);
  });
}

async function clearCache() {
  await api('/cache/clear', { method: 'POST' });
  showMessage('Cache cleared');
  refreshCache();
}

function initCache() {
  refreshCache();
  document.getElementById('refresh-cache')?.addEventListener('click', refreshCache);
  document.getElementById('clear-cache')?.addEventListener('click', clearCache);
  // Live toggle-controlled auto-refresh
  const toggle = document.getElementById('cache-live-toggle');
  let timer = null;
  const setLive = (on) => {
    if (timer) { clearInterval(timer); timer = null; }
    if (on) timer = setInterval(refreshCache, 3000);
  };
  setLive(toggle ? toggle.checked : true);
  toggle?.addEventListener('change', (e) => setLive(e.target.checked));
}

// Filter
async function refreshFilters() {
  const data = await api('/filter/view');
  const tbody = document.querySelector('#filter-table tbody');
  if (!tbody) return;
  tbody.innerHTML = '';
  (data.blocked || []).forEach(domain => {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td>${domain}</td>
      <td style="text-align:right">
        <button class="btn danger" data-domain="${domain}">Remove</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
  tbody.querySelectorAll('button[data-domain]').forEach(btn => {
    btn.addEventListener('click', async (e) => {
      const d = e.currentTarget.getAttribute('data-domain');
      if (confirm(`Remove '${d}' from blocked list?`)) {
        await api('/filter/remove', { method: 'POST', body: JSON.stringify({ domain: d }) });
        showMessage('Domain removed');
        refreshFilters();
      }
    });
  });
}

async function addFilter() {
  const input = document.getElementById('domain-input');
  const domain = (input?.value || '').trim();
  if (!domain) return;
  await api('/filter/add', { method: 'POST', body: JSON.stringify({ domain }) });
  input.value = '';
  showMessage('Domain added');
  refreshFilters();
}

function initFilter() {
  refreshFilters();
  document.getElementById('add-domain')?.addEventListener('click', addFilter);
  // Auto-refresh filters every 5s in case of concurrent updates
  setInterval(refreshFilters, 5000);
}

function extractDomainFromUrl(u) {
  try {
    if (!u) return '';
    if (!u.includes('://') && u.includes(':')) return u.split(':')[0];
    const url = new URL(u);
    return url.hostname || u;
  } catch {
    return (u || '').replace(/^https?:\/\//, '').split('/')[0].split(':')[0];
  }
}

async function refreshRecentVisits() {
  const data = await api('/logs/view');
  const container = document.getElementById('recent-visits');
  if (!container) return;
  const logs = (data.logs || []).slice().reverse();
  const domains = [];
  const seen = new Set();
  for (const row of logs) {
    const d = extractDomainFromUrl(row.url);
    if (!d) continue;
    if (!seen.has(d)) {
      seen.add(d);
      domains.push({ domain: d, action: row.action, ts: row.timestamp });
      if (domains.length >= 8) break;
    }
  }
  container.innerHTML = domains.map(it => `<li style=\"display:flex; justify-content:space-between; padding:6px 0; border-bottom:1px solid #22303f;\">\n    <span>${it.domain}</span>\n    <span class=\"sub\">${it.action || ''}</span>\n  </li>`).join('');
}

// Page router
document.addEventListener('DOMContentLoaded', () => {
  const page = document.body.dataset.page;
  document.querySelectorAll('.navbar .links a').forEach(a => {
    const href = a.getAttribute('href') || '';
    if (href.includes(page)) a.classList.add('active');
  });
  if (page === 'dashboard') { initDashboard(); refreshRecentVisits(); setInterval(refreshRecentVisits, 3000); }
  if (page === 'logs') initLogs();
  if (page === 'cache') initCache();
  if (page === 'filter') initFilter();
});