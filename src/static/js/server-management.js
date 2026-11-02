// Server Management - see templates/server_management.html.
  const LDAP_BASE = '/admin/server/ldap';
  // the Redis switch's URLs: data-redis-test, data-redis-save, data-redis-back
  const REDIS_URLS = document.currentScript.dataset;

  /* ── TLS certificate (Access.upload_certificate / generate_selfsigned) ── */
  (() => {
    const status = document.getElementById('certStatus');
    const result = document.getElementById('certResult');
    const form = document.getElementById('certForm');
    const uploadBtn = document.getElementById('certUploadBtn');
    const selfBtn = document.getElementById('certSelfBtn');
    let access = nrData('nr-access');
    nrAccessStatus(status, access);

    const show = (kind, html) => { result.className = 'cert-result show ' + kind; result.innerHTML = html; };
    // what nginx did with it - the reply of both buttons
    const NGINX = {
      applied: 'nginx applied it.',
      not_managed: 'No NetRollout nginx reports here - with your own reverse proxy, give it the new files too.',
      no_answer: "nginx didn't confirm within a few seconds - check <code>netrollout status</code> on the server.",
    };

    async function send(url, body, label) {
      uploadBtn.disabled = selfBtn.disabled = true;
      show('warn', '<i class="bi bi-hourglass-split me-1"></i>Applying…');
      let resp, data = {};
      try {
        resp = await fetch(url, {method: 'POST', headers: {'X-CSRFToken': CSRF_TOKEN}, body});
        try { data = await resp.json(); } catch (_) {}
      } catch (_) { resp = {ok: false, status: 0}; }
      uploadBtn.disabled = selfBtn.disabled = false;
      if (resp.ok && data.status === 'ok') {
        access = data.access; nrAccessStatus(status, access);
        const state = data.proxy && data.proxy.state;
        show(state === 'no_answer' ? 'warn' : 'success',
          `<i class="bi bi-check-circle me-1"></i>${label} ${NGINX[state] || ''}`);
        form.reset();
      } else {
        show('error', '<i class="bi bi-x-circle me-1"></i>' +
          nrEscape(data.message || `Server error (${resp.status || 'no answer'}).`));
      }
    }

    form.addEventListener('submit', e => {
      e.preventDefault();
      const cert = form.elements.certificate.files[0], key = form.elements.key.files[0];
      if (!cert || !key) { show('error', '<i class="bi bi-x-circle me-1"></i>Choose both files: the certificate and its private key.'); return; }
      const body = new FormData();
      body.append('certificate', cert); body.append('key', key);
      send('/admin/server/certificate', body, 'Certificate in use.');
    });

    selfBtn.addEventListener('click', async () => {
      const ours = access.certificate && access.certificate.selfsigned;
      const yes = await (ours || !access.certificate
        ? nrConfirm({title: 'Generate a new self-signed certificate?', confirmLabel: 'Generate',
                     message: 'Browsers warn about it until each one trusts it.'})
        : nrConfirm({title: "Replace your organisation's certificate?", danger: true,
                     confirmLabel: 'Replace with self-signed',
                     message: 'It is replaced by a self-signed one: browsers will warn about it.'}));
      if (yes) send('/admin/server/certificate/selfsigned', null, 'New self-signed certificate in use.');
    });
  })();

  /* ── Trusted CA certificates (TrustedCAs: LDAPS) ── */
  (() => {
    const form = document.getElementById('caForm');
    const addBtn = document.getElementById('caAddBtn');
    const result = document.getElementById('caResult');
    const table = document.getElementById('caTable');
    const rows = document.getElementById('caRows');
    const none = document.getElementById('caNone');
    const show = (kind, html) => { result.className = 'cert-result show ' + kind; result.innerHTML = html; };

    function render(trusted) {
      rows.replaceChildren();
      trusted.forEach(ca => {
        const tr = document.createElement('tr');
        const subject = document.createElement('td');
        subject.textContent = ca.subject;
        if (ca.self_signed) {
          const note = document.createElement('div');
          note.className = 'cert-hint';
          note.textContent = 'self-signed';
          subject.appendChild(note);
        }
        const issuer = document.createElement('td');
        issuer.textContent = ca.self_signed ? '-' : ca.issuer;
        const expires = document.createElement('td');
        expires.textContent = ca.not_after.slice(0, 10);
        if (ca.warning) {
          const warn = document.createElement('div');
          warn.className = 'ca-warning';
          warn.textContent = ca.warning;
          expires.appendChild(warn);
        }
        const action = document.createElement('td');
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'btn btn-outline-danger btn-sm';
        btn.textContent = 'Remove';
        btn.dataset.fingerprint = ca.fingerprint;
        btn.dataset.subject = ca.subject;
        btn.addEventListener('click', () => remove(btn.dataset.fingerprint, btn.dataset.subject));
        action.appendChild(btn);
        tr.append(subject, issuer, expires, action);
        rows.appendChild(tr);
      });
      table.style.display = trusted.length ? '' : 'none';
      none.style.display = trusted.length ? 'none' : '';
    }

    async function post(url, body) {
      addBtn.disabled = true;
      let resp, data = {};
      try {
        resp = await fetch(url, {method: 'POST', headers: {'X-CSRFToken': CSRF_TOKEN}, body});
        try { data = await resp.json(); } catch (_) {}
      } catch (_) { resp = {ok: false, status: 0}; }
      addBtn.disabled = false;
      if (resp.ok && data.status === 'ok') {
        render(data.trusted);
        show('success', '<i class="bi bi-check-circle me-1"></i>' + nrEscape(data.message));
        form.reset();
      } else {
        show('error', '<i class="bi bi-x-circle me-1"></i>' +
          nrEscape(data.message || `Server error (${resp.status || 'no answer'}).`));
      }
    }

    async function remove(fingerprint, subject) {
      if (await nrConfirm({title: 'Stop trusting this CA?', danger: true, confirmLabel: 'Stop trusting',
                           message: `${subject}\nDirectory servers whose certificates it vouches for can't be reached over LDAPS until it's added again.`})) {
        post(`/admin/server/trusted-ca/${encodeURIComponent(fingerprint)}/remove`, null);
      }
    }

    form.addEventListener('submit', e => {
      e.preventDefault();
      const file = form.elements.certificates.files[0];
      if (!file) { show('error', '<i class="bi bi-x-circle me-1"></i>Choose a PEM file with one or more CA certificates.'); return; }
      const body = new FormData();
      body.append('certificates', file);
      post('/admin/server/trusted-ca', body);
    });

    render(nrData('nr-trusted-cas'));
  })();

  /* ── Service selector ─────────────────────────────────────── */
  // svc null closes both; the open one's button closes it again
  function selectService(svc) {
    if (svc && document.getElementById(`form-${svc}`).style.display === 'block') svc = null;
    ['postgres', 'redis'].forEach(s => {
      document.getElementById(`svc-${s}`).classList.toggle('selected', s === svc);
      document.getElementById(`svc-${s}`).setAttribute('aria-expanded', s === svc ? 'true' : 'false');
      document.getElementById(`form-${s}`).style.display = s === svc ? 'block' : 'none';
    });
  }


  document.addEventListener('DOMContentLoaded', () => {
    ['rd_host','rd_port','rd_db','rd_password'].forEach(id => {
      document.getElementById(id)?.addEventListener('input', () => {
        rdTestPassed = false;
        document.getElementById('rdBtnSave').disabled = true;
        document.getElementById('rdTestResult').style.display = 'none';
      });
    });
  });

  /* ── PostgreSQL: move to another server / back ───────────── */
  (() => {
    const $ = id => document.getElementById(id);
    const val = id => $(id).value.trim();
    const BASE = '/admin/server/database';
    let checked = null;          // the last check's report, for the fields as they are

    async function call(url, body, background) {
      const headers = {'X-CSRFToken': CSRF_TOKEN};
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (background) headers['X-NR-Background'] = '1';
      let resp, data = {};
      try {
        resp = await fetch(url, {method: body === undefined ? 'GET' : 'POST', headers,
                                 body: body === undefined ? undefined : JSON.stringify(body)});
        try { data = await resp.json(); } catch (_) {}
      } catch (_) { return {status: 'error', message: 'No answer from NetRollout.'}; }
      if (!data.status) data = {status: 'error', message: `Server error (${resp.status}).`};
      return data;
    }
    const busy = (btn, on, label) => {
      btn.disabled = on;
      if (on) { btn.dataset.label = btn.innerHTML; btn.innerHTML = `<span class="spinner-border spinner-border-sm me-1"></span>${label}`; }
      else if (btn.dataset.label) btn.innerHTML = btn.dataset.label;
    };
    const error = msg => `<span class="res-err"><i class="bi bi-x-circle me-1"></i>${nrEscape(msg)}</span>`;

    // Step 1: the two ways
    const ways = {dba: [$('mvWayDba'), $('mvDba')], admin: [$('mvWayAdmin'), $('mvAdmin')]};
    Object.entries(ways).forEach(([name, [btn]]) => btn.addEventListener('click', () => {
      Object.entries(ways).forEach(([other, [b, panel]]) => {
        b.setAttribute('aria-pressed', String(other === name));
        panel.style.display = other === name ? 'block' : 'none';
      });
    }));

    function fillStep2(d, host, port) {
      $('mvDb').value = d.database; $('mvSchema').value = d.schema;
      $('mvUser').value = d.login; $('mvPassword').value = d.password;
      if (host) $('mvHost').value = host;
      if (port) $('mvPort').value = port;
      forget();
    }

    // Show / hide the SQL. It's written once and kept (its password may have
    // been sent already); written again only for other names.
    const SHOW_SQL = '<i class="bi bi-file-earmark-code me-1"></i>Show the SQL for your administrator';
    const HIDE_SQL = '<i class="bi bi-eye-slash me-1"></i>Hide the SQL';
    let sqlFor = null;
    const sqlNames = () => [val('mvSqlDb'), val('mvSqlSchema'), val('mvSqlLogin')].join('\u0000');
    function showSql(on) {
      $('mvSqlOut').style.display = on ? 'block' : 'none';
      $('mvSqlBtn').innerHTML = on ? HIDE_SQL : SHOW_SQL;
      $('mvSqlBtn').setAttribute('aria-expanded', String(on));
    }
    ['mvSqlDb', 'mvSqlSchema', 'mvSqlLogin'].forEach(id => $(id).addEventListener('input', () => {
      if (sqlFor !== null && sqlNames() !== sqlFor) showSql(false);     // it no longer matches
    }));
    $('mvSqlBtn').addEventListener('click', async () => {
      const btn = $('mvSqlBtn');
      if ($('mvSqlOut').style.display !== 'none') { showSql(false); return; }
      if (sqlFor === sqlNames()) { showSql(true); return; }
      busy(btn, true, 'Writing…');
      const d = await call(`${BASE}/sql`, {database: val('mvSqlDb'), schema: val('mvSqlSchema'), login: val('mvSqlLogin')});
      busy(btn, false);
      if (d.status !== 'ok') { $('mvSqlText').textContent = ''; $('mvSqlHint').innerHTML = error(d.message); showSql(true); return; }
      sqlFor = sqlNames();
      $('mvSqlText').textContent = d.sql;
      $('mvSqlHint').innerHTML =
        'It holds a new password for NetRollout\'s login - step 2 below is filled in with it, so keep this ' +
        'page open until the move (or keep the password). Hand the SQL over securely. Hiding and showing it keeps ' +
        'the same password; other names above write it again, with a new one.' +
        (d.grafana_known ? '' : '<br><i class="bi bi-info-circle me-1"></i>Grafana\'s password isn\'t known to this ' +
         'server, so its line is commented out: without it the dashboards stay empty after the move.');
      showSql(true);
      fillStep2(d);
    });
    $('mvSqlCopy').addEventListener('click', async () => {
      try { await navigator.clipboard.writeText($('mvSqlText').textContent); $('mvSqlCopy').innerHTML = '<i class="bi bi-check2 me-1"></i>Copied'; }
      catch (_) { $('mvSqlCopy').innerHTML = 'Select and copy'; }
    });

    $('mvPrepBtn').addEventListener('click', async () => {
      const btn = $('mvPrepBtn'), out = $('mvPrepOut');
      busy(btn, true, 'Creating…');
      const body = {host: val('mvAdHost'), port: val('mvAdPort'), admin_user: val('mvAdUser'),
                    admin_password: $('mvAdPassword').value, database: val('mvAdDb'),
                    schema: val('mvAdSchema'), login: val('mvAdLogin')};
      const d = await call(`${BASE}/prepare`, body);
      busy(btn, false);
      out.style.display = 'block';
      if (d.status !== 'ok') { out.innerHTML = error(d.message); return; }
      $('mvAdPassword').value = '';            // not kept, not even in the page
      out.innerHTML = '<span class="res-ok"><i class="bi bi-check-circle me-1"></i>Ready: ' +
        d.done.map(nrEscape).join('; ') + '. Step 2 is filled in - check it.</span>' +
        (d.grafana_known ? '' : '<div class="mv-hint">Grafana\'s password isn\'t known to this server: its login wasn\'t created.</div>');
      fillStep2(d, body.host, body.port);
    });

    // Step 2: check
    const target = () => ({host: val('mvHost'), port: val('mvPort'), database: val('mvDb'),
                           schema: val('mvSchema'), user: val('mvUser'), password: $('mvPassword').value});
    function forget() {
      checked = null; $('mvMoveBtn').disabled = true;
      $('mvReport').style.display = 'none'; $('mvReplaceRow').style.display = 'none'; $('mvReplace').checked = false;
    }
    ['mvHost', 'mvPort', 'mvDb', 'mvSchema', 'mvUser', 'mvPassword'].forEach(id => $(id).addEventListener('input', forget));

    $('mvCheckBtn').addEventListener('click', async () => {
      const btn = $('mvCheckBtn'), out = $('mvReport');
      busy(btn, true, 'Checking…');
      const d = await call(`${BASE}/check`, target());
      busy(btn, false);
      out.style.display = 'block';
      if (d.status !== 'ok') { forget(); out.style.display = 'block'; out.innerHTML = error(d.message); return; }
      const r = d.report;
      const list = (items, cls) => items.length ? `<ul class="${cls}">${items.map(i => `<li>${nrEscape(i)}</li>`).join('')}</ul>` : '';
      out.innerHTML = (r.ok
          ? `<span class="res-ok"><i class="bi bi-check-circle me-1"></i>Ready for the move - PostgreSQL ${nrEscape(r.server_version)}, schema ${nrEscape(r.schema)}, ${r.contents === 'empty' ? 'empty' : 'holding a NetRollout database'}.</span>`
          : `<span class="res-err"><i class="bi bi-x-circle me-1"></i>NetRollout can't move there yet:</span>${list(r.problems, 'res-err')}`)
        + list(r.notes, 'mv-hint');
      checked = r.ok ? r : null;
      $('mvReplaceRow').style.display = r.ok && r.contents === 'netrollout' ? 'block' : 'none';
      updateMove();
    });
    const updateMove = () => {
      $('mvMoveBtn').disabled = !checked || (checked.contents === 'netrollout' && !$('mvReplace').checked);
    };
    $('mvReplace').addEventListener('change', updateMove);

    // Step 3 / Move back: start, then follow
    async function start(url, body, question) {
      if (!await nrConfirm({title: question.title, message: question.message,
                            confirmLabel: question.label, danger: true})) return;
      const d = await call(url, body);
      if (d.status !== 'ok') { showProgress({state: 'failed', message: d.message}); return; }
      follow(d.move);
    }
    $('mvMoveBtn').addEventListener('click', () => start(`${BASE}/move`, {...target(), replace: $('mvReplace').checked},
      {title: "Move NetRollout's data now?", label: 'Move now',
       message: 'New rollouts pause and running ones finish first; then NetRollout is ' +
                'unavailable for a few minutes while the data is copied. Everyone stays signed in.'}));
    $('mvBackBtn')?.addEventListener('click', () => start(`${BASE}/move-back`, {},
      {title: 'Move back to the bundled database?', label: 'Move back',
       message: 'Its current contents are replaced by the data on your server. ' +
                'New rollouts pause and running ones finish first; then NetRollout is unavailable for a few minutes.'}));

    const mmss = s => `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    // live: this page watched the move end (a result from before is only shown)
    function showProgress(m, live) {
      const box = $('mvProgress');
      box.style.display = 'block';
      box.className = 'mv-progress' + (m.state === 'done' ? ' done' : ['failed', 'cancelled'].includes(m.state) ? ' failed' : '');
      const moving = ['waiting', 'copying', 'switching'].includes(m.state);
      $('mvWizard').style.display = moving ? 'none' : 'block';
      if ($('mvBack')) $('mvBack').style.display = moving ? 'none' : 'block';
      let html = '';
      if (m.state === 'waiting') {
        const n = (m.rollouts || []).length, what = m.what || '';
        html = `<strong><span class="spinner-border spinner-border-sm text-info me-2"></span>${nrEscape(what.charAt(0).toUpperCase() + what.slice(1))}:
          waiting for ${n === 1 ? 'a rollout' : n ? `${n} rollouts` : 'the last rollout'} to finish</strong>
          <div class="mv-hint mt-1">New rollouts are paused. The move starts by itself when none is left; it's given up in
          ${mmss(m.seconds_left || 0)} if they're still running. Cancelling a rollout skips devices it hasn't reached;
          a device being configured finishes first.</div>
          <div id="mvRollouts"></div>
          <button class="btn btn-outline-secondary btn-sm mt-1" id="mvCancelMove">Cancel the move</button>`;
      } else if (m.state === 'copying' || m.state === 'switching') {
        html = `<strong><span class="spinner-border spinner-border-sm text-info me-2"></span>${nrEscape(m.step || 'Working')}…</strong>
          <div class="mv-hint mt-1">NetRollout is in maintenance for everyone else. ${nrEscape(m.source)} → ${nrEscape(m.target)}.
          Keep this page open; it updates by itself.</div>`;
      } else if (m.state === 'done') {
        html = `<strong class="res-ok"><i class="bi bi-check-circle me-1"></i>${nrEscape(m.message)}</strong>
          <div class="mv-hint mt-1">Backup made before the move: ${nrEscape(m.backup || '-')}.${live ? ' This page reloads in a moment.' : ''}</div>`;
        if (live) setTimeout(() => location.reload(), 4000);
      } else {
        html = `<strong class="res-err"><i class="bi bi-x-circle me-1"></i>${m.state === 'cancelled' ? 'The move was cancelled' : 'The move didn\'t happen'}</strong>
          <div class="mv-hint mt-1">${nrEscape(m.message || '')}</div>`;
      }
      box.innerHTML = html;
      nrRolloutsTable($('mvRollouts'), m.rollouts);
      $('mvCancelMove')?.addEventListener('click', async () => {
        $('mvCancelMove').disabled = true;
        await call(`${BASE}/move/cancel`, {});
      });
    }

    let timer = null;
    function follow(m, live = true) {
      showProgress(m, live);
      clearTimeout(timer);
      if (!['waiting', 'copying', 'switching'].includes(m.state)) return;
      timer = setTimeout(async () => {
        const d = await call(`${BASE}/move/status`, undefined, true);
        follow(d.status === 'ok' ? d.move : m);      // no answer for a moment: ask again
      }, 2000);
    }

    // a move under way (or just ended) when the page opens
    const initial = nrData('nr-db-move');
    if (initial.state !== 'idle') { selectService('postgres'); follow(initial, false); }
  })();

  /* ── Redis ────────────────────────────────────────────────── */
  let rdTestPassed = false;

  function testRedis() {
    const btn = document.getElementById('rdBtnTest');
    const res = document.getElementById('rdTestResult');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1"></span>Testing…';
    res.style.display = 'none';

    fetch(REDIS_URLS.redisTest, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
      body: JSON.stringify({
        host:     document.getElementById('rd_host').value.trim(),
        port:     document.getElementById('rd_port').value.trim(),
        db:       document.getElementById('rd_db').value.trim(),
        password: document.getElementById('rd_password').value.trim(),
      })
    })
    .then(r => r.json())
    .then(data => {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-plug me-1"></i>Test Connection';
      res.style.display = 'block';
      if (data.status === 'ok') {
        rdTestPassed = true;
        document.getElementById('rdBtnSave').disabled = false;
        res.innerHTML = '<span class="res-ok"><i class="bi bi-check-circle me-1"></i>Connection successful</span>';
      } else {
        res.innerHTML = `<span class="res-err"><i class="bi bi-x-circle me-1"></i>${nrEscape(data.message)}</span>`;
      }
    })
    .catch(() => {
      btn.disabled = false;
      btn.innerHTML = '<i class="bi bi-plug me-1"></i>Test Connection';
      res.style.display = 'block';
      res.innerHTML = '<span class="res-err"><i class="bi bi-x-circle me-1"></i>Request failed</span>';
    });
  }

  function saveRedis() {
    if (!rdTestPassed) return;
    const btn = document.getElementById('rdBtnSave');
    const spinner = document.getElementById('rdSaveSpinner');
    btn.disabled = true;
    spinner.style.display = 'inline-block';

    fetch(REDIS_URLS.redisSave, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
      body: JSON.stringify({
        host:     document.getElementById('rd_host').value.trim(),
        port:     document.getElementById('rd_port').value.trim(),
        db:       document.getElementById('rd_db').value.trim(),
        password: document.getElementById('rd_password').value.trim(),
      })
    })
    .then(r => r.json())
    .then(data => {
      spinner.style.display = 'none';
      if (data.status === 'ok') {
        redisSwitched(document.getElementById('rdRestartBanner'), data.message);
      } else {
        btn.disabled = false;
        document.getElementById('rdTestResult').style.display = 'block';
        document.getElementById('rdTestResult').innerHTML =
          `<span class="res-err"><i class="bi bi-x-circle me-1"></i>${nrEscape(data.message || 'Switch failed')}</span>`;
      }
    })
    .catch(() => { spinner.style.display = 'none'; btn.disabled = false; });
  }

  // Applied at once (no restart); this page reloads to show the new state
  function redisSwitched(box, message) {
    box.style.display = 'block';
    box.innerHTML = `<i class="bi bi-check-circle me-2"></i>${nrEscape(message)}`;
    setTimeout(() => location.reload(), 3000);
  }

  document.getElementById('rdBackBtn')?.addEventListener('click', async () => {
    if (!await nrConfirm({title: 'Switch back to the bundled Redis?', danger: true, confirmLabel: 'Switch back',
                          message: 'Everyone else is signed out.'})) return;
    const btn = document.getElementById('rdBackBtn'), out = document.getElementById('rdBackResult');
    btn.disabled = true;
    let data = {};
    try {
      const r = await fetch(REDIS_URLS.redisBack,
                            {method: 'POST', headers: {'X-CSRFToken': CSRF_TOKEN}});
      data = await r.json();
    } catch (_) { data = {status: 'error', message: 'No answer from NetRollout.'}; }
    if (data.status === 'ok') { out.className = 'nr-restart-banner mt-2'; redisSwitched(out, data.message); }
    else { btn.disabled = false; out.innerHTML = `<span class="res-err"><i class="bi bi-x-circle me-1"></i>${nrEscape(data.message)}</span>`; }
  });

  /* ── LDAP server management ───────────────────────────────── */
  let ldapServers = {};
  let expandedId  = null;
  let newBindSelected = 'simple';

  window.addEventListener('DOMContentLoaded', loadServers);

  function loadServers() {
    fetch(LDAP_BASE)
      .then(r => r.json())
      .then(data => {
        ldapServers = {};
        data.forEach(s => { ldapServers[s.id] = s; });
        renderList(data);
      })
      .catch(() => {
        document.getElementById('ldapServerList').innerHTML =
          '<div class="res-err" style="font-size:0.82rem; padding:0.5rem 0;">Failed to load servers</div>';
      });
  }

  function renderList(data) {
    const el = document.getElementById('ldapServerList');
    if (!data.length) {
      el.innerHTML = '<div class="ldap-empty">No LDAP servers configured</div>';
      return;
    }
    el.innerHTML = data.map(serverRowHtml).join('');
  }

  function serverRowHtml(s) {
    const dot = s.is_active
      ? '<span class="conn-dot connected"></span>'
      : '<span class="conn-dot disconnected"></span>';
    return `
      <div class="ldap-server-row" id="row-${s.id}" onclick="toggleExpand('${s.id}')"
           role="button" tabindex="0" data-nr-activate aria-expanded="false" aria-controls="panel-${s.id}">
        ${dot}
        <span class="ldap-server-name">${nrEscape(s.name)}</span>
        <span class="ldap-server-host">${nrEscape(s.host)}:${s.port}</span>
        <span class="ldap-bind-badge">${s.bind_type.toUpperCase()}</span>
        <span style="flex:1;"></span>
        <i class="bi bi-chevron-down ldap-chevron" id="chev-${s.id}" aria-hidden="true"></i>
      </div>
      <div id="panel-${s.id}" style="display:none;"></div>
    `;
  }

  function toggleExpand(id) {
    if (expandedId === id) {
      collapseServer(id);
      expandedId = null;
      return;
    }
    if (expandedId) collapseServer(expandedId);
    expandServer(id);
    expandedId = id;
  }

  function collapseServer(id) {
    document.getElementById(`panel-${id}`).style.display = 'none';
    document.getElementById(`row-${id}`).classList.remove('expanded');
    document.getElementById(`row-${id}`).setAttribute('aria-expanded', 'false');
    document.getElementById(`chev-${id}`).style.transform = '';
  }

  // An open card - PostgreSQL's or Redis' form, an LDAP server's panel -
  // closes on Escape or a click outside it, as a window does. Not while a
  // window is open over the page (Escape and clicks are its own), nor while
  // a database move runs (its progress is in PostgreSQL's card).
  function closeOpenCards(e, escape) {
    // from a window (Bootstrap closes it on Escape before this runs)
    if (document.querySelector('.modal.show') || e.target.closest('.modal')) return;
    const outside = sel => escape || !e.target.closest(sel);
    const moving = document.getElementById('mvProgress').style.display !== 'none';
    ['postgres', 'redis'].forEach(s => {
      if (document.getElementById(`form-${s}`).style.display === 'block'
          && !(s === 'postgres' && moving) && outside(`#form-${s}, #svc-${s}`)) selectService(null);
    });
    if (expandedId && outside(`#row-${expandedId}, #panel-${expandedId}`)) {
      collapseServer(expandedId);
      expandedId = null;
    }
  }
  document.addEventListener('click', e => {
    // a click that re-drew its own part of a card has left the page: not outside
    if (document.contains(e.target) && e.target.closest) closeOpenCards(e, false);
  });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && e.target.closest) closeOpenCards(e, true);
  });

  function expandServer(id) {
    const s = ldapServers[id];
    const panel = document.getElementById(`panel-${id}`);
    panel.innerHTML = editPanelHtml(s);
    panel.style.display = 'block';
    document.getElementById(`row-${id}`).classList.add('expanded');
    document.getElementById(`row-${id}`).setAttribute('aria-expanded', 'true');
    document.getElementById(`chev-${id}`).style.transform = 'rotate(180deg)';
  }

  function editPanelHtml(s) {
    const isReg = s.bind_type === 'regular';
    const bfDisp = isReg ? 'block' : 'none';
    const expDisabled = isReg ? '' : 'disabled title="Requires Regular bind type"';
    return `
      <div class="ldap-edit-panel">
        <div class="row g-3">
          <div class="col-md-4">
            <label class="nr-label">Name</label>
            <input id="e_${s.id}_name" class="form-control form-control-sm" value="${nrEscape(s.name)}">
          </div>
          <div class="col-md-4">
            <label class="nr-label">Host</label>
            <input id="e_${s.id}_host" class="form-control form-control-sm" value="${nrEscape(s.host)}">
          </div>
          <div class="col-md-2">
            <label class="nr-label">Port</label>
            <input id="e_${s.id}_port" class="form-control form-control-sm" type="number" value="${s.port}">
          </div>
          <div class="col-md-2 d-flex align-items-end pb-1">
            <div class="form-check form-switch mb-0">
              <input class="form-check-input" type="checkbox" id="e_${s.id}_ssl"
                     ${s.use_ssl ? 'checked' : ''} onchange="ldapsToggle('${s.id}')">
              <label class="form-check-label nr-label mb-0" for="e_${s.id}_ssl">LDAPS</label>
            </div>
          </div>
          <div class="col-md-8">
            <label class="nr-label">Base DN</label>
            <div class="input-group input-group-sm">
              <input id="e_${s.id}_dn" class="form-control" value="${nrEscape(s.base_dn)}">
              <button class="btn btn-outline-secondary" type="button" onclick="fetchDn('${s.id}')">
                <span id="e_${s.id}_fetch_spin" style="display:none;"
                      class="spinner-border spinner-border-sm"></span>
                <i class="bi bi-search" id="e_${s.id}_fetch_ico"></i> Fetch
              </button>
            </div>
          </div>
          <div class="col-md-4">
            <label class="nr-label">CN Identifier</label>
            <input id="e_${s.id}_cn" class="form-control form-control-sm"
                   value="${nrEscape(s.cn_identifier)}" placeholder="sAMAccountName">
          </div>
          <div class="col-12">
            <label class="nr-label d-block mb-2">Bind Type</label>
            <div class="d-flex gap-2" role="radiogroup" aria-label="Bind type">
              <div class="ldap-bind-option ${!isReg ? 'selected' : ''}"
                   onclick="setBindType('${s.id}', 'simple')" id="e_${s.id}_opt_simple"
                   role="radio" aria-checked="${!isReg}" tabindex="0" data-nr-activate>
                <div style="font-weight:600; font-size:0.84rem; color:#ccc;">Simple</div>
                <div style="font-size: 0.75rem; color:var(--nr-text-muted); margin-top:0.15rem;">No stored service credentials</div>
              </div>
              <div class="ldap-bind-option ${isReg ? 'selected' : ''}"
                   onclick="setBindType('${s.id}', 'regular')" id="e_${s.id}_opt_regular"
                   role="radio" aria-checked="${isReg}" tabindex="0" data-nr-activate>
                <div style="font-weight:600; font-size:0.84rem; color:#ccc;">Regular</div>
                <div style="font-size: 0.75rem; color:var(--nr-text-muted); margin-top:0.15rem;">Service account - enables Explorer + group rules</div>
              </div>
            </div>
          </div>
          <div id="e_${s.id}_bind_fields" class="col-12" style="display:${bfDisp};">
            <div class="row g-3">
              <div class="col-md-6">
                <label class="nr-label" for="e_${s.id}_bdn">Bind DN <span class="nr-label-hint">(service account)</span></label>
                <input id="e_${s.id}_bdn" class="form-control form-control-sm"
                       value="${nrEscape(s.bind_dn || '')}" placeholder="CN=svc-ldap,DC=corp,DC=com">
                <div class="nr-help-text">A full DN. Active Directory also takes <code>svc@corp.local</code> or <code>CORP\\svc</code>, never the bare name.</div>
              </div>
              <div class="col-md-6">
                <label class="nr-label">
                  Bind Password
                  <span style="color:var(--nr-text-muted); font-size: 0.75rem; text-transform:none; letter-spacing:0;">
                    - leave blank to keep current
                  </span>
                </label>
                <input id="e_${s.id}_bpw" class="form-control form-control-sm"
                       type="password" placeholder="••••••••">
              </div>
            </div>
          </div>
          <div class="col-12">
            <div class="form-check form-switch mb-0">
              <input class="form-check-input" type="checkbox" id="e_${s.id}_active"
                     ${s.is_active ? 'checked' : ''}>
              <label class="form-check-label nr-label mb-0" for="e_${s.id}_active">Active</label>
            </div>
          </div>
        </div>

        <div class="d-flex gap-2 mt-4 flex-wrap align-items-center">
          <button class="btn btn-outline-primary btn-sm" onclick="testConn('${s.id}')">
            <i class="bi bi-plug me-1"></i>Test
          </button>
          <button class="btn btn-outline-secondary btn-sm" onclick="toggleTestUser('${s.id}')">
            <i class="bi bi-person-check me-1"></i>Test User
          </button>
          <button class="btn btn-sm" style="color:#80deea; border:1px solid rgba(0,188,212,0.25);
                  background:rgba(0,188,212,0.05);" onclick="openExplorer('${s.id}')"
                  id="e_${s.id}_exp_btn" ${expDisabled}>
            <i class="bi bi-diagram-3 me-1"></i>Explorer
          </button>
          <span style="flex:1;"></span>
          <button class="btn btn-outline-danger btn-sm" onclick="deleteServer('${s.id}')">
            <i class="bi bi-trash3 me-1"></i>Delete
          </button>
          <button class="btn btn-primary btn-sm" onclick="saveServer('${s.id}')">
            <i class="bi bi-floppy me-1"></i>Save
          </button>
        </div>

        <div id="e_${s.id}_result" style="display:none; margin-top:0.6rem;"></div>

        <div id="e_${s.id}_tu" class="ldap-test-user-panel mt-3" style="display:none;">
          <div class="row g-2 align-items-end">
            <div class="col-md-4">
              <label class="nr-label">Username</label>
              <input id="e_${s.id}_tuu" class="form-control form-control-sm">
            </div>
            <div class="col-md-4">
              <label class="nr-label">Password</label>
              <input id="e_${s.id}_tup" class="form-control form-control-sm" type="password">
            </div>
            <div class="col-md-4">
              <button class="btn btn-outline-secondary btn-sm w-100"
                      onclick="runTestUser('${s.id}')">Test</button>
            </div>
          </div>
          <div id="e_${s.id}_tur" style="display:none; margin-top:0.5rem;"></div>
        </div>
      </div>
    `;
  }

  /* ── Bind type + LDAPS helpers ── */
  function setBindType(id, type) {
    const isReg = type === 'regular';
    document.getElementById(`e_${id}_opt_simple`).classList.toggle('selected', !isReg);
    document.getElementById(`e_${id}_opt_regular`).classList.toggle('selected', isReg);
    document.getElementById(`e_${id}_opt_simple`).setAttribute('aria-checked', !isReg ? 'true' : 'false');
    document.getElementById(`e_${id}_opt_regular`).setAttribute('aria-checked', isReg ? 'true' : 'false');
    document.getElementById(`e_${id}_bind_fields`).style.display = isReg ? 'block' : 'none';
    const expBtn = document.getElementById(`e_${id}_exp_btn`);
    if (expBtn) expBtn.disabled = !isReg;
  }

  function ldapsToggle(id) {
    const ssl = document.getElementById(`e_${id}_ssl`).checked;
    const portEl = document.getElementById(`e_${id}_port`);
    const cur = parseInt(portEl.value);
    if (ssl && cur === 389) portEl.value = 636;
    if (!ssl && cur === 636) portEl.value = 389;
  }

  function newBindType(type) {
    newBindSelected = type;
    document.getElementById('n_opt_simple').classList.toggle('selected', type === 'simple');
    document.getElementById('n_opt_regular').classList.toggle('selected', type === 'regular');
    document.getElementById('n_opt_simple').setAttribute('aria-checked', type === 'simple' ? 'true' : 'false');
    document.getElementById('n_opt_regular').setAttribute('aria-checked', type === 'regular' ? 'true' : 'false');
    document.getElementById('n_bind_fields').style.display = type === 'regular' ? 'block' : 'none';
  }

  function newLdapsToggle() {
    const ssl = document.getElementById('n_ssl').checked;
    const portEl = document.getElementById('n_port');
    const cur = parseInt(portEl.value);
    if (ssl && cur === 389) portEl.value = 636;
    if (!ssl && cur === 636) portEl.value = 389;
  }

  /* ── New server form ── */
  function toggleNewForm() {
    const panel = document.getElementById('newServerPanel');
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  }

  function saveNewServer() {
    const body = new URLSearchParams({
      label:       document.getElementById('n_name').value.trim(),
      ip:          document.getElementById('n_host').value.trim(),
      port:        document.getElementById('n_port').value.trim(),
      base_dn:     document.getElementById('n_dn').value.trim(),
      cn_identifier: document.getElementById('n_cn').value.trim(),
      bind_type:   newBindSelected,
      bind_dn:     document.getElementById('n_bdn').value.trim(),
      bind_password: document.getElementById('n_bpw').value,
      use_ssl:     document.getElementById('n_ssl').checked ? 'true' : 'false',
      is_active:   document.getElementById('n_active').checked ? 'true' : 'false',
    });
    const res = document.getElementById('n_result');
    res.style.display = 'none';

    fetch(`${LDAP_BASE}/new`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': CSRF_TOKEN },
      body
    })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'ok') {
        document.getElementById('newServerPanel').style.display = 'none';
        loadServers();
      } else {
        res.style.display = 'block';
        res.innerHTML = `<span class="res-err"><i class="bi bi-x-circle me-1"></i>${nrEscape(data.message)}</span>`;
      }
    });
  }

  /* ── Save existing server ── */
  function saveServer(id) {
    const body = new URLSearchParams({
      label:        document.getElementById(`e_${id}_name`).value.trim(),
      ip:           document.getElementById(`e_${id}_host`).value.trim(),
      port:         document.getElementById(`e_${id}_port`).value.trim(),
      base_dn:      document.getElementById(`e_${id}_dn`).value.trim(),
      cn_identifier:document.getElementById(`e_${id}_cn`).value.trim(),
      bind_type:    document.getElementById(`e_${id}_opt_regular`).classList.contains('selected')
                      ? 'regular' : 'simple',
      bind_dn:      document.getElementById(`e_${id}_bdn`) ? document.getElementById(`e_${id}_bdn`).value.trim() : '',
      bind_password:document.getElementById(`e_${id}_bpw`) ? document.getElementById(`e_${id}_bpw`).value : '',
      use_ssl:      document.getElementById(`e_${id}_ssl`).checked ? 'true' : 'false',
      is_active:    document.getElementById(`e_${id}_active`).checked ? 'true' : 'false',
    });
    const res = document.getElementById(`e_${id}_result`);
    res.style.display = 'none';

    fetch(`${LDAP_BASE}/${id}/save`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': CSRF_TOKEN },
      body
    })
    .then(r => r.json())
    .then(data => {
      res.style.display = 'block';
      if (data.status === 'ok') {
        res.innerHTML = '<span class="res-ok"><i class="bi bi-check-circle me-1"></i>Saved</span>';
        loadServers();
      } else {
        res.innerHTML = `<span class="res-err"><i class="bi bi-x-circle me-1"></i>${nrEscape(data.message)}</span>`;
      }
    });
  }

  /* ── Delete server ── */
  // Deleting a server: its users listed first, and whether they go too
  let deletingServerId = null;
  function deleteServer(id) {
    const name = ldapServers[id]?.name || 'this server';
    fetch(`${LDAP_BASE}/${id}/users`, { headers: { 'X-NR-Background': '1' } })
    .then(r => r.json())
    .then(data => {
      if (data.status !== 'ok') {
        const res = document.getElementById(`e_${id}_result`);
        if (res) { res.style.display = 'block'; res.innerHTML = `<span class="res-err">${nrEscape(data.message)}</span>`; }
        return;
      }
      deletingServerId = id;
      document.getElementById('ldapDeleteTitle').textContent = `Delete "${name}"?`;
      const list = document.getElementById('ldapDeleteUsers');
      list.replaceChildren(...data.users.map(u => {
        const li = document.createElement('li');
        li.textContent = u.username + (u.role === 'admin' ? ' (admin)' : '') + (u.is_active ? '' : ' - disabled');
        return li;
      }));
      document.getElementById('ldapDeleteCount').textContent = data.users.length;
      document.getElementById('ldapDeleteUsersBox').style.display = data.users.length ? '' : 'none';
      document.getElementById('ldapDeleteNoUsers').style.display = data.users.length ? 'none' : '';
      document.getElementById('ldapDeleteUsersCheck').checked = false;
      updateDeleteHint();
      document.getElementById('ldapDeleteError').style.display = 'none';
      document.getElementById('ldapDeleteBtn').disabled = false;
      bootstrap.Modal.getOrCreateInstance(document.getElementById('ldapDeleteModal')).show();
    });
  }

  function updateDeleteHint() {
    document.getElementById('ldapDeleteHint').textContent =
      document.getElementById('ldapDeleteUsersCheck').checked
        ? 'Ticked: deleted with their own devices, profiles, mappings and results. Global devices they added stay.'
        : "Unticked: they're kept but disabled, with their devices, profiles and results.";
  }
  document.getElementById('ldapDeleteUsersCheck').addEventListener('change', updateDeleteHint);

  function confirmDeleteServer() {
    const id = deletingServerId;
    const btn = document.getElementById('ldapDeleteBtn');
    btn.disabled = true;
    fetch(`${LDAP_BASE}/${id}/delete`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
      body: JSON.stringify({ delete_users: document.getElementById('ldapDeleteUsersCheck').checked })
    })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'ok') {
        bootstrap.Modal.getInstance(document.getElementById('ldapDeleteModal')).hide();
        delete ldapServers[id];
        if (expandedId === id) expandedId = null;
        loadServers();
      } else {
        const box = document.getElementById('ldapDeleteError');
        box.textContent = data.message || 'The server was not deleted.';
        box.style.display = 'block';
        btn.disabled = false;
      }
    });
  }

  /* ── Test connection ── */
  function testConn(id) {
    const res = document.getElementById(`e_${id}_result`);
    res.style.display = 'block';
    res.innerHTML = '<span class="spinner-border spinner-border-sm text-secondary me-1"></span><span style="color:var(--nr-text-muted); font-size:0.82rem;">Testing…</span>';

    fetch(`${LDAP_BASE}/${id}/test`, {
      method: 'POST',
      headers: { 'X-CSRFToken': CSRF_TOKEN }
    })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'ok') {
        res.innerHTML = `<span class="res-ok"><i class="bi bi-check-circle me-1"></i>${nrEscape(data.message)}</span>`;
      } else {
        res.innerHTML = `<span class="res-err"><i class="bi bi-x-circle me-1"></i>${nrEscape(data.message)}</span>`;
      }
    });
  }

  /* ── Test user ── */
  function toggleTestUser(id) {
    const panel = document.getElementById(`e_${id}_tu`);
    panel.style.display = panel.style.display === 'none' ? 'block' : 'none';
  }

  function runTestUser(id) {
    const user = document.getElementById(`e_${id}_tuu`).value.trim();
    const pass = document.getElementById(`e_${id}_tup`).value;
    const res  = document.getElementById(`e_${id}_tur`);
    res.style.display = 'block';
    res.innerHTML = '<span class="spinner-border spinner-border-sm text-secondary me-1"></span><span style="color:var(--nr-text-muted); font-size:0.8rem;">Testing…</span>';

    const body = new URLSearchParams({ username: user, password: pass });
    fetch(`${LDAP_BASE}/${id}/test_user`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': CSRF_TOKEN },
      body
    })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'ok') {
        res.innerHTML = `<span class="res-ok"><i class="bi bi-check-circle me-1"></i>${nrEscape(data.message)}</span>`;
      } else {
        res.innerHTML = `<span class="res-err"><i class="bi bi-x-circle me-1"></i>${nrEscape(data.message)}</span>`;
      }
    });
  }

  /* ── Fetch DN ── */
  function fetchDn(id) {
    const spin = document.getElementById(`e_${id}_fetch_spin`);
    const ico  = document.getElementById(`e_${id}_fetch_ico`);
    spin.style.display = 'inline-block';
    ico.style.display  = 'none';

    fetch(`${LDAP_BASE}/${id}/fetch_dn`, {
      method: 'POST',
      headers: { 'X-CSRFToken': CSRF_TOKEN }
    })
    .then(r => r.json())
    .then(data => {
      spin.style.display = 'none';
      ico.style.display  = 'inline-block';
      const res = document.getElementById(`e_${id}_result`);
      res.style.display = 'block';
      if (data.status === 'ok') {
        document.getElementById(`e_${id}_dn`).value = data.base_dn;
        res.innerHTML = `<span class="res-ok"><i class="bi bi-check-circle me-1"></i>Base DN populated</span>`;
      } else {
        res.innerHTML = `<span class="res-err"><i class="bi bi-x-circle me-1"></i>${nrEscape(data.message)}</span>`;
      }
    });
  }

  /* ── Explorer ─────────────────────────────────────────────── */
  let explorerServerId = null;
  let explorerStack    = [];
  let explorerSelected = {};

  function openExplorer(id) {
    explorerServerId = id;
    explorerStack    = [{ dn: null, label: 'Root' }];
    explorerSelected = {};
    document.getElementById('explorerTitle').textContent = ldapServers[id]?.name || 'Directory Browser';
    document.getElementById('expImportResult').style.display = 'none';
    renderSelectedPanel();
    loadExplorerLevel(null);
    new bootstrap.Modal(document.getElementById('explorerModal')).show();
  }

  function loadExplorerLevel(dn) {
    const tree = document.getElementById('explorerTree');
    tree.innerHTML = '<div class="exp-tree-item loading"><span class="spinner-border spinner-border-sm text-secondary me-2"></span>Loading…</div>';
    renderBreadcrumb();

    const body = new URLSearchParams();
    if (dn) body.append('dn', dn);

    fetch(`${LDAP_BASE}/${explorerServerId}/explore`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', 'X-CSRFToken': CSRF_TOKEN },
      body
    })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'error') {
        tree.innerHTML = `<div class="exp-tree-item" style="color:#ef9a9a; font-size:0.82rem; padding:1rem;">
          <i class="bi bi-x-circle me-2"></i>${nrEscape(data.message)}</div>`;
        return;
      }
      renderTree(data.entries || []);
    })
    .catch(() => {
      tree.innerHTML = '<div class="exp-tree-item" style="color:#ef9a9a; font-size:0.82rem; padding:1rem;">Request failed</div>';
    });
  }

  function renderTree(entries) {
    const tree = document.getElementById('explorerTree');
    tree.innerHTML = '';

    if (!entries.length) {
      const empty = document.createElement('div');
      empty.className = 'exp-tree-item';
      empty.style.cssText = 'color:var(--nr-text-muted); font-size:0.82rem; justify-content:center;';
      empty.textContent = 'Empty';
      tree.appendChild(empty);
      return;
    }

    const order = { ou: 0, group: 1, user: 2 };
    entries.sort((a, b) => (order[a.type] ?? 3) - (order[b.type] ?? 3) || a.label.localeCompare(b.label));

    entries.forEach(e => {
      const el = document.createElement('div');

      if (e.type === 'ou') {
        el.className = 'exp-tree-item ou';
        el.innerHTML = `
          <i class="bi bi-folder exp-item-icon" style="color:#ffe082;"></i>
          <span class="exp-item-label">${nrEscape(e.label)}</span>
          <i class="bi bi-chevron-right" style="color:var(--nr-text-faint); font-size: 0.75rem;"></i>`;
        el.addEventListener('click', () => drillDown(e.dn, e.label));
      } else {
        const icon     = e.type === 'group' ? 'bi-people-fill' : 'bi-person-fill';
        const color    = e.type === 'group' ? '#80deea' : '#a5d6a7';
        const subLabel = e.type === 'user' ? nrEscape(e.username || e.label) : nrEscape(e.dn);
        const isSel    = !!explorerSelected[e.dn];
        el.className   = `exp-tree-item selectable${isSel ? ' selected' : ''}`;
        el.dataset.dn  = e.dn;
        el.innerHTML   = `
          <i class="bi ${icon} exp-item-icon" style="color:${color};"></i>
          <span class="exp-item-label">${nrEscape(e.label)}</span>
          <span class="exp-item-sub">${subLabel}</span>
          ${isSel ? '<i class="bi bi-check-lg chk" style="color:#00bcd4; font-size:0.8rem;"></i>' : ''}`;
        el.addEventListener('click', () => {
          if (explorerSelected[e.dn]) {
            delete explorerSelected[e.dn];
          } else {
            explorerSelected[e.dn] = e;
          }
          const nowSel = !!explorerSelected[e.dn];
          el.classList.toggle('selected', nowSel);
          const chk = el.querySelector('.chk');
          if (chk) chk.remove();
          if (nowSel) {
            const check = document.createElement('i');
            check.className = 'bi bi-check-lg chk';
            check.style.cssText = 'color:#00bcd4; font-size:0.8rem;';
            el.appendChild(check);
          }
          renderSelectedPanel();
        });
      }

      tree.appendChild(el);
    });
  }

  function drillDown(dn, label) {
    explorerStack.push({ dn, label });
    loadExplorerLevel(dn);
  }

  function navigateTo(index) {
    explorerStack = explorerStack.slice(0, index + 1);
    loadExplorerLevel(explorerStack[explorerStack.length - 1].dn);
  }

  function renderBreadcrumb() {
    const el = document.getElementById('explorerBreadcrumb');
    el.innerHTML = '';
    explorerStack.forEach((crumb, i) => {
      if (i > 0) {
        const sep = document.createElement('span');
        sep.className = 'exp-crumb-sep';
        sep.textContent = '/';
        el.appendChild(sep);
      }
      const span = document.createElement('span');
      span.textContent = crumb.label;
      if (i === explorerStack.length - 1) {
        span.className = 'exp-crumb current';
      } else {
        span.className = 'exp-crumb';
        span.addEventListener('click', () => navigateTo(i));
      }
      el.appendChild(span);
    });
  }

  function renderSelectedPanel() {
    const items = Object.values(explorerSelected);
    const panel = document.getElementById('expSelectedPanel');
    const count = document.getElementById('expSelCount');
    const btn   = document.getElementById('btnImport');

    count.textContent = items.length;
    btn.disabled = !items.length;
    panel.innerHTML = '';

    if (!items.length) {
      const empty = document.createElement('div');
      empty.style.cssText = 'color:var(--nr-text-muted); font-size:0.78rem; text-align:center; padding-top:1rem;';
      empty.textContent = 'Nothing selected';
      panel.appendChild(empty);
      return;
    }

    items.forEach(e => {
      const icon  = e.type === 'group' ? 'bi-people-fill' : 'bi-person-fill';
      const color = e.type === 'group' ? '#80deea' : '#a5d6a7';
      const el = document.createElement('div');
      el.className = 'exp-sel-item';
      el.innerHTML = `
        <i class="bi ${icon} exp-sel-icon" style="color:${color};"></i>
        <span class="exp-sel-label" title="${nrEscape(e.dn)}">${nrEscape(e.label)}</span>
        <i class="bi bi-x exp-sel-remove"></i>`;
      el.querySelector('.exp-sel-remove').addEventListener('click', () => {
        delete explorerSelected[e.dn];
        renderSelectedPanel();
        const treeEl = document.querySelector(`[data-dn="${CSS.escape(e.dn)}"]`);
        if (treeEl) {
          treeEl.classList.remove('selected');
          const chk = treeEl.querySelector('.chk');
          if (chk) chk.remove();
        }
      });
      panel.appendChild(el);
    });
  }

  function importSelected() {
    const items = Object.values(explorerSelected);
    const btn = document.getElementById('btnImport');
    const res = document.getElementById('expImportResult');
    btn.disabled = true;
    res.style.display = 'none';

    fetch(`${LDAP_BASE}/${explorerServerId}/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
      // ticked: names of accounts whose server was deleted reconnect to this one
      body: JSON.stringify(document.getElementById('expReconnect').checked
        ? {items, reconnect: true} : items)
    })
    .then(r => r.json())
    .then(data => {
      res.style.display = 'block';
      if (data.status === 'ok') {
        const parts = [];
        if (data.users_created)  parts.push(`${data.users_created} user${data.users_created !== 1 ? 's' : ''}`);
        if (data.groups_created) parts.push(`${data.groups_created} group rule${data.groups_created !== 1 ? 's' : ''}`);
        if (data.reconnected)    parts.push(`${data.reconnected} reconnected (disabled - enable them in User Management)`);
        if (data.skipped)        parts.push(`${data.skipped} skipped`);
        res.innerHTML = `<span class="res-ok"><i class="bi bi-check-circle me-1"></i>Imported: ${parts.join(', ') || 'nothing new'}</span>`;
        explorerSelected = {};
        renderSelectedPanel();
      } else {
        res.innerHTML = `<span class="res-err"><i class="bi bi-x-circle me-1"></i>${nrEscape(data.message)}</span>`;
        btn.disabled = false;
      }
    });
  }
