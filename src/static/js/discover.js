// Discover devices - templates/discover.html. The setup form (with the
// address check as you type: the server's rules, src/discovery/targets.py;
// the server's word is final), the warning confirmed before every start
// (its numbers from POST check), then the discovery as its events arrive
// (GET status?since=N, background requests): counters and the address map,
// a card per device found (a table above TABLE_ABOVE), the addresses that
// gave none by reason, and Add (POST create: each card through Inventory's
// rules). A card's edits stay in this page.
const DISC = document.currentScript.dataset;
const MAX_ADDRESSES = Number(DISC.maxAddresses);
const MAP_LIMIT = Number(DISC.mapLimit);
const INVENTORY_URL = DISC.inventoryUrl;
const TABLE_ABOVE = 20;
const POLL_MS = 1000;
const LOGOS = nrData('nr-vendor-logos');
const PLATFORM_NAMES = nrData('nr-platform-labels');
const INVENTORY_LABELS = new Set(nrData('nr-inventory-labels'));
const NF = new Intl.NumberFormat('en');
const ENDED = ['done', 'cancelled'];
const REASONS = {
    'wrong-credentials': 'Wrong credentials',
    'timed-out': 'Timed out',
    'failed': 'Failed',
    'not-supported': 'Not a platform NetRollout supports',
    'in-inventory': 'Already in Inventory',
    'in-rollout': 'In a rollout',
    'host-key-changed': 'SSH host key changed - not signed in to (Server Management -> SSH Host Keys)',
    'not-asked': 'Not asked - the discovery was cancelled',
};

// ── The addresses: the server's rules (targets.py), for the check as you type ──
const BLOCKS = [   // [family, network, prefix, what it holds]
    [4, '0.0.0.0', 8, 'no device addresses'], [4, '127.0.0.0', 8, "loopback - NetRollout's own server"],
    [4, '224.0.0.0', 4, 'multicast, not devices'], [4, '240.0.0.0', 4, 'reserved, not devices'],
    [6, '::', 128, 'no device address'], [6, '::1', 128, "loopback - NetRollout's own server"],
    [6, 'fe80::', 10, "link-local - scan the devices' global addresses"], [6, 'ff00::', 8, 'multicast, not devices'],
];

function v4(s) {
    const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(s);
    if (!m) return null;
    const parts = m.slice(1);
    if (parts.some(p => Number(p) > 255 || (p.length > 1 && p[0] === '0'))) return null;   // 010.0.0.1 too
    return parts.reduce((n, p) => (n << 8n) + BigInt(p), 0n);
}

function v6(s) {
    if (!s.includes(':') || !/^[0-9a-fA-F:.]+$/.test(s)) return null;
    const last = s.lastIndexOf(':');
    if (s.slice(last + 1).includes('.')) {           // an IPv4 tail
        const n = v4(s.slice(last + 1));
        if (n === null) return null;
        s = s.slice(0, last + 1) + (n >> 16n).toString(16) + ':' + (n & 0xffffn).toString(16);
    }
    const halves = s.split('::');
    if (halves.length > 2) return null;
    const parse = h => (h === '' ? [] : h.split(':'));
    const head = parse(halves[0]), tail = halves.length === 2 ? parse(halves[1]) : [];
    const groups = [...head, ...tail];
    if (groups.some(g => !/^[0-9a-fA-F]{1,4}$/.test(g))) return null;
    if (halves.length === 1 ? groups.length !== 8 : groups.length > 7) return null;
    const all = [...head, ...Array(8 - groups.length).fill('0'), ...tail];
    return all.reduce((n, g) => (n << 16n) + BigInt(parseInt(g, 16)), 0n);
}

function v4s(n) { return [24n, 16n, 8n, 0n].map(b => String((n >> b) & 255n)).join('.'); }

function v6s(n) {
    const g = Array.from({length: 8}, (_, i) => Number((n >> BigInt((7 - i) * 16)) & 0xffffn));
    let best = [-1, 0], cur = [-1, 0];
    g.forEach((x, i) => {
        if (x === 0) { if (cur[0] < 0) cur = [i, 0]; cur[1]++; if (cur[1] > best[1]) best = [...cur]; }
        else cur = [-1, 0];
    });
    const hex = g.map(x => x.toString(16));
    if (best[1] < 2) return hex.join(':');
    return hex.slice(0, best[0]).join(':') + '::' + hex.slice(best[0] + best[1]).join(':');
}

const show = (fam, n) => (fam === 4 ? v4s(n) : v6s(n));

function address(text, entry) {
    if (text.includes('%')) throw bad(entry);
    const a = v4(text);
    if (a !== null) return {fam: 4, n: a, bits: 32};
    const b = v6(text);
    if (b === null) throw bad(entry);
    if ((b >> 32n) === 0xffffn) throw new Error(`"${entry}": write an IPv4 address as ${v4s(b & 0xffffffffn)}`);
    return {fam: 6, n: b, bits: 128};
}

function bad(entry) {
    return new Error(`"${entry}" isn't an address, a subnet (10.0.0.0/24) or a range (10.0.0.10-50)`);
}

function span(entry, notes) {
    let fam, first, last;
    if (entry.includes('/')) {
        const [ip, prefix, more] = entry.split('/');
        if (more !== undefined || !/^\d{1,3}$/.test(prefix || '')) throw bad(entry);
        const a = address(ip, entry), p = Number(prefix);
        if (p > a.bits) throw new Error(`"${entry}": an IPv${a.fam} prefix is 0 to ${a.bits}`);
        const size = 1n << BigInt(a.bits - p), net = a.n - (a.n % size);
        if (net !== a.n) notes.push(`${entry} means ${show(a.fam, net)}/${p}`);
        fam = a.fam; first = net; last = net + size - 1n;
        if (fam === 4 && p <= 30) { first += 1n; last -= 1n; }
        else if (fam === 6 && p <= 126) first += 1n;
    } else if (entry.includes('-')) {
        const at = entry.indexOf('-'), left = entry.slice(0, at), right = entry.slice(at + 1);
        if (!right || right.includes('-')) throw bad(entry);
        const a = address(left, entry);
        let b;
        if (a.fam === 4 && /^\d{1,3}$/.test(right)) {
            if (Number(right) > 255) throw new Error(`"${entry}": the range's end is 0 to 255`);
            b = {fam: 4, n: (a.n & ~255n) + BigInt(right)};
        } else b = address(right, entry);
        if (b.fam !== a.fam) throw new Error(`"${entry}" mixes IPv4 and IPv6`);
        if (b.n < a.n) throw new Error(`"${entry}" runs backwards - write the lower address first`);
        fam = a.fam; first = a.n; last = b.n;
    } else {
        const a = address(entry, entry);
        fam = a.fam; first = last = a.n;
    }
    for (const [bf, net, prefix, what] of BLOCKS) {
        if (bf !== fam) continue;
        const lo = fam === 4 ? v4(net) : v6(net), hi = lo + (1n << BigInt((fam === 4 ? 32 : 128) - prefix)) - 1n;
        if (first <= hi && lo <= last)
            throw new Error(`"${entry}" ${first === last ? 'is in' : 'reaches into'} ${net}/${prefix}: ${what}`);
    }
    return {fam, first, last};
}

// {entries, notes, count, spans} or {error}; {empty} for nothing typed
function parseTargets(text) {
    const entries = text.split(/[\s,;]+/).filter(Boolean);
    if (!entries.length) return {empty: true};
    const notes = [];
    let spans;
    try { spans = entries.map(e => span(e, notes)); } catch (e) { return {error: e.message}; }
    spans.sort((a, b) => a.fam - b.fam || (a.first < b.first ? -1 : a.first > b.first ? 1 : 0));
    const merged = [];
    let overlapped = false;
    for (const s of spans) {
        const top = merged[merged.length - 1];
        if (top && top.fam === s.fam && s.first <= top.last + 1n) {
            overlapped = overlapped || s.first <= top.last;
            if (s.last > top.last) top.last = s.last;
        } else merged.push({...s});
    }
    if (overlapped) notes.push('overlapping entries counted once');
    const count = merged.reduce((n, s) => n + (s.last - s.first + 1n), 0n);
    if (count > BigInt(MAX_ADDRESSES)) {
        const said = count > 10n ** 15n ? 'about 2^' + Math.round(Math.log2(Number(count))) : NF.format(count);
        return {error: `${said} addresses - one discovery takes at most ${NF.format(MAX_ADDRESSES)}. ` +
                       'Split it, or narrow the subnet.'};
    }
    return {entries, notes, count: Number(count), spans: merged};
}

function* addressesOf(spans) {
    for (const s of spans) for (let n = s.first; n <= s.last; n++) yield show(s.fam, n);
}

// ── Small helpers ──────────────────────────────────────────────────────────
function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
        if (v === null || v === undefined || v === false) continue;
        if (k === 'class') node.className = v;
        else if (k === 'text') node.textContent = v;
        else if (k.startsWith('data-') || k.startsWith('aria-') || ['role', 'for', 'spellcheck'].includes(k))
            node.setAttribute(k, v);
        else node[k] = v;
    }
    for (const child of children) if (child !== null && child !== undefined) node.append(child);
    return node;
}

const $ = id => document.getElementById(id);
const plural = (n, one, many) => `${NF.format(n)} ${n === 1 ? one : (many || one + 's')}`;

async function call(method, url, body) {
    const headers = {'X-CSRFToken': CSRF_TOKEN};
    if (body !== undefined) headers['Content-Type'] = 'application/json';
    if (method === 'GET') headers['X-NR-Background'] = '1';
    let resp;
    try {
        resp = await fetch(url, {method, headers, cache: 'no-store',
                                 body: body === undefined ? undefined : JSON.stringify(body)});
    } catch (e) {
        return {ok: false, message: "NetRollout didn't answer - check the connection and try again."};
    }
    let data = {};
    try { data = await resp.json(); } catch (e) { /* not JSON: an error page */ }
    return {ok: resp.ok, code: resp.status, ...data,
            message: data.message || (resp.ok ? '' : `The request failed (${resp.status})`)};
}

// ── The setup form ─────────────────────────────────────────────────────────
function checkTyped() {
    const parsed = parseTargets($('discAddresses').value);
    const out = $('discAddressesCheck');
    const port = $('discPort').value.trim();
    const portOk = /^\d{1,5}$/.test(port) && Number(port) >= 1 && Number(port) <= 65535;
    out.className = 'disc-check';
    if (parsed.empty) out.textContent = '';
    else if (parsed.error) { out.classList.add('is-bad'); out.textContent = parsed.error; }
    else {
        out.classList.add(parsed.count > 1024 ? 'is-warn' : 'is-ok');
        out.textContent = [plural(parsed.entries.length, 'entry', 'entries') + ' · ' + plural(parsed.count, 'address', 'addresses'),
                           ...parsed.notes].join(' · ');
    }
    $('discPort').classList.toggle('is-invalid', !portOk);
    $('discStartBtn').disabled = !(parsed.count && portOk && $('discProfile').value);
    return parsed;
}

function asked() {
    return {addresses: $('discAddresses').value, port: $('discPort').value.trim(), profile_id: $('discProfile').value};
}

async function startClicked() {
    if (!checkTyped().count) return;
    const btn = $('discStartBtn');
    btn.disabled = true;
    const check = await call('POST', '/inventory/discover/check', asked());
    btn.disabled = false;
    if (!check.ok) { showCheckError(check.message); return; }
    if (check.refusal) { nrAlert(check.refusal, 'Not started'); return; }
    const from = !check.sources.length ? 'from this server'
        : check.sources.length === 1 ? `from ${check.sources[0]}` : `from one of ${check.sources.join(', ')}`;
    const what = $('discConfirmWhat');
    what.replaceChildren(
        'This sends TCP connections to ', el('strong', {text: plural(check.addresses, 'address', 'addresses')}),
        ` on port ${check.port} ${from}, then signs in as `, el('strong', {text: check.username}),
        ' to each one that answers - up to 4 at a time. It pauses after 3 failed sign-ins.');
    const big = $('discConfirmBig');
    big.hidden = check.addresses <= 1024;
    big.textContent = `This is a large sweep: ${plural(check.addresses, 'address', 'addresses')}.`;
    bootstrap.Modal.getOrCreateInstance($('discConfirmModal')).show();
}

function showCheckError(message) {
    const out = $('discAddressesCheck');
    out.className = 'disc-check is-bad';
    out.textContent = message;
}

async function confirmed() {
    const go = $('discConfirmGo');
    go.disabled = true;
    const started = await call('POST', '/inventory/discover/start', {...asked(), confirmed: true});
    go.disabled = false;
    bootstrap.Modal.getOrCreateInstance($('discConfirmModal')).hide();
    if (!started.ok) { nrAlert(started.message, 'Not started'); return; }
    setupShown = false;
    reset();
    apply(started.discovery);
    poll();
}

// ── The discovery's state ──────────────────────────────────────────────────
let current = null;          // the summary last received
let seq = 0;                 // the last event applied
let pollTimer = null;
const cards = new Map();     // finding id -> {finding, label, platform, global, out, selected, created, note, node}
const lines = [];            // {address, reason, detail}
let cells = null;            // address -> its map cell (up to MAP_LIMIT)
let tableMode = false;
let setupShown = false;      // "New discovery" pressed after one ended

function reset() {
    current = null; seq = 0; cells = null; tableMode = false;
    cards.clear(); lines.length = 0;
    $('discCards').replaceChildren(); $('discRows').replaceChildren(); $('discLineGroups').replaceChildren();
    $('discMap').replaceChildren(); $('discAdded').hidden = true;
}

function apply(d) {
    if (!d) { current = null; render(); return; }
    if (!current || current.started !== d.started) { if (current) reset(); buildMap(d); }
    current = d;
    for (const event of d.events) {
        if (event.seq <= seq) continue;
        seq = event.seq;
        if (event.kind === 'port') mark(event.address, event.open ? 'm-open' : 'm-closed');
        else if (event.kind === 'finding') found(event);
        else if (event.kind === 'line') { lines.push(event); mark(event.address, 'm-line'); }
    }
    for (const id of d.created || []) {
        const card = cards.get(id);
        if (card && !card.created) { card.created = true; card.note = 'Added to Inventory'; draw(card); }
    }
    render();
}

function buildMap(d) {
    const parsed = parseTargets(d.entries.join(','));
    $('discMapWrap').hidden = true;
    if (!parsed.count || parsed.count > MAP_LIMIT) return;
    cells = new Map();
    const map = $('discMap');
    for (const a of addressesOf(parsed.spans)) {
        const cell = el('i', {title: a});
        cells.set(a, cell);
        map.append(cell);
    }
    $('discMapWrap').hidden = false;
}

function mark(addr, state) {
    const cell = cells && cells.get(addr);
    if (cell && cell.className !== 'm-found') cell.className = state;
}

function found(f) {
    const card = {finding: f, label: f.hostname || f.address, platform: f.platform || '', global: false,
                  out: false, selected: false, created: false, note: '', node: null};
    cards.set(f.id, card);
    mark(f.address, 'm-found');
    if (!tableMode && cards.size > TABLE_ABOVE) {          // to the table, once: every card a row
        tableMode = true;
        $('discCards').replaceChildren();
        for (const c of cards.values()) { c.node = null; draw(c); }
    } else draw(card);
    $('discAnnounce').textContent = `Found ${f.hostname || f.address}` +
        (f.platform ? `, ${PLATFORM_NAMES[f.platform]}` : ', platform not known');
}

// ── A card (or a table row) ────────────────────────────────────────────────
function state(card) {
    if (card.created) return {text: 'Added', cls: 'is-added'};
    if (card.out) return {text: 'Left out', cls: 'is-out'};
    if (!card.label.trim()) return {text: 'Needs a label', cls: 'is-needs'};
    if (!card.platform) return {text: 'Needs a platform', cls: 'is-needs'};
    return {text: 'Ready', cls: 'is-ready'};
}

function warnings(card) {
    const label = card.label.trim();
    if (!label || card.created || card.out) return [];
    const out = [];
    if (INVENTORY_LABELS.has(label)) out.push('Inventory has a device with this label already');
    const twins = [...cards.values()].filter(c => c !== card && !c.out && !c.created && c.label.trim() === label);
    if (twins.length) out.push(`${plural(twins.length, 'other card has', 'other cards have')} this label too`);
    return out;
}

function ready(card) { return state(card).cls === 'is-ready'; }

function logo(platform) {
    const badge = el('span', {class: 'vendor-badge', 'aria-hidden': 'true'});
    if (platform && LOGOS[platform]) badge.append(el('img', {src: LOGOS[platform], alt: '', class: 'vendor-img'}));
    else badge.append(el('i', {class: 'bi bi-router'}));
    return badge;
}

function platformSelect(card, id) {
    const select = el('select', {class: 'form-select form-select-sm', id, 'data-nr-change': 'discCardEdit',
                                 'data-finding': card.finding.id, 'data-field': 'platform', disabled: card.created});
    select.append(el('option', {value: '', text: 'Choose the platform…'}));
    for (const [value, name] of Object.entries(PLATFORM_NAMES))
        select.append(el('option', {value, text: name, selected: value === card.platform}));
    return select;
}

function draw(card) {
    const fresh = tableMode ? row(card) : tile(card);
    if (card.node) card.node.replaceWith(fresh);
    else (tableMode ? $('discRows') : $('discCards')).append(fresh);
    card.node = fresh;
    refresh(card);
}

// a card's own controls are redrawn only around them, so typing keeps its focus
function refresh(card) {
    if (!card.node) return draw(card);
    const s = state(card);
    const chip = card.node.querySelector('.disc-state');
    chip.className = 'disc-state ' + s.cls;
    chip.textContent = s.text;
    const warn = card.node.querySelector('.disc-warn');
    const w = warnings(card);
    warn.hidden = !w.length && !card.note;
    warn.textContent = [...w, card.note].filter(Boolean).join(' · ');
    card.node.classList.toggle('is-out', card.out);
    const leave = card.node.querySelector('.disc-leave');
    leave.setAttribute('aria-pressed', String(card.out));
    leave.textContent = card.out ? 'Put back' : 'Leave out';
    const badge = card.node.querySelector('.vendor-badge');
    if (badge) badge.replaceWith(logo(card.platform));
}

function tile(card) {
    const f = card.finding, id = f.id, s = state(card);
    const node = el('article', {class: 'disc-card' + (card.out ? ' is-out' : ''), 'data-finding': id,
                                'aria-label': `${f.hostname || f.address} at ${f.address}`});
    node.append(
        el('div', {class: 'disc-card-head'}, logo(card.platform),
           el('div', {class: 'disc-card-id'},
              el('div', {class: 'disc-card-name', text: f.hostname || 'No hostname read'}),
              el('div', {class: 'nr-mono disc-card-addr', text: `${f.address}:${f.port}`})),
           el('span', {class: 'disc-state ' + s.cls, text: s.text})),
        el('div', {class: 'disc-card-fields'},
           el('div', {}, el('label', {class: 'nr-label', for: `dl-${id}`, text: 'Label'}),
              el('input', {class: 'form-control form-control-sm', id: `dl-${id}`, value: card.label, autocomplete: 'off',
                           spellcheck: 'false', disabled: card.created, 'data-nr-input': 'discCardEdit',
                           'data-finding': id, 'data-field': 'label'})),
           el('div', {}, el('label', {class: 'nr-label', for: `dp-${id}`, text: 'Platform'}), platformSelect(card, `dp-${id}`))),
        el('div', {class: 'disc-card-foot'},
           el('label', {class: 'disc-global'},
              el('input', {type: 'checkbox', class: 'form-check-input', checked: card.global, disabled: card.created,
                           'data-nr-change': 'discCardEdit', 'data-finding': id, 'data-field': 'global'}),
              ' Global'),
           el('button', {type: 'button', class: 'btn btn-link btn-sm disc-leave', 'aria-pressed': String(card.out),
                         disabled: card.created, 'data-nr-click': 'discLeaveOut', 'data-finding': id,
                         text: card.out ? 'Put back' : 'Leave out'})),
        evidence(f),
        el('p', {class: 'disc-warn', hidden: true}));
    return node;
}

function evidence(f) {
    if (f.evidence) return el('p', {class: 'disc-evidence nr-mono', title: f.evidence, text: f.evidence});
    return el('p', {class: 'disc-evidence', text: f.problem || 'Signed in, but the platform wasn\'t recognised - choose it.'});
}

function row(card) {
    const f = card.finding, id = f.id, s = state(card);
    const tr = el('tr', {'data-finding': id, class: card.out ? 'is-out' : ''});
    tr.append(
        el('td', {}, el('input', {type: 'checkbox', class: 'form-check-input', checked: card.selected,
                                   disabled: card.created, 'aria-label': `Select ${f.address}`,
                                   'data-nr-change': 'discCardEdit', 'data-finding': id, 'data-field': 'selected'})),
        el('td', {class: 'nr-mono'}, `${f.address}:${f.port}`),
        el('td', {}, el('input', {class: 'form-control form-control-sm', value: card.label, 'aria-label': `Label of ${f.address}`,
                                   autocomplete: 'off', spellcheck: 'false', disabled: card.created,
                                   'data-nr-input': 'discCardEdit', 'data-finding': id, 'data-field': 'label'})),
        el('td', {}, (() => { const s2 = platformSelect(card, `dp-${id}`); s2.setAttribute('aria-label', `Platform of ${f.address}`); return s2; })()),
        el('td', {}, el('input', {type: 'checkbox', class: 'form-check-input', checked: card.global, disabled: card.created,
                                   'aria-label': `Global: ${f.address}`, 'data-nr-change': 'discCardEdit',
                                   'data-finding': id, 'data-field': 'global'})),
        el('td', {}, el('span', {class: 'disc-state ' + s.cls, text: s.text}), el('p', {class: 'disc-warn', hidden: true})),
        el('td', {}, el('button', {type: 'button', class: 'btn btn-link btn-sm disc-leave', 'aria-pressed': String(card.out),
                                    disabled: card.created, 'data-nr-click': 'discLeaveOut', 'data-finding': id,
                                    text: card.out ? 'Put back' : 'Leave out'})));
    return tr;
}

function edited(input) {
    const card = cards.get(Number(input.dataset.finding));
    if (!card) return;
    const field = input.dataset.field;
    if (field === 'label') card.label = input.value;
    else if (field === 'platform') card.platform = input.value;
    else if (field === 'global') card.global = input.checked;
    else if (field === 'selected') card.selected = input.checked;
    card.note = '';
    if (field === 'label') for (const c of cards.values()) refresh(c);   // a twin's warning too
    else refresh(card);
    renderBar();
}

function leaveOut(button) {
    const card = cards.get(Number(button.dataset.finding));
    if (!card) return;
    card.out = !card.out;
    for (const c of cards.values()) refresh(c);
    renderBar();
}

function bulk(action) {
    const chosen = [...cards.values()].filter(c => !c.created);
    if (action === 'needing') {
        for (const c of chosen) c.selected = !c.platform && !c.out;
    } else if (action === 'platform') {
        const platform = $('discBulkPlatform').value;
        if (!platform) { $('discBulkPlatform').focus(); return; }
        for (const c of chosen) if (c.selected) c.platform = platform;
    } else if (action === 'out') {
        for (const c of chosen) if (c.selected) { c.out = true; c.selected = false; }
    }
    for (const c of cards.values()) draw(c);
    renderBar();
}

async function add() {
    const chosen = [...cards.values()].filter(ready);
    if (!chosen.length) return;
    $('discAddBtn').disabled = true;
    const answer = await call('POST', '/inventory/discover/create', {cards: chosen.map(c => ({
        finding: c.finding.id, label: c.label.trim(), device_type: c.platform, is_global: c.global}))});
    if (!answer.ok) { nrAlert(answer.message, 'Not added'); renderBar(); return; }
    let added = 0;
    for (const result of answer.results) {
        const card = cards.get(result.finding);
        if (!card) continue;
        if (result.created) {
            added++;
            card.created = true;
            card.note = result.warning || '';
            INVENTORY_LABELS.add(result.label);
        } else card.note = result.message;
        draw(card);
    }
    const note = $('discAdded');
    note.hidden = !added;
    note.replaceChildren(`${plural(added, 'device')} added to Inventory. `,
                         el('a', {href: INVENTORY_URL, text: 'View in Inventory'}));
    const refused = answer.results.length - added;
    if (refused) $('discAnnounce').textContent = `${plural(refused, 'device was', 'devices were')} not added - see the cards.`;
    for (const c of cards.values()) refresh(c);
    renderBar();
}

// ── The page around them ───────────────────────────────────────────────────
function render() {
    const d = current;
    const running = d && !ENDED.includes(d.phase);
    $('discSetup').hidden = !!running || (!!d && !setupShown);
    $('discProgress').hidden = !d;
    if (!d) return;
    const c = d.counts, phases = {running: 'Running', paused: 'Paused', cancelling: 'Cancelling',
                                  done: 'Finished', cancelled: 'Cancelled'};
    $('discPhase').textContent = phases[d.phase] || d.phase;
    $('discPhase').className = 'disc-phase is-' + d.phase;
    $('discWhat').textContent = `${d.entries.join(', ')} · port ${d.port} · ${d.profile} · started by ${d.by}`;
    $('discCancelBtn').hidden = !(d.phase === 'running' || d.phase === 'paused');
    $('discAgainBtn').hidden = !!running || setupShown;
    $('discPaused').hidden = d.phase !== 'paused';
    const linesTotal = Object.values(c.reasons).reduce((a, b) => a + b, 0);
    $('cAddresses').textContent = NF.format(c.addresses);
    $('cChecked').textContent = NF.format(c.checked);
    $('cOpen').textContent = NF.format(c.open);
    $('cSigning').textContent = NF.format(c.signing_in);
    $('cFound').textContent = NF.format(c.found);
    $('cLines').textContent = NF.format(linesTotal);
    const pct = c.addresses ? Math.round(100 * c.checked / c.addresses) : 0;
    $('discBar').setAttribute('aria-valuenow', String(pct));
    $('discBarFill').style.width = pct + '%';
    let note = '';
    if (d.phase === 'cancelling') note = 'Cancelled: the sign-ins under way finish, nothing more is asked.';
    else if (d.phase === 'cancelled') note = `Cancelled by ${d.cancelled_by}.`;
    if (!d.mine) note = (note ? note + ' ' : '') + `Only ${d.by} can add devices from this discovery.`;
    $('discNotMine').hidden = !note;
    $('discNotMine').textContent = note;
    $('discFound').hidden = !cards.size;
    $('discCards').hidden = tableMode;
    $('discTableWrap').hidden = !tableMode;
    $('discBulk').hidden = !tableMode;
    renderLines();
    renderBar();
}

function renderLines() {
    $('discLines').hidden = !lines.length;
    const groups = new Map();
    for (const line of lines) {
        if (!groups.has(line.reason)) groups.set(line.reason, []);
        groups.get(line.reason).push(line);
    }
    const host = $('discLineGroups');
    for (const [reason, items] of groups) {
        let group = host.querySelector(`details[data-reason="${reason}"]`);
        if (!group) {
            group = el('details', {class: 'disc-line-group', 'data-reason': reason},
                       el('summary', {}), el('ul', {class: 'disc-line-list'}));
            host.append(group);
        }
        group.querySelector('summary').textContent = `${REASONS[reason] || reason} (${NF.format(items.length)})`;
        const list = group.querySelector('ul');
        for (const line of items.slice(list.children.length))
            list.append(el('li', {}, el('span', {class: 'nr-mono', text: line.address}),
                           line.detail ? ` - ${line.detail}` : ''));
    }
}

function renderBar() {
    const d = current;
    const bar = $('discActionbar');
    bar.hidden = !d || !d.mine || !cards.size;
    if (bar.hidden) return;
    const all = [...cards.values()];
    const count = all.filter(ready).length;
    const waiting = all.filter(c => !c.created && !c.out && !ready(c)).length;
    $('discReadyText').textContent = `${plural(count, 'device')} ready` +
        (waiting ? ` · ${plural(waiting, 'card needs', 'cards need')} a label or platform` : '');
    const btn = $('discAddBtn');
    btn.disabled = !count;
    btn.textContent = count ? `Add ${plural(count, 'device')}` : 'Add devices';
}

async function poll() {
    clearTimeout(pollTimer);
    const answer = await call('GET', `/inventory/discover/status?since=${seq}`);
    if (answer.ok) apply(answer.discovery);
    if (!answer.ok || (current && !ENDED.includes(current.phase)))
        pollTimer = setTimeout(poll, POLL_MS);
}

async function control(path) {
    const answer = await call('POST', path);
    if (!answer.ok) nrAlert(answer.message, 'Not done');
    poll();
}

// ── Start ──────────────────────────────────────────────────────────────────
$('discConfirmModal').addEventListener('shown.bs.modal', () => $('discConfirmCancel').focus());
(() => {
    const initial = nrData('nr-discovery');
    checkTyped();
    if (initial) { apply(initial); if (!ENDED.includes(initial.phase)) pollTimer = setTimeout(poll, POLL_MS); }
    else render();
})();

nrHandlers({
    discAddressesTyped: () => { checkTyped(); },
    discStart: () => { startClicked(); return false; },
    discConfirmed: () => { confirmed(); },
    discCancel: () => { control('/inventory/discover/cancel'); },
    discContinue: () => { control('/inventory/discover/continue'); },
    discAgain: () => { setupShown = true; render(); $('discAddresses').focus(); },
    discCardEdit: el => { edited(el); },
    discLeaveOut: el => { leaveOut(el); },
    discSelectNeeding: () => { bulk('needing'); },
    discSetPlatform: () => { bulk('platform'); },
    discLeaveOutSelected: () => { bulk('out'); },
    discAdd: () => { add(); },
});
