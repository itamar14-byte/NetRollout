// Server Management's SSH Host Keys card - templates/server_management.html.
// Every target's keys (GET /admin/server/host-keys), a refused one waiting
// first: Accept (replacing the old key of its type - a replaced device),
// Accept as HA unit (keeping both), Remove a key, Forget a target's keys.
// Each action answers with the card's new state.
(() => {
    const list = document.getElementById('hkList');
    if (!list) return;
    let targets = [];

    function el(tag, props = {}, ...children) {
        const node = document.createElement(tag);
        for (const [k, v] of Object.entries(props)) {
            if (v === null || v === undefined || v === false) continue;
            if (k === 'class') node.className = v;
            else if (k === 'text') node.textContent = v;
            else if (k.startsWith('data-') || k.startsWith('aria-')) node.setAttribute(k, v);
            else node[k] = v;
        }
        for (const child of children) if (child !== null && child !== undefined) node.append(child);
        return node;
    }

    const when = iso => iso ? iso.replace('T', ' ').slice(0, 16) : '';

    function show(state) {
        targets = state.targets;
        document.getElementById('hkOff').hidden = state.on;
        draw();
    }

    function draw() {
        const query = document.getElementById('hkSearch').value.trim().toLowerCase();
        const shown = targets.filter(t => !query || t.target.toLowerCase().includes(query)
            || t.devices.some(d => d.toLowerCase().includes(query)));
        const refused = targets.filter(t => t.keys.some(k => k.state === 'refused')).length;
        document.getElementById('hkCount').textContent = targets.length
            ? `${targets.length} device address${targets.length === 1 ? '' : 'es'}` +
              (refused ? ` · ${refused} with a refused key` : '') : '';
        list.replaceChildren();
        if (!targets.length) {
            list.append(el('p', {class: 'hk-empty', text: 'No host keys stored yet.'}));
            return;
        }
        if (!shown.length) list.append(el('p', {class: 'hk-empty', text: 'Nothing matches.'}));
        for (const t of shown) list.append(target(t));
    }

    function target(t) {
        const waiting = t.keys.some(k => k.state === 'refused');
        const box = el('section', {class: 'hk-target' + (waiting ? ' is-refused' : ''), 'aria-label': t.target});
        box.append(el('div', {class: 'hk-target-head'},
            el('span', {class: 'hk-target-name', text: t.target}),
            el('span', {class: 'hk-devices', text: t.devices.length ? t.devices.join(', ') : 'No device in Inventory'}),
            el('button', {type: 'button', class: 'btn btn-outline-secondary btn-sm', text: 'Forget all',
                          'aria-label': `Forget every key of ${t.target}`,
                          'data-nr-click': 'hkReset', 'data-target': t.target})));
        for (const k of t.keys) box.append(key(t, k));
        return box;
    }

    function key(t, k) {
        const refused = k.state === 'refused';
        const meta = refused
            ? `refused since ${when(k.first_seen)}, last ${when(k.last_seen)} (${k.seen_by})`
            : `first seen ${when(k.first_seen)} (${k.seen_by})` +
              (k.accepted_by ? `, accepted by ${k.accepted_by} ${when(k.accepted_at)}` : '') +
              `, last ${when(k.last_seen)}`;
        const actions = el('div', {class: 'hk-actions'});
        if (refused) {
            actions.append(
                el('button', {type: 'button', class: 'btn btn-warning btn-sm', text: 'Accept',
                              'aria-label': `Accept ${k.fingerprint} for ${t.target}, replacing its old ${k.key_type} key`,
                              'data-nr-click': 'hkAccept', 'data-key': k.id, 'data-keep': '0',
                              'data-target': t.target, 'data-fp': k.fingerprint}),
                el('button', {type: 'button', class: 'btn btn-outline-secondary btn-sm', text: 'Accept as HA unit',
                              'aria-label': `Accept ${k.fingerprint} for ${t.target} as the other unit of an HA pair`,
                              'data-nr-click': 'hkAccept', 'data-key': k.id, 'data-keep': '1',
                              'data-target': t.target, 'data-fp': k.fingerprint}));
        }
        actions.append(el('button', {type: 'button', class: 'btn btn-outline-secondary btn-sm', text: 'Remove',
                                     'aria-label': `Remove ${k.fingerprint} of ${t.target}`,
                                     'data-nr-click': 'hkRemove', 'data-key': k.id, 'data-fp': k.fingerprint}));
        return el('div', {class: 'hk-key'},
            el('span', {class: 'hk-state ' + (refused ? 'is-refused' : 'is-accepted'), text: refused ? 'Refused' : 'Accepted'}),
            el('span', {class: 'nr-mono hk-meta', text: k.key_type}),
            el('span', {class: 'hk-fp', text: k.fingerprint}),
            el('span', {class: 'hk-meta', text: meta}),
            actions);
    }

    function result(message, ok) {
        const box = document.getElementById('hkResult');
        box.className = 'cert-result show ' + (ok ? 'success' : 'error');
        box.textContent = message;
    }

    async function call(url, body) {
        let resp, data = {};
        try {
            resp = await fetch(url, {method: body === undefined ? 'GET' : 'POST', cache: 'no-store',
                headers: {'X-CSRFToken': CSRF_TOKEN, 'Content-Type': 'application/json'},
                body: body === undefined ? undefined : JSON.stringify(body)});
            data = await resp.json();
        } catch (e) { result("NetRollout didn't answer - try again.", false); return; }
        if (!resp.ok) { result(data.message || `Failed (${resp.status})`, false); return; }
        if (data.message) result(data.message, true);
        show(data);
    }

    async function accept(button) {
        const keep = button.dataset.keep === '1';
        const message = keep
            ? `Accept ${button.dataset.fp} as the other unit of an HA pair at ${button.dataset.target}? Both keys stay accepted.`
            : `Accept ${button.dataset.fp} for ${button.dataset.target}? Its old key of this type is removed - do this ` +
              'after a device was replaced, once you know the new key is the device\'s.';
        if (!await nrConfirm({title: 'Accept this host key?', message, confirmLabel: 'Accept'})) return;
        call(`/admin/server/host-keys/${button.dataset.key}/accept`, {keep_others: keep});
    }

    async function remove(button) {
        if (!await nrConfirm({title: 'Remove this host key?', danger: true, confirmLabel: 'Remove',
                              message: `Remove ${button.dataset.fp}? A device showing it is then refused (or, with ` +
                                       'no key left, its next connection stores the key it shows).'})) return;
        call(`/admin/server/host-keys/${button.dataset.key}/remove`, {});
    }

    async function reset(button) {
        if (!await nrConfirm({title: 'Forget these host keys?', danger: true, confirmLabel: 'Forget',
                              message: `Forget every key of ${button.dataset.target}? Its next connection stores ` +
                                       'whatever key it shows.'})) return;
        call('/admin/server/host-keys/reset', {target: button.dataset.target});
    }

    call('/admin/server/host-keys');

    nrHandlers({
        hkFilter: () => { draw(); },
        hkAccept: el => { accept(el); },
        hkRemove: el => { remove(el); },
        hkReset: el => { reset(el); },
    });
})();
