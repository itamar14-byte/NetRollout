// System Settings - see templates/admin_settings.html.
(() => {
  // The registry and rules as the server enforces them - the page checks the
  // same things before sending; the server checks again (authoritative).
  let SETTINGS = nrData('nr-settings');
  const RULES = nrData('nr-rules');
  const RULES_KEY = '_rules';

  const form = document.getElementById('settingsForm');
  const saveBtn = document.getElementById('saveBtn');
  const discardBtn = document.getElementById('discardBtn');
  const dirtyHint = document.getElementById('dirtyHint');
  const alertBox = document.getElementById('alertBox');
  const byKey = () => Object.fromEntries(SETTINGS.map(s => [s.key, s]));

  function input(key) { return document.getElementById('set-' + key); }

  // ── Field validation: mirrors Setting.parse on the server ──
  function parseField(s, raw) {
    if (s.kind === 'int') {
      const text = String(raw).trim();
      if (!/^-?\d+$/.test(text)) throw `${s.label}: must be a whole number`;
      const n = parseInt(text, 10);
      if (s.minimum !== null && n < s.minimum) throw `${s.label}: must be at least ${s.minimum}`;
      if (s.maximum !== null && n > s.maximum) throw `${s.label}: must be at most ${s.maximum}`;
      return n;
    }
    const v = String(raw ?? '').trim();
    if (s.choices) {
      if (!s.choices.some(c => c[0] === v))
        throw `${s.label}: must be one of: ${s.choices.map(c => c[1]).join(', ')}`;
      return v;
    }
    if (s.required && !v) throw `${s.label}: required`;
    if (s.max_length && v.length > s.max_length) throw `${s.label}: must be at most ${s.max_length} characters`;
    if (v && s.pattern && !new RegExp('^(?:' + s.pattern + ')$').test(v))
      throw `${s.label}: ${s.pattern_hint || 'has an invalid format'}`;
    return v;
  }

  function showFieldError(key, msg) {
    const el = input(key), box = document.getElementById('err-' + key);
    el.classList.toggle('is-invalid', !!msg);
    el.setAttribute('aria-invalid', msg ? 'true' : 'false');
    box.textContent = msg || '';
    box.classList.toggle('show', !!msg);
  }

  function showAlert(kind, html) {
    alertBox.className = 'set-alert show ' + kind;
    alertBox.innerHTML = html;
    // it sits at the top: a save made far down the page would go unseen
    const box = alertBox.getBoundingClientRect();
    if (box.top < 60 || box.bottom > window.innerHeight) {   // 60: under the sticky top bar
      const still = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      alertBox.scrollIntoView({behavior: still ? 'auto' : 'smooth', block: 'start'});
    }
  }
  function clearAlert() { alertBox.className = 'set-alert'; alertBox.textContent = ''; }

  // ── Edited fields, validated; returns {values, errors, ruleErrors} ──
  function collect() {
    const current = byKey(), values = {}, errors = {};
    const merged = Object.fromEntries(SETTINGS.map(s => [s.key, s.value]));
    for (const s of SETTINGS) {
      if (!s.editable) continue;
      const el = input(s.key);
      if (String(el.value).trim() === String(s.value ?? '')) continue;
      try {
        const v = parseField(current[s.key], el.value);
        if (v !== s.value) { values[s.key] = v; merged[s.key] = v; }
      } catch (msg) { errors[s.key] = msg; }
    }
    // cross-setting rules: the same list update() checks on the server
    const touched = new Set(Object.keys(values));
    const ruleErrors = RULES.filter(r => (touched.has(r.left) || touched.has(r.right)) &&
      !(r.op === '>=' ? merged[r.left] >= merged[r.right] : merged[r.left] <= merged[r.right]))
      .map(r => r.message);
    return {values, errors, ruleErrors};
  }

  function refreshDirty() {
    let dirty = 0;
    for (const s of SETTINGS) {
      if (!s.editable) continue;
      const changed = String(input(s.key).value).trim() !== String(s.value ?? '');
      input(s.key).classList.toggle('dirty', changed);
      dirty += changed;
    }
    saveBtn.disabled = discardBtn.disabled = !dirty;
    dirtyHint.textContent = dirty ? `${dirty} unsaved change${dirty > 1 ? 's' : ''}` : 'No unsaved changes';
  }

  // Server state → inputs and markers (after save / reset)
  function applyState(state) {
    SETTINGS = state.settings;
    for (const s of SETTINGS) {
      const row = document.querySelector(`.set-row[data-key="${s.key}"]`);
      input(s.key).value = s.value ?? '';
      showFieldError(s.key, '');
      row.querySelector('[data-marker="changed"]').hidden = !s.changed || s.from_install;
      row.querySelector('[data-marker="install"]').hidden = !s.from_install;
      row.querySelector('[data-reset]').hidden = !s.changed;
    }
    // only when the server reported it (a save / reset), not on Discard
    if (Array.isArray(state.restart_pending) && window.setSettingsRestartPending)
      setSettingsRestartPending(state.restart_pending);
    if (state.port) renderPort(state.port);
    if (state.access) renderAccess(state.access);
    if (window.nrSyncBackups) nrSyncBackups(state.restart_pending !== null);
    refreshDirty();
  }

  // ── What nginx last did and the certificate in use (shown whenever the page
  // is open, not only after a save; _access_status.html) ──
  const accessBox = document.getElementById('accessStatus');
  const renderAccess = a => nrAccessStatus(accessBox, a);
  renderAccess(nrData('nr-access'));

  // ── HTTPS port: applied by the server's port helper (src/webapp/port_apply.py).
  // A trial opens the new port next to the current one; the page served on the
  // new port confirms it - unconfirmed, the helper goes back.
  const portBox = document.getElementById('portState');
  let portTimer = null, probedId = null;
  const onPort = p => `https://${location.hostname}:${p}`;

  function renderPort(p) {
    clearTimeout(portTimer);
    const s = p.state;
    if (s === 'applied') { portBox.className = 'set-alert port-state'; portBox.textContent = ''; return; }
    let kind = 'warn', icon = 'bi-hourglass-split', html;
    if (s === 'manual') {
      icon = 'bi-info-circle';
      html = `Port ${p.saved} is saved. NetRollout stays on port ${p.serving} until ` +
             '<code>netrollout apply</code> is run on the server.';
    } else if (s === 'waiting') {
      html = `Opening port ${p.saved}… NetRollout is unavailable for a few seconds while nginx restarts.`;
    } else if (s === 'no_helper_answer') {
      icon = 'bi-exclamation-triangle';
      html = `The server hasn't picked up port ${p.saved} - check <code>netrollout status</code> on the server. ` +
             `NetRollout stays on port ${p.serving}.`;
    } else if (s === 'trying') {
      const url = onPort(p.trying) + '/admin/settings?confirm_port=' + encodeURIComponent(p.id);
      const left = p.deadline ? Math.max(0, Math.round(p.deadline - Date.now() / 1000)) : null;
      icon = 'bi-signpost-split';
      html = `Port ${p.trying} is open next to port ${p.serving}. <a href="${nrEscape(url)}">Open NetRollout on port ${p.trying}</a> ` +
             `to keep it${left !== null ? ` (${left} s left)` : ''} - if it doesn't open (e.g. a firewall blocks it), ` +
             `port ${p.serving} stays.`;
      // reachable from this browser → go there; the page there confirms
      if (probedId !== p.id) {
        probedId = p.id;
        fetch(onPort(p.trying) + '/_netrollout/health', {mode: 'no-cors', cache: 'no-store'})
          .then(() => { location.href = url; }).catch(() => {});
      }
    } else if (s === 'confirming') {
      html = `Keeping port ${p.saved}…`;
    } else if (s === 'rolled_back' || s === 'failed') {
      kind = 'error'; icon = 'bi-x-circle';
      html = `Port ${p.saved} wasn't applied: ${nrEscape(p.message || 'no reason given')}. ` +
             `NetRollout stays on port ${p.serving}. ` +
             '<button type="button" class="btn btn-sm btn-outline-warning" data-port-retry>Try again</button>';
    } else {
      html = `Port change: unknown state (${nrEscape(s)}) - check <code>netrollout status</code> on the server.`;
    }
    portBox.className = 'set-alert show port-state ' + kind;
    portBox.innerHTML = `<i class="bi ${icon} me-1"></i>` + html;
    if (['waiting', 'trying', 'confirming'].includes(s)) portTimer = setTimeout(pollPort, 1500);
  }

  async function pollPort() {
    try {
      // a background request: watching this must not keep the session alive
      const resp = await fetch('/admin/settings/port?_bg=1', {headers: {'X-NR-Background': '1'}});
      if (resp.status === 401) return;          // signed out: _idle_timeout takes over
      const data = await resp.json();
      if (data.status === 'ok') { renderPort(data.port); return; }
    } catch (_) {}                              // nginx restarting: ask again
    portTimer = setTimeout(pollPort, 1500);
  }

  portBox.addEventListener('click', async e => {
    if (!e.target.closest('[data-port-retry]')) return;
    const {resp, data} = await post('/admin/settings/port/retry');
    if (resp.ok && data.status === 'ok') renderPort(data.port);
    else showAlert('error', 'Not retried: ' + nrEscape(data.message || `server error (${resp.status})`));
  });

  function showServerErrors(data) {
    const errors = data.errors || {};
    for (const [key, msg] of Object.entries(errors)) {
      if (key !== RULES_KEY && input(key)) showFieldError(key, msg);
    }
    const general = errors[RULES_KEY] ? [errors[RULES_KEY]] : [];
    showAlert('error', '<i class="bi bi-x-circle me-1"></i>Not saved. ' +
      (general.length ? nrEscape(general.join(' ')) : 'Fix the highlighted fields.'));
  }

  async function post(url, body) {
    const resp = await fetch(url, {method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
      body: JSON.stringify(body || {})});
    let data = {};
    try { data = await resp.json(); } catch (_) {}
    return {resp, data};
  }

  // ── Live checks while typing ──
  form.addEventListener('input', e => {
    const key = e.target.name;
    if (!key) return;
    const s = byKey()[key];
    try { parseField(s, e.target.value); showFieldError(key, ''); }
    catch (msg) { showFieldError(key, msg); }
    refreshDirty();
  });

  // ── Save ──
  form.addEventListener('submit', async e => {
    e.preventDefault();
    clearAlert();
    const {values, errors, ruleErrors} = collect();
    for (const s of SETTINGS) if (s.editable) showFieldError(s.key, errors[s.key] || '');
    if (Object.keys(errors).length || ruleErrors.length) {
      showAlert('error', '<i class="bi bi-x-circle me-1"></i>Not saved. ' +
        (ruleErrors.length ? nrEscape(ruleErrors.join(' ')) : 'Fix the highlighted fields.'));
      return;                                   // nothing sent
    }
    if (!Object.keys(values).length) { refreshDirty(); return; }
    saveBtn.disabled = true;
    const {resp, data} = await post('/admin/settings', {values});
    if (resp.ok && data.status === 'ok') {
      applyState(data);
      const n = data.changed.length;
      let msg = `<i class="bi bi-check-circle me-1"></i>Saved ${n} setting${n === 1 ? '' : 's'}.`;
      if ((data.restart_pending || []).length)
        msg += ' ' + nrEscape(data.restart_pending.join(', ')) + ' applies after a restart (Restart, bottom left).';
      // what nginx did with a new hostname
      const proxy = data.proxy, proxyWarn = proxy && proxy.state === 'no_answer';
      if (proxy) msg += ' ' + {
        applied: 'nginx applied it' + (proxy.message ? ' (' + nrEscape(proxy.message) + ')' : '') + '.',
        not_managed: 'Your own reverse proxy serves NetRollout - update it for the new hostname.',
        no_answer: "nginx didn't confirm within a few seconds - check <code>netrollout status</code> on the server.",
      }[proxy.state] || '';
      showAlert((data.restart_pending || []).length || proxyWarn ? 'warn' : 'success', msg);
    } else if (resp.status === 422) {
      showServerErrors(data);
      refreshDirty();
    } else {
      showAlert('error', 'Not saved: ' + nrEscape(data.message || `server error (${resp.status})`));
      refreshDirty();
    }
  });

  discardBtn.addEventListener('click', () => { clearAlert(); applyState({settings: SETTINGS,
    restart_pending: null}); });

  // Opened on the new port during a trial (the link above): confirm from here.
  const confirmId = new URLSearchParams(location.search).get('confirm_port');
  if (confirmId) {
    history.replaceState(null, '', location.pathname);
    post('/admin/settings/port/confirm', {id: confirmId}).then(({resp, data}) => {
      if (resp.ok && data.status === 'ok') {
        showAlert('success', `<i class="bi bi-check-circle me-1"></i>Port ${nrEscape(location.port || 443)} kept - ` +
          `NetRollout is now at <code>${nrEscape(location.origin)}</code>.`);
        renderPort(data.port);
      } else {
        showAlert('error', 'Port not confirmed: ' + nrEscape(data.message || `server error (${resp.status})`));
      }
    });
  } else {
    renderPort(nrData('nr-port'));
  }

  // ── Reset to default ──
  form.addEventListener('click', async e => {
    const key = e.target.dataset && e.target.dataset.reset;
    if (!key) return;
    clearAlert();
    const {resp, data} = await post(`/admin/settings/${encodeURIComponent(key)}/reset`);
    if (resp.ok && data.status === 'ok') {
      applyState(data);
      showAlert('success', `<i class="bi bi-arrow-counterclockwise me-1"></i>${nrEscape(byKey()[key].label)} reset to its default.`);
    } else if (resp.status === 422) {
      showServerErrors(data);
    } else {
      showAlert('error', 'Not reset: ' + nrEscape(data.message || `server error (${resp.status})`));
    }
  });

  // ── Test access (two-step reverse-proxy check) ──
  const testBtn = document.getElementById('testBtn');
  if (testBtn) testBtn.addEventListener('click', async () => {
    const out = document.getElementById('testResult');
    const hostname = input('public_hostname').value, port = input('https_port').value;
    try {
      parseField(byKey().public_hostname, hostname);
      parseField(byKey().https_port, port);
    } catch (msg) { out.innerHTML = `<span class="fail">✗ ${nrEscape(msg)}</span>`; return; }
    testBtn.disabled = true;
    out.textContent = 'Checking…';
    const {resp, data} = await post('/admin/settings/test', {hostname, port});
    testBtn.disabled = false;
    if (!resp.ok) { out.innerHTML = `<span class="fail">✗ ${nrEscape(data.message || 'check failed')}</span>`; return; }
    if (data.container) {
      // from inside the container the published port can't be probed: what
      // nginx itself last reported (the status below, refreshed)
      if (data.access) renderAccess(data.access);
      out.innerHTML = (data.url ? `${nrEscape(data.url)}<br>` : '') + 'nginx: ' + nrNginxLine(data.access && data.access.nginx);
      return;
    }
    const line = (name, r) => r.ok
      ? `<span class="ok">✓ ${name}: forwards to this NetRollout</span>`
      : `<span class="fail">✗ ${name}: ${nrEscape(r.reason)}</span>`;
    out.innerHTML = `${nrEscape(data.url)} <span style="color:var(--nr-text-muted)">(${nrEscape(data.source)})</span><br>` +
      line('nginx on this machine', data.local) + '<br>' + line('via the URL', data.public);
  });

  // ── Backups: the list, Back up now, Download, Delete (admin_backups.py) ──
  const KIND = {manual: 'manual', scheduled: 'scheduled', 'before-restore': 'before restore',
                'before-upgrade': 'before upgrade'};
  const size = n => n >= 1048576 ? (n / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
  const when = iso => iso ? new Date(iso).toLocaleString([], {dateStyle: 'medium', timeStyle: 'short'}) : null;
  const rowOf = key => document.querySelector(`.set-row[data-key="${key}"]`);

  function syncBackupRows() {
    // the time / day / keep matter only when they're used
    const schedule = input('backup_schedule').value;
    rowOf('backup_time').hidden = rowOf('backup_keep').hidden = schedule === 'off';
    rowOf('backup_weekday').hidden = schedule !== 'weekly';
  }

  function renderBackups(b) {
    const next = document.getElementById('backupNext'), last = document.getElementById('backupLast');
    next.textContent = b.next ? `Next scheduled backup: ${when(b.next)}` : 'Scheduled backups are off.';
    if (b.last && b.last.ok) {
      last.className = 'set-alert show success bk-last';
      last.innerHTML = `<i class="bi bi-check-circle me-1"></i>Last scheduled backup: ${nrEscape(when(b.last.time))} - OK.`;
    } else if (b.last) {
      last.className = 'set-alert show error bk-last';
      last.innerHTML = `<i class="bi bi-exclamation-triangle me-1"></i>The last scheduled backup failed ` +
        `(${nrEscape(when(b.last.time))}): ${nrEscape(b.last.message)}. It's tried again within the hour.`;
    } else { last.className = 'set-alert bk-last'; last.textContent = ''; }
    const list = document.getElementById('backupList');
    if (!b.backups.length) { list.innerHTML = '<div class="bk-empty">No backups yet.</div>'; return; }
    const kindClass = k => k === 'manual' ? 'manual' : k.startsWith('before') ? 'before' : '';
    list.innerHTML = '<table class="bk-table"><thead><tr><th>Made</th><th>Kind</th><th>Version</th>' +
      '<th style="text-align:right">Size</th><th><span class="visually-hidden">Actions</span></th></tr></thead><tbody>' +
      b.backups.map(x => '<tr><td>' +
        (x.problem ? `<span class="bk-bad" title="${nrEscape(x.problem)}">damaged</span> ` + nrEscape(x.name)
                   : nrEscape(when(x.created))) + '</td>' +
        `<td><span class="bk-kind ${kindClass(x.kind)}">${nrEscape(KIND[x.kind] || x.kind)}</span></td>` +
        `<td>${nrEscape(x.version || '-')}</td><td class="num">${size(x.size)}</td><td class="acts">` +
        `<a class="btn btn-sm btn-outline-secondary" href="/admin/backups/${encodeURIComponent(x.name)}" ` +
        `title="Download ${nrEscape(x.name)}" aria-label="Download ${nrEscape(x.name)}"><i class="bi bi-download"></i></a>` +
        `<button type="button" class="btn btn-sm btn-outline-danger" data-backup-delete="${nrEscape(x.name)}" ` +
        `title="Delete ${nrEscape(x.name)}" aria-label="Delete ${nrEscape(x.name)}"><i class="bi bi-trash3"></i></button>` +
        '</td></tr>').join('') +
      `</tbody></table><div class="bk-next" style="margin-top:0.4rem">${b.backups.length} ` +
      `backup${b.backups.length === 1 ? '' : 's'}, ${size(b.total)} in the backups folder.</div>`;
  }

  async function refreshBackups() {
    try {
      const resp = await fetch('/admin/backups', {headers: {'X-Requested-With': 'XMLHttpRequest'}});
      const data = await resp.json();
      if (data.status === 'ok') renderBackups(data);
    } catch (_) {}
  }

  const backupNowBtn = document.getElementById('backupNowBtn');
  backupNowBtn.addEventListener('click', async () => {
    clearAlert();
    backupNowBtn.disabled = true;
    backupNowBtn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Backing up…';
    const {resp, data} = await post('/admin/backups');
    backupNowBtn.disabled = false;
    backupNowBtn.innerHTML = '<i class="bi bi-archive me-1"></i>Back up now';
    if (resp.ok && data.status === 'ok') {
      renderBackups(data);
      showAlert('success', `<i class="bi bi-check-circle me-1"></i>Backed up: <code>${nrEscape(data.created)}</code>.`);
    } else {
      showAlert('error', 'Not backed up: ' + nrEscape(data.message || `server error (${resp.status})`));
    }
  });

  document.getElementById('backupList').addEventListener('click', async e => {
    const button = e.target.closest('[data-backup-delete]');
    if (!button) return;
    const name = button.dataset.backupDelete;
    if (!await nrConfirm({title: 'Delete this backup?', danger: true, badge: 'IRREVERSIBLE ACTION',
                          message: `${name}\nThis can't be undone.`, confirmLabel: 'Delete'})) return;
    clearAlert();
    const {resp, data} = await post(`/admin/backups/${encodeURIComponent(name)}/delete`);
    if (resp.ok && data.status === 'ok') {
      renderBackups(data);
      showAlert('success', `<i class="bi bi-trash3 me-1"></i>Deleted <code>${nrEscape(name)}</code>.`);
    } else {
      showAlert('error', 'Not deleted: ' + nrEscape(data.message || `server error (${resp.status})`));
    }
  });

  input('backup_schedule').addEventListener('change', syncBackupRows);
  window.nrSyncBackups = saved => { syncBackupRows(); if (saved) refreshBackups(); };
  syncBackupRows();
  renderBackups(nrData('nr-backups'));

  refreshDirty();
})();
