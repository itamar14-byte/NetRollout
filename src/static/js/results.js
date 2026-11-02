// Results - see templates/results.html (data-split-view).
// ── Verification: each device's judged push, loaded on demand ────────────────
const VERIFICATIONS = {}; // cache: "job_id:ip:port" → the config_diff answer
const VD_TAB_THRESHOLD = 5;
let _verifyDiffJobId = null;
// View Log: the job's log in the page (its lines as written - redacted), filterable
let _logLines = [];
function _renderLog() {
    const want = document.getElementById('logFilter').value.trim().toLowerCase();
    const box = document.getElementById('logLines');
    const shown = want ? _logLines.filter(l => l.toLowerCase().includes(want)) : _logLines;
    const frag = document.createDocumentFragment();
    shown.forEach(line => {
        const tab = line.indexOf('\t');
        const row = document.createElement('div');
        if (tab > 0) {
            row.appendChild(_el('span', 'lv-time', line.slice(0, tab)));
            row.appendChild(document.createTextNode(line.slice(tab + 1)));
        } else {
            row.textContent = line;
        }
        if (line.includes('ACTION NEEDED')) row.className = 'lv-action';
        frag.appendChild(row);
    });
    box.replaceChildren(frag);
    const info = document.getElementById('logInfo');
    info.textContent = info.dataset.base + (want ? ` - ${shown.length} matching` : '');
}

async function showLog(btn) {
    const box = document.getElementById('logLines');
    const info = document.getElementById('logInfo');
    document.getElementById('logFilter').value = '';
    document.getElementById('logDownload').href = btn.dataset.download;
    box.replaceChildren(_el('div', 'nr-muted', 'Loading…'));
    info.dataset.base = '';
    info.textContent = '';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('logModal')).show();
    try {
        const resp = await fetch(`/results/log/${btn.dataset.jobId}`);
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.message || 'Couldn\'t load the log.');
        _logLines = data.lines;
        info.dataset.base = data.name + ' - ' + data.total + ' line' + (data.total !== 1 ? 's' : '') +
            (data.total > data.lines.length ? ` (the last ${data.lines.length} shown - Download for all)` : '');
        _renderLog();
    } catch (e) {
        _logLines = [];
        box.replaceChildren(_el('div', 'nr-danger', e.message));
    }
}
document.addEventListener('DOMContentLoaded', () => {
    const filter = document.getElementById('logFilter');
    if (filter) filter.addEventListener('input', _renderLog);
});

// A device not judged: what it replied, command by command (device_results.replies)
function showReplies(btn) {
    const replies = JSON.parse(btn.dataset.replies || '[]');
    document.getElementById('repliesDevice').textContent = btn.dataset.device;
    document.getElementById('repliesWhy').textContent = (btn.dataset.status === 'partial'
        ? 'Not judged: the device stopped answering before the end.'
        : 'Not judged: Verify was off for this rollout, or the configuration couldn\'t be read.') +
        ' A device also replies when a command works (FortiOS: "new entry added"), so read what it said - ' +
        'or run it again with Verify on to have the configuration decide.';
    const list = document.getElementById('repliesList');
    list.replaceChildren();
    replies.forEach(r => {
        list.appendChild(_el('div', 'rp-cmd', r.command));
        list.appendChild(_el('div', 'rp-said', (r.reply || []).join('\n')));
    });
    bootstrap.Modal.getOrCreateInstance(document.getElementById('repliesModal')).show();
}

let _reloadOnClose = false;   // a decision was taken: the page shows it after the window

document.getElementById('verifyDiffModal').addEventListener('hidden.bs.modal', () => {
    if (_reloadOnClose) location.reload();
});

// btn: a job's Verification button, or a device's review pill (data-ip / data-port:
// the tab to open first)
async function showConfigDiff(btn) {
    _verifyDiffJobId   = btn.dataset.jobId;
    const devices      = JSON.parse(btn.dataset.devices);
    const start        = Math.max(0, devices.findIndex(d => d.ip === btn.dataset.ip &&
                                                           String(d.port) === btn.dataset.port));
    const tabBar = document.getElementById('verifyDiffTabs');
    tabBar.innerHTML = '';

    if (devices.length <= VD_TAB_THRESHOLD) {
        tabBar.style.cssText = 'display:flex; gap:0; overflow-x:auto;';
        devices.forEach((device, i) => {
            const tab = document.createElement('button');
            tab.type = 'button';
            tab.textContent = device.label || device.ip;
            if (device.decide) {
                const dot = document.createElement('i');
                dot.className = 'bi bi-question-circle-fill ms-1';
                dot.style.color = '#ffa726';
                tab.appendChild(dot);
            }
            tab.title = device.ip;
            tab.style.cssText =
                'background:none; border:none; border-bottom:2px solid transparent;' +
                'padding:6px 14px; font-family:\'JetBrains Mono\',monospace; font-size:0.75rem;' +
                'color:var(--nr-text-muted); cursor:pointer; white-space:nowrap; transition:color 0.15s, border-color 0.15s;';
            tab.onclick = () => _renderVerifyDiff(device, tab);
            tabBar.appendChild(tab);
        });
        _renderVerifyDiff(devices[start], tabBar.children[start]);
    } else {
        tabBar.style.cssText = 'display:flex; flex-direction:column; gap:6px; padding:0 0 10px;';
        const searchInput = document.createElement('input');
        searchInput.type = 'text';
        searchInput.placeholder = 'Search device…';
        searchInput.setAttribute('aria-label', 'Search device');
        searchInput.style.cssText =
            'width:100%; background:#111; border:1px solid #2a2a2a; border-radius:6px;' +
            'color:#ccc; font-size:0.78rem; padding:5px 10px; outline:none;';
        tabBar.appendChild(searchInput);
        const listEl = document.createElement('div');
        listEl.style.cssText = 'max-height:140px; overflow-y:auto; display:flex; flex-direction:column; gap:2px;';
        const buildList = (filter) => {
            listEl.innerHTML = '';
            devices.forEach((device, i) => {
                const label = device.label || device.ip;
                if (filter && !(label + ' ' + device.ip).toLowerCase().includes(filter.toLowerCase())) return;
                const opt = document.createElement('button');
                opt.type = 'button';
                opt.style.cssText =
                    'background:none; border:none; border-radius:4px; text-align:left; padding:5px 8px;' +
                    'font-size:0.78rem; cursor:pointer; color:#ccc; font-family:\'JetBrains Mono\',monospace;';
                opt.textContent = label + (label !== device.ip ? ' (' + device.ip + ')' : '') +
                                  (device.decide ? ' - to decide' : '');
                opt.onclick = () => {
                    listEl.querySelectorAll('button').forEach(b => b.style.background = 'none');
                    opt.style.background = 'rgba(0,188,212,0.08)';
                    _renderVerifyDiff(device, null);
                };
                listEl.appendChild(opt);
            });
        };
        buildList('');
        searchInput.addEventListener('input', () => buildList(searchInput.value));
        tabBar.appendChild(listEl);
        _renderVerifyDiff(devices[start], null);
    }
    bootstrap.Modal.getOrCreateInstance(document.getElementById('verifyDiffModal')).show();
}

// How each verdict shows (src/rollout/evidence.Verdict)
const VERDICT_LOOK = {
    'took effect':      {icon: 'bi-check-circle-fill', color: '#66bb6a', css: 'vf-ok'},
    'already in place': {icon: 'bi-check-circle',      color: '#66bb6a', css: 'vf-ok'},
    'removed':          {icon: 'bi-dash-circle-fill',  color: '#66bb6a', css: 'vf-ok'},
    'review':           {icon: 'bi-question-circle-fill', color: '#ffa726', css: 'vf-review'},
    'rejected':         {icon: 'bi-x-circle-fill',     color: '#ef5350', css: 'vf-rejected'},
    'not a configuration command': {icon: 'bi-terminal', color: 'var(--nr-text-faint)', css: ''},
    'navigation':       {icon: 'bi-arrow-return-left', color: 'var(--nr-text-faint)', css: ''},
};

function _el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
}

// a device line as evidence: "+ …" added, "- …" removed, "  …" there already
function _evidenceLine(line) {
    const kind = line.startsWith('+') ? 'add' : line.startsWith('-') ? 'del' : 'same';
    return _el('div', 'vf-line ' + kind, line);
}

// device: {ip, port, label, status} - the IP alone is ambiguous when devices share it
async function _renderVerifyDiff(device, activeTab) {
    if (activeTab) {
        document.querySelectorAll('#verifyDiffTabs > button').forEach(t => {
            t.style.color = 'var(--nr-text-muted)';
            t.style.borderColor = 'transparent';
        });
        activeTab.style.color = '#00bcd4';
        activeTab.style.borderColor = '#00bcd4';
    }
    const key = _verifyDiffJobId + ':' + device.ip + ':' + device.port;
    const list = document.getElementById('verifyDiffList');
    if (!VERIFICATIONS[key]) {
        list.replaceChildren(_el('div', 'nr-muted', 'Loading…'));
        try {
            const resp = await fetch(`/results/config_diff/${_verifyDiffJobId}/${encodeURIComponent(device.ip)}?port=${device.port}`);
            const data = await resp.json();
            if (data.status === 'error') {
                list.replaceChildren(_el('div', 'nr-danger', data.message));
                return;
            }
            VERIFICATIONS[key] = data;
        } catch (e) {
            list.replaceChildren(_el('div', 'nr-danger', 'Couldn\'t load the verification.'));
            return;
        }
    }
    const data = VERIFICATIONS[key];
    list.replaceChildren();
    const judged = (data.transcript && data.transcript.commands) || [];
    if (!judged.length) {
        list.appendChild(_el('div', 'nr-muted',
            'This job was verified before NetRollout judged pushes by the configuration: ' +
            'its results per command are in the rollout log.'));
        return;
    }

    // the decision: to take, or taken
    if (data.review) {
        const box = _el('div', 'vf-decided');
        box.appendChild(_el('strong', '', data.review.outcome.charAt(0).toUpperCase() + data.review.outcome.slice(1)));
        box.append(' by ' + data.review.by + (data.review.at ? ' at ' + data.review.at : ''));
        if (data.review.comment) box.appendChild(_el('div', 'nr-muted mt-1', data.review.comment));
        if (data.review.alike && data.review.alike.length) {
            box.appendChild(_el('div', 'nr-muted mt-1', 'Decided with it: ' + data.review.alike.join(', ')));
        }
        list.appendChild(box);
    } else if (data.decidable) {
        const box = _el('div', 'vf-decide');
        const rejected = judged.some(c => c.verdict === 'rejected');
        box.appendChild(_el('div', '', rejected
            ? 'Rejected: the configuration doesn\'t show these commands, and the device replied (its reply ' +
              'is with each). If the device did take them - a command whose effect isn\'t kept in the ' +
              'configuration, such as generating keys - confirm. The decision is kept with the result.'
            : 'NetRollout couldn\'t prove every command on this device. Check the evidence below, then ' +
              'decide: the decision is kept with the result.'));
        // one decision covers all of them: say so when they differ in kind
        if (rejected && judged.some(c => c.verdict === 'review')) {
            box.appendChild(_el('div', 'vf-mixed',
                'This device has a command in review and a rejected one, and one decision covers both: ' +
                'Confirm says they all took effect; Reject leaves it partial (another command was proven) or ' +
                'failed. Say which was which in the comment.'));
        }
        const comment = _el('textarea');
        comment.placeholder = 'Comment (optional) - why, for the record';
        comment.maxLength = 2000;
        comment.setAttribute('aria-label', 'Comment on the decision');
        box.appendChild(comment);
        // the job's other devices awaiting a decision with the same commands,
        // verdicts and replies: decided the same way on request
        const alike = data.alike || [];
        const together = _el('input', 'nr-check');
        if (alike.length) {
            const label = _el('label', 'vf-alike');
            together.type = 'checkbox';
            const shown = alike.slice(0, 6).join(', ') + (alike.length > 6 ? ', …' : '');
            label.append(together, _el('span', '', `Also decide the ${alike.length} other device` +
                `${alike.length !== 1 ? 's' : ''} in this job with the same commands, verdicts and replies ` +
                `(${shown}) - each gets its own record.`));
            box.appendChild(label);
        }
        const row = _el('div', 'd-flex gap-2 align-items-center');
        const confirm = _el('button', 'btn btn-sm btn-success', 'Confirm - it took effect');
        const reject = _el('button', 'btn btn-sm btn-outline-danger', 'Reject - it didn\'t');
        confirm.type = reject.type = 'button';
        const said = _el('span', 'nr-muted ms-2');
        said.setAttribute('role', 'status');
        row.append(confirm, reject, said);
        box.appendChild(row);
        const decide = async (outcome) => {
            confirm.disabled = reject.disabled = true;
            said.textContent = 'Saving…';
            try {
                const r = await fetch(`/results/review/${_verifyDiffJobId}/${encodeURIComponent(device.ip)}?port=${device.port}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
                    body: JSON.stringify({outcome: outcome, comment: comment.value,
                                          alike: alike.length > 0 && together.checked}),
                });
                const answer = await r.json();
                if (!r.ok) throw new Error(answer.message || 'Not saved');
                data.device_status = answer.status;
                data.review = {outcome: answer.review_outcome, by: 'you',
                               at: (answer.reviewed_at || '').replace('T', ' ').slice(0, 16),
                               comment: comment.value.trim() || null,
                               alike: answer.alike || []};
                // the devices decided with it: read again when opened
                Object.keys(VERIFICATIONS).forEach(k => {
                    if (k.startsWith(_verifyDiffJobId + ':') && k !== key) delete VERIFICATIONS[k];
                });
                _reloadOnClose = true;
                _renderVerifyDiff(device, null);
            } catch (e) {
                said.textContent = e.message;
                confirm.disabled = reject.disabled = false;
            }
        };
        confirm.onclick = () => decide('confirmed');
        reject.onclick = () => decide('rejected');
        list.appendChild(box);
    }

    judged.forEach(c => {
        const look = VERDICT_LOOK[c.verdict] || VERDICT_LOOK.review;
        const item = _el('div', 'vf-cmd ' + look.css);
        const head = _el('div', 'vf-head');
        const icon = _el('i', 'bi ' + look.icon);
        icon.style.color = look.color;
        icon.setAttribute('aria-hidden', 'true');
        head.append(icon, _el('span', 'vf-text', c.command), _el('span', 'vf-verdict', c.verdict));
        item.appendChild(head);
        const more = _el('div', 'vf-more');
        (c.evidence || []).forEach(line => more.appendChild(_evidenceLine(line)));
        if (c.note) more.appendChild(_el('div', 'vf-note', c.note));
        if (c.reply && c.reply.length) {
            const reply = _el('div', 'vf-reply', 'The device replied: ');
            reply.appendChild(_el('code', '', c.reply.join(' | ')));
            more.appendChild(reply);
        }
        if (more.children.length) item.appendChild(more);
        list.appendChild(item);
    });

    const other = (data.transcript.other_changes || []);
    if (other.length) {
        list.appendChild(_el('div', 'vf-section', 'Changed, not by these commands'));
        list.appendChild(_el('div', 'vf-note', 'Another person, or the device itself, during the rollout.'));
        other.forEach(line => list.appendChild(_evidenceLine(line)));
    }
    // an empty diff: read before and after, and nothing changed (every
    // command already in place, or none took effect)
    if (data.diff === '') {
        list.appendChild(_el('div', 'vf-section', 'The configuration, before → after'));
        list.appendChild(_el('div', 'vf-note', 'Unchanged: the push added and removed nothing.'));
    }
    if (data.diff) {
        // a line the device rewrites by itself on every push or read: a comment
        // (no tag: the grammar doesn't read it) or the device's own churn
        const tagsOf = data.diff_tags || [];
        const changed = (line) => (line.startsWith('+') && !line.startsWith('+++'))
                               || (line.startsWith('-') && !line.startsWith('---'));
        const isNoise = (line, n) => !!data.diff_tags && changed(line) && (!tagsOf[n] || tagsOf[n].device);
        const lines = data.diff.split('\n');
        const onlyNoise = !!data.diff_tags && lines.every((line, n) => !changed(line) || isNoise(line, n));
        if (onlyNoise) {
            list.appendChild(_el('div', 'vf-note',
                'Unchanged: the push changed no configuration - only lines the device rewrites by itself.'));
        }
        const details = _el('details');
        const summary = _el('summary', 'vf-section', 'The configuration, before → after');
        summary.style.cursor = 'pointer';
        details.appendChild(summary);
        // each changed line tagged with what made it, matched by the server
        // (rows judged before the tags were kept show the plain diff)
        const tags = data.diff_tags || [];
        const sent = data.transcript.commands || [];
        const pre = _el('pre', 'vf-diff');
        let noise = 0;
        lines.forEach((line, n) => {
            const kind = line.startsWith('+') && !line.startsWith('+++') ? 'add'
                       : line.startsWith('-') && !line.startsWith('---') ? 'del' : 'same';
            const row = _el('div', 'vf-line ' + kind);
            row.appendChild(_el('span', '', line));
            const change = tags[n] || null;
            if (change) {
                let tag;
                if (change.device) {
                    tag = 'the device\'s own';
                    row.classList.add('vf-churn');
                } else if (change.review && change.review.length) {
                    tag = '← ' + change.review.map(i => `'${sent[i].command.trim()}' (review)`).join(', ');
                } else if (change.by.length) {
                    tag = '← ' + change.by.map(i => `'${sent[i].command.trim()}'`).join(', ');
                } else {
                    tag = 'not by these commands';
                    row.classList.add('vf-other');
                }
                row.appendChild(_el('span', 'vf-by', tag));
            } else if (data.diff_tags && kind !== 'same') {
                // a changed line the platform's grammar doesn't read as configuration
                // (a version comment, a block's closing "next"): never judged
                row.classList.add('vf-skip');
                row.appendChild(_el('span', 'vf-by', 'not configuration'));
            }
            if (isNoise(line, n)) {
                row.classList.add('vf-noise');
                noise++;
            }
            pre.appendChild(row);
        });
        if (noise) {
            // NetRollout's own switch (.nr-toggle), not the browser's checkbox
            const label = _el('label', 'vf-switch');
            const toggle = _el('span', 'nr-toggle');
            const box = _el('input');
            box.type = 'checkbox';
            box.id = 'vf-show-noise';
            box.onchange = () => pre.classList.toggle('show-noise', box.checked);
            toggle.append(box, _el('span', 'nr-toggle-slider'));
            label.append(toggle, `Show lines the device rewrites by itself (${noise}) - version comments and re-encrypted passwords, changed on every push or read`);
            details.appendChild(label);
        }
        details.appendChild(pre);
        list.appendChild(details);
    }
}

// ── Copy job ID ──────────────────────────────────────────────────────────────
function copyJobId(btn, id) {
    navigator.clipboard.writeText(id).then(() => {
        const icon = btn.querySelector('i');
        icon.className = 'bi bi-clipboard-check';
        btn.style.color = '#a5d6a7';
        setTimeout(() => {
            icon.className = 'bi bi-clipboard';
            btn.style.color = 'var(--nr-text-muted)';
        }, 1500);
    });
}

// ── Admin: all-users toggle ──────────────────────────────────────────────────
function toggleAllUsers() {
    const flat  = document.getElementById('flat-view');
    const split = document.getElementById('split-view');
    const btn   = document.getElementById('allUsersToggle');
    const label = document.getElementById('jobCountLabel');
    if (!split) return;
    const on = split.style.display === 'none';
    flat.style.display  = on ? 'none' : '';
    split.style.display = on ? ''     : 'none';
    btn.style.borderColor = on ? 'rgba(0,188,212,0.35)' : '';
    btn.style.color       = on ? '#00bcd4'               : '';
    btn.style.background  = on ? 'rgba(0,188,212,0.06)'  : '';
    // the count names the status filter, as the server writes it
    const filter = label.dataset.filter ? label.dataset.filter + ' ' : '';
    if (on) {
        const total = parseInt(label.dataset.allTotal);
        label.textContent = total + ' ' + filter + 'job' + (total !== 1 ? 's' : '') + ' (all users)';
    } else {
        const mine = parseInt(label.dataset.mineTotal);
        label.textContent = mine + ' ' + filter + 'job' + (mine !== 1 ? 's' : '');
    }
    // the filters follow the view: the user filter is the all-users view's
    // only (leaving it with one set reloads without it), the export the view's
    const viewInput = document.getElementById('filterView');
    const userFilter = document.getElementById('filterUser');
    if (viewInput) viewInput.disabled = !on;
    if (userFilter) userFilter.hidden = !on;
    // the view stays on a reload; its pages are its own
    const url = new URL(window.location.href);
    if (on) url.searchParams.set('view', 'all'); else url.searchParams.delete('view');
    if (!on && url.searchParams.get('user')) {
        url.searchParams.delete('user');
        window.location.assign(url);
        return;
    }
    history.replaceState(null, '', url);
}

// ── Filters: server-side, so a change reloads the page. Typing applies after
// a pause, and the field typed in keeps the focus across the reload. ──
(() => {
    const form = document.getElementById('resultsFilters');
    if (!form) return;
    const FOCUS_KEY = 'nr-results-filter-focus';
    const apply = field => {
        try { sessionStorage.setItem(FOCUS_KEY, field ? field.name : ''); } catch (e) { /* none */ }
        // blank fields stay out of the address
        form.querySelectorAll('input, select').forEach(f => { if (!f.value) f.disabled = true; });
        form.submit();
    };
    let timer = null;
    form.querySelectorAll('input[type=search]').forEach(field => {
        field.addEventListener('input', () => {
            clearTimeout(timer);
            timer = setTimeout(() => apply(field), 600);
        });
        field.addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); clearTimeout(timer); apply(field); }
            if (e.key === 'Escape' && field.value) { e.preventDefault(); field.value = ''; clearTimeout(timer); apply(field); }
        });
    });
    form.querySelectorAll('select, input[type=date]').forEach(field =>
        field.addEventListener('change', () => apply(null)));
    let focused = '';
    try { focused = sessionStorage.getItem(FOCUS_KEY) || ''; sessionStorage.removeItem(FOCUS_KEY); } catch (e) { /* none */ }
    const field = focused && form.querySelector(`input[name="${focused}"]`);
    if (field) {
        field.focus();
        field.setSelectionRange(field.value.length, field.value.length);
    }
    // the export: what the form says now, in the view shown
    document.getElementById('exportCsv').addEventListener('click', function () {
        const params = new URLSearchParams([...new FormData(form)].filter(([, value]) => value));
        params.delete('per_page');
        window.location.href = this.dataset.url + (params.toString() ? '?' + params : '');
    });
})();
if (document.currentScript.dataset.splitView) toggleAllUsers();

function toggleSection(header, bodyId) {
    const body = document.getElementById(bodyId);
    const open = header.classList.toggle('open');
    header.setAttribute('aria-expanded', open ? 'true' : 'false');
    body.style.display = open ? '' : 'none';
}

// ── Expand/collapse ──────────────────────────────────────────────────────────
function toggleJob(row) {
    if (diffMode) {               // a click on a card picks it for the diff
        const box = row.querySelector('.diff-checkbox');
        if (row.classList.contains('dimmed') || box.disabled) return;
        box.checked = !box.checked;
        handleDiffSelect(box);
        return;
    }
    setJobOpen(row, !row.classList.contains('expanded'));
}

function setJobOpen(row, open) {
    row.classList.toggle('expanded', open);
    row.querySelector('.job-toggle').setAttribute('aria-expanded', open ? 'true' : 'false');
}

function showCommands(commands) {
    const body = document.getElementById('commandsModalBody');
    body.textContent = Array.isArray(commands) && commands.length
        ? commands.join('\n')
        : '(no commands recorded)';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('commandsModal')).show();
}

// ── /results?job=<id> (the completion card's link): open that job ─────────────
// (the server shows the page it is on, and the all-users view for another
// user's job; an own job is in both views - the one shown is opened)
(() => {
    const id = new URLSearchParams(window.location.search).get('job');
    if (!id) return;
    const row = [...document.querySelectorAll('.job-row')]
        .find(r => r.dataset.jobId === id && r.offsetParent !== null);
    if (!row) return;
    setJobOpen(row, true);
    row.scrollIntoView({block: 'center'});
})();

// ── Diff mode ────────────────────────────────────────────────────────────────
let diffMode = false;
let selectedJobs = [];

function toggleDiffMode() {
    diffMode = !diffMode;
    document.getElementById('job-list').classList.toggle('diff-mode', diffMode);
    document.getElementById('diffModeBtn').classList.toggle('active', diffMode);
    if (!diffMode) resetDiffSelection();
}

// A click outside the cards (not on Compare / Diff, not in a dialog), or
// Escape, leaves compare mode
document.addEventListener('click', e => {
    if (!diffMode) return;
    if (e.target.closest('.job-row, #diffModeBtn, #compareBtn, .modal')) return;
    toggleDiffMode();
});
document.addEventListener('keydown', e => {
    // not the Escape that closes a dialog (Bootstrap hides it before this runs)
    if (diffMode && e.key === 'Escape' && !e.target.closest('.modal')
            && !document.body.classList.contains('modal-open')) toggleDiffMode();
});

function resetDiffSelection() {
    selectedJobs = [];
    document.querySelectorAll('.diff-checkbox').forEach(cb => {
        cb.checked = false;
        cb.disabled = false;
    });
    document.querySelectorAll('.job-row').forEach(r =>
        r.classList.remove('dimmed', 'diff-selected'));
    document.getElementById('compareBtn').style.display = 'none';
}

function handleDiffSelect(checkbox) {
    const row = checkbox.closest('.job-row');
    const jobId = row.dataset.jobId;

    if (checkbox.checked) {
        if (selectedJobs.length >= 2) { checkbox.checked = false; return; }
        if (selectedJobs.some(j => j.jobId === jobId)) { checkbox.checked = false; return; }
        selectedJobs.push({
            jobId,
            commands: JSON.parse(row.dataset.commands),
            startedAt: row.dataset.startedAt,
            label: jobId.slice(0, 8) + '… · ' +
                   row.dataset.startedAt.slice(0, 16).replace('T', ' ') +
                   (row.dataset.comment ? ' · ' + row.dataset.comment : '')
        });
        row.classList.add('diff-selected');
    } else {
        selectedJobs = selectedJobs.filter(j => j.jobId !== jobId);
        row.classList.remove('diff-selected');
    }

    const allRows = document.querySelectorAll('.job-row');
    if (selectedJobs.length === 2) {
        allRows.forEach(r => {
            if (!r.classList.contains('diff-selected')) {
                r.classList.add('dimmed');
                r.querySelector('.diff-checkbox').disabled = true;
            }
        });
        document.getElementById('compareBtn').style.display = 'inline-flex';
    } else {
        allRows.forEach(r => {
            r.classList.remove('dimmed');
            r.querySelector('.diff-checkbox').disabled = false;
        });
        document.getElementById('compareBtn').style.display = 'none';
    }
}

// ── LCS diff ─────────────────────────────────────────────────────────────────
function computeLCS(a, b) {
    const m = a.length, n = b.length;
    const dp = Array.from({length: m + 1}, () => new Array(n + 1).fill(0));
    for (let i = 1; i <= m; i++)
        for (let j = 1; j <= n; j++)
            dp[i][j] = a[i-1] === b[j-1]
                ? dp[i-1][j-1] + 1
                : Math.max(dp[i-1][j], dp[i][j-1]);

    const ops = [];
    let i = m, j = n;
    while (i > 0 || j > 0) {
        if (i > 0 && j > 0 && a[i-1] === b[j-1]) {
            ops.unshift({type: 'equal', left: a[i-1], right: b[j-1]});
            i--; j--;
        } else if (j > 0 && (i === 0 || dp[i][j-1] >= dp[i-1][j])) {
            ops.unshift({type: 'insert', left: null, right: b[j-1]});
            j--;
        } else {
            ops.unshift({type: 'delete', left: a[i-1], right: null});
            i--;
        }
    }
    return ops;
}

function mergeDiff(ops) {
    const out = [];
    let i = 0;
    while (i < ops.length) {
        if (ops[i].type === 'delete' && i + 1 < ops.length && ops[i+1].type === 'insert') {
            out.push({type: 'change', left: ops[i].left, right: ops[i+1].right});
            i += 2;
        } else {
            out.push(ops[i++]);
        }
    }
    return out;
}

// ── Run & render diff ─────────────────────────────────────────────────────────
function runDiff() {
    if (selectedJobs.length !== 2) return;

    const sorted = [...selectedJobs].sort((a, b) =>
        a.startedAt.localeCompare(b.startedAt));
    const left = sorted[0], right = sorted[1];

    document.getElementById('diffLeftLabel').textContent  = left.label;
    document.getElementById('diffRightLabel').textContent = right.label;

    const ops = mergeDiff(computeLCS(left.commands, right.commands));
    const tbody = document.getElementById('diffTbody');
    const empty = document.getElementById('diffEmpty');
    tbody.innerHTML = '';

    const allEqual = ops.every(o => o.type === 'equal');
    empty.style.display = allEqual ? 'block' : 'none';

    let ln = 1, rn = 1;
    ops.forEach(op => {
        const tr = document.createElement('tr');
        if (op.type === 'equal') {
            tr.innerHTML =
                `<td class="diff-num">${ln++}</td>` +
                `<td class="diff-cell">${nrEscape(op.left)}</td>` +
                `<td class="diff-sep"></td>` +
                `<td class="diff-num">${rn++}</td>` +
                `<td class="diff-cell">${nrEscape(op.right)}</td>`;
        } else if (op.type === 'delete') {
            tr.innerHTML =
                `<td class="diff-num diff-del-num">${ln++}</td>` +
                `<td class="diff-cell diff-del"><span class="diff-sign">−</span>${nrEscape(op.left)}</td>` +
                `<td class="diff-sep"></td>` +
                `<td class="diff-num"></td>` +
                `<td class="diff-cell diff-del" style="opacity:0.2;"></td>`;
        } else if (op.type === 'insert') {
            tr.innerHTML =
                `<td class="diff-num"></td>` +
                `<td class="diff-cell diff-ins" style="opacity:0.2;"></td>` +
                `<td class="diff-sep"></td>` +
                `<td class="diff-num diff-ins-num">${rn++}</td>` +
                `<td class="diff-cell diff-ins"><span class="diff-sign">+</span>${nrEscape(op.right)}</td>`;
        } else {
            tr.innerHTML =
                `<td class="diff-num diff-chg-num">${ln++}</td>` +
                `<td class="diff-cell diff-chg"><span class="diff-sign">~</span>${nrEscape(op.left)}</td>` +
                `<td class="diff-sep"></td>` +
                `<td class="diff-num diff-chg-num">${rn++}</td>` +
                `<td class="diff-cell diff-chg"><span class="diff-sign">~</span>${nrEscape(op.right)}</td>`;
        }
        tbody.appendChild(tr);
    });

    const diffModal = bootstrap.Modal.getOrCreateInstance(document.getElementById('diffModal'));
    diffModal.show();
}

document.getElementById('diffModal').addEventListener('hidden.bs.modal', resetDiffSelection);
