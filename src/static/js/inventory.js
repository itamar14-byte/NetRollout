// Inventory - see templates/inventory.html.
const VENDOR_LOGOS   = nrData('nr-vendor-logos');   // the platform picker's selected logo
const PROP_DEFS      = nrData('nr-prop-defs');
// System Settings' DNS suffix: a bare name is shown as it's reached (src/rollout/address.py qualify)
const DNS_SUFFIX     = nrData('nr-dns-suffix');
// the device's own (shared) properties; every other attribute is the user's own
const SYS_PROP_NAMES = new Set(nrData('nr-sys-prop-names'));
const QCM_LIST_PROPS = new Set(nrData('nr-list-props'));

// ════════════════════════════════════════
//  NrSelect - custom dropdown widget
// ════════════════════════════════════════
function initNrSelect(containerId, onChange) {
    const container = document.getElementById(containerId);
    if (!container) return null;

    const hidden  = container.querySelector('input[type="hidden"]');
    const display = container.querySelector('.nr-select-display');
    const list    = container.querySelector('.nr-select-list');
    const shell   = nrSelectShell(container, filterOptions, container.closest('.modal'));

    function liveOptions() { return container.querySelectorAll('.nr-select-option'); }

    function renderDisplay(label) {
        if (!label) {
            display.innerHTML = '<span class="nr-select-placeholder">No profile assigned</span>';
        } else {
            display.innerHTML =
                '<i class="bi bi-shield-lock opt-shield" style="font-size:0.82rem;"></i>' +
                '<span class="nr-select-display-label">' +
                nrEscape(label) +
                '</span>';
        }
    }

    function selectOption(value, label) {
        hidden.value = value;
        renderDisplay(label || '');
        liveOptions().forEach(o => o.classList.toggle('selected', o.dataset.value === value));
        shell.close();
        if (onChange) onChange(value);
    }

    function filterOptions(q) {
        const lq = q.toLowerCase();
        liveOptions().forEach(o => {
            const match = !q
                || o.classList.contains('none-opt')
                || (o.dataset.label || '').toLowerCase().includes(lq);
            o.style.display = match ? '' : 'none';
        });
    }

    function wireOption(opt) {
        shell.wire(opt, o => selectOption(o.dataset.value, o.dataset.label));
    }

    liveOptions().forEach(wireOption);
    shell.listen();

    return {
        getValue: () => hidden.value,
        setValue(value, label) { selectOption(value || '', label || ''); },
        reset()               { selectOption('', ''); },
        addOption(value, label) {
            const opt = document.createElement('div');
            opt.className = 'nr-select-option';
            opt.dataset.value = value;
            opt.dataset.label = label;
            opt.innerHTML =
                '<i class="bi bi-shield-lock opt-shield"></i>' +
                '<span class="opt-label">' + nrEscape(label) + '</span>' +
                '<i class="bi bi-check2 opt-check"></i>';
            list.appendChild(opt);
            wireOption(opt);
        }
    };
}

// Init security profile selects
const addSecSelect  = initNrSelect('addSecSelect',  () => { resetSignIn('add'); resetDetect('add'); });
let   editSecSelect = initNrSelect('editSecSelect', () => { resetSignIn('edit'); resetDetect('edit'); });

// ════════════════════════════════════════
//  Test sign-in - the selected profile on what the window holds now
// ════════════════════════════════════════
// Shown while a profile is selected; tries an SSH sign-in with it at the
// window's address, port and platform, saved or not (/security/<id>/test).
function signInParts(which) {
    return {
        profile: (which === 'add' ? addSecSelect : editSecSelect).getValue(),
        box:     document.getElementById(which + 'SigninTest'),
        btn:     document.getElementById(which + 'SigninBtn'),
        status:  document.getElementById(which + 'SigninStatus'),
        ip:      document.getElementById(which + 'DevIp').value.trim(),
        port:    document.getElementById(which + 'DevPort').value.trim(),
        type:    document.getElementById(which + 'DevTypeHidden').value,
    };
}

function showSignIn(status, ok, message) {
    status.style.display = '';
    status.className = 'conn-status ' + (ok ? 'conn-ok' : 'conn-fail');
    status.innerHTML = '<i class="bi bi-' + (ok ? 'check' : 'x') + '-circle-fill"></i>' + nrEscape(message);
}

function resetSignIn(which) {
    const box = document.getElementById(which + 'SigninTest');
    if (!box) return;
    const parts = signInParts(which);
    box.hidden = !parts.profile;
    parts.status.style.display = 'none';
    parts.status.textContent = '';
}

async function testSignIn(which) {
    const parts = signInParts(which);
    if (!parts.profile) return;
    if (!parts.ip) {
        showSignIn(parts.status, false, "Enter the device's IP address or hostname first");
        return;
    }
    parts.btn.disabled = true;
    parts.btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Signing in…';
    parts.status.style.display = 'none';
    try {
        const resp = await fetch('/security/' + encodeURIComponent(parts.profile) + '/test', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
            body:    JSON.stringify({ ip: parts.ip, port: parts.port, device_type: parts.type })
        });
        const data = await resp.json();
        showSignIn(parts.status, data.status === 'ok', data.message || 'The test failed');
    } catch (_) {
        showSignIn(parts.status, false, 'Network error');
    }
    parts.btn.disabled = false;
    parts.btn.innerHTML = '<i class="bi bi-key me-1"></i>Test sign-in';
}
['add', 'edit'].forEach(which => {
    ['DevIp', 'DevPort'].forEach(field => document.getElementById(which + field)
        .addEventListener('input', () => { resetSignIn(which); resetDetect(which); }));
});

// ════════════════════════════════════════
//  Detect - the platform, asked of the device itself
// ════════════════════════════════════════
// Beside the platform field: signs in with the selected profile at the
// window's address and port (/security/<id>/detect) and fills the platform
// in - and an empty label with the device's hostname. Greyed (aria-disabled,
// the reason in its tooltip) until the address, the port and a profile are
// there; nothing is sent then. Not in the read-only window (.edit-only).
const DETECT_IDLE = '<i class="bi bi-search me-1" aria-hidden="true"></i>Detect';

function detectMissing(which) {
    const parts = signInParts(which);
    const port = Number(parts.port);
    if (!parts.ip) return "Enter the device's address first";
    if (!Number.isInteger(port) || port < 1 || port > 65535) return 'Enter the SSH port first';
    if (!parts.profile) return 'Choose your security profile first - Detect signs in with it';
    return '';
}

function updateDetect(which) {
    const btn = document.getElementById(which + 'DetectBtn');
    if (!btn || btn.dataset.busy) return;
    const missing = detectMissing(which);
    btn.setAttribute('aria-disabled', missing ? 'true' : 'false');
    btn.title = missing || 'Sign in with your security profile and detect the platform';
}

function resetDetect(which) {
    const status = document.getElementById(which + 'DetectStatus');
    if (status) { status.style.display = 'none'; status.textContent = ''; }
    updateDetect(which);
}

async function detectPlatform(which) {
    const btn = document.getElementById(which + 'DetectBtn');
    if (btn.getAttribute('aria-disabled') === 'true' || btn.dataset.busy) return;
    const parts  = signInParts(which);
    const status = document.getElementById(which + 'DetectStatus');
    btn.dataset.busy = '1';
    btn.setAttribute('aria-disabled', 'true');
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span>Detecting…';
    status.style.display = 'none';
    try {
        const resp = await fetch('/security/' + encodeURIComponent(parts.profile) + '/detect', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
            body:    JSON.stringify({ ip: parts.ip, port: parts.port })
        });
        const data = await resp.json();
        if (data.status !== 'ok') {
            showSignIn(status, false, data.message || 'Detection failed');
        } else if (data.platform) {
            (which === 'add' ? addPlatformSel : editPlatformSel).setValue(data.platform);
            const label = document.getElementById(which + 'DevLabel');
            if (data.hostname && !label.value.trim()) label.value = data.hostname;
            showSignIn(status, true, 'Detected: ' + (PLATFORM_LABELS[data.platform] || data.platform)
                + (data.evidence ? ' - ' + data.evidence : ''));
        } else if (data.detected) {
            showSignIn(status, false, 'Detected ' + (data.detected_name || data.detected)
                + ", which NetRollout doesn't support - choose the platform.");
        } else {
            showSignIn(status, false, data.problem ? data.problem + ' - choose the platform.'
                : "Couldn't detect the platform - choose it.");
        }
    } catch (_) {
        showSignIn(status, false, 'Network error');
    }
    delete btn.dataset.busy;
    btn.innerHTML = DETECT_IDLE;
    updateDetect(which);
}

// ════════════════════════════════════════
//  Platform NrSelect
// ════════════════════════════════════════
const PLATFORM_LABELS = nrData('nr-platform-labels');

function initPlatformSelect(containerId, hiddenId, errorId) {
    const container = document.getElementById(containerId);
    const hidden    = document.getElementById(hiddenId);
    const errorEl   = document.getElementById(errorId);
    if (!container || !hidden) return null;

    const trigger = container.querySelector('.nr-select-trigger');
    const options = container.querySelectorAll('.nr-select-option');
    const display = container.querySelector('.nr-select-display');
    const shell   = nrSelectShell(container, filterOptions, container.closest('.modal'));

    function renderDisplay(value, label) {
        if (!value) {
            display.innerHTML = '<span class="nr-select-placeholder">Select platform…</span>';
        } else {
            const logoUrl = (typeof VENDOR_LOGOS !== 'undefined') ? (VENDOR_LOGOS[value] || '') : '';
            const logoHtml = logoUrl
                ? `<img alt="" src="${nrEscape(logoUrl)}" width="16" height="16"
                        style="border-radius:3px;opacity:0.85;flex-shrink:0;"
                        data-nr-error="nrHide">`
                : '';
            display.innerHTML =
                logoHtml +
                `<span class="nr-select-display-label">${nrEscape(label)}</span>`;
        }
    }

    function selectOption(value, label) {
        hidden.value = value;
        renderDisplay(value, label);
        options.forEach(o => o.classList.toggle('selected', o.dataset.value === value));
        container.classList.remove('nr-platform-error');
        if (errorEl) errorEl.style.display = 'none';
        shell.close();
    }

    function filterOptions(q) {
        const lq = q.toLowerCase();
        options.forEach(o => {
            o.style.display = (!q || (o.dataset.label||'').toLowerCase().includes(lq)) ? '' : 'none';
        });
    }

    options.forEach(opt => shell.wire(opt, o => selectOption(o.dataset.value, o.dataset.label)));
    shell.listen();

    return {
        getValue: ()            => hidden.value,
        setValue: (value)       => selectOption(value, PLATFORM_LABELS[value] || value),
        reset:    ()            => selectOption('', ''),
        markError: ()           => {
            container.classList.add('nr-platform-error');
            if (errorEl) errorEl.style.display = 'block';
            trigger.style.animation = 'none';
            void trigger.offsetHeight;
            trigger.style.animation = 'shake 0.4s ease';
            trigger.addEventListener('animationend', () => { trigger.style.animation = ''; }, { once: true });
        }
    };
}

// An IPv4 or IPv6 address, or a hostname (the server checks it too:
// src/rollout/address.py). IPv6 through the browser's own URL parser:
// [address] is a valid host only for a valid one.
function isAddress(v) {
    if (/^((25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(25[0-5]|2[0-4]\d|[01]?\d\d?)$/.test(v)) return true;
    if (!v.includes(':') || /[\[\]/%]/.test(v)) return isHostname(v);
    try { new URL('http://[' + v + ']/'); return true; } catch (e) { return false; }
}
// Labels of letters, digits and hyphens (1-63, no hyphen first or last),
// 253 at most, a trailing dot allowed; an all-digits last label is a
// mistyped IP (10.0.0), not a name
function isHostname(v) {
    const name = v.endsWith('.') ? v.slice(0, -1) : v;
    if (!name || name.length > 253) return false;
    const labels = name.split('.');
    return labels.every(l => /^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$/i.test(l))
        && !/^\d+$/.test(labels[labels.length - 1]);
}
// The IP fields take part in the forms' own validation (form.checkValidity):
// checked as typed, when a script fills one in (openEdit: no input event, so
// the empty field's verdict from page load would stay), and again just
// before a submit
function checkIpField(field) {
    field.setCustomValidity(
        isAddress(field.value.trim()) ? '' : 'Enter an IPv4 or IPv6 address, or a hostname.');
}
['addDevIp', 'editDevIp'].forEach(id => {
    const field = document.getElementById(id);
    if (!field) return;
    const check = () => checkIpField(field);
    field.addEventListener('input', check);
    field.form.addEventListener('submit', check, true);
    check();
});

const addPlatformSel  = initPlatformSelect('addPlatformSelect',  'addDevTypeHidden',  'addPlatformError');
let   editPlatformSel = initPlatformSelect('editPlatformSelect', 'editDevTypeHidden', 'editPlatformError');

// Validate platform on form submit
document.getElementById('addDeviceModal').querySelector('form')
    .addEventListener('submit', e => {
        if (!addPlatformSel.getValue()) {
            e.preventDefault();
            addPlatformSel.markError();
        }
    });

// ════════════════════════════════════════
//  Connection test - Add Device modal
// ════════════════════════════════════════
function resetAddTestBtn() {
    const btn    = document.getElementById('addTestBtn');
    const status = document.getElementById('addTestStatus');
    btn.type      = 'button';
    btn.className = 'btn btn-secondary btn-sm';
    btn.innerHTML = '<i class="bi bi-plug me-1"></i>Test Connection';
    btn.disabled  = false;
    btn.onclick   = testAddConnection;
    status.style.display = 'none';
    status.className     = 'conn-status';
    status.textContent   = '';
}

// Add's Test Connection: an onclick property, which the test's outcome
// takes away (the button turns into the form's submit) and gives back
document.getElementById('addTestBtn').onclick = testAddConnection;

async function testAddConnection() {
    const ipEl   = document.getElementById('addDevIp');
    const portEl = document.getElementById('addDevPort');
    const ip     = ipEl.value.trim();
    const port   = portEl.value.trim();
    const btn    = document.getElementById('addTestBtn');
    const status = document.getElementById('addTestStatus');

    // Client-side validation before sending
    if (!isAddress(ip)) {
        ipEl.classList.add('is-invalid');
        return;
    }
    const portNum = parseInt(port, 10);
    if (!port || isNaN(portNum) || portNum < 1 || portNum > 65535) {
        portEl.classList.add('is-invalid');
        return;
    }

    // Testing state
    btn.disabled  = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Testing…';
    status.style.display = 'none';

    try {
        const resp = await fetch('/inventory/test_connection', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
            body:    JSON.stringify({ ip, port: portNum })
        });
        const data = await resp.json();

        status.style.display = '';
        if (data.status === 'ok') {
            status.className  = 'conn-status conn-ok';
            status.innerHTML  = '<i class="bi bi-check-circle-fill"></i>' + nrEscape(data.message);
            btn.type      = 'submit';
            btn.className = 'btn btn-success btn-sm';
            btn.innerHTML = '<i class="bi bi-check-lg me-1"></i>Confirm';
            btn.disabled  = false;
            btn.onclick   = null;
        } else {
            status.className  = 'conn-status conn-fail';
            status.innerHTML  = '<i class="bi bi-x-circle-fill"></i>' + nrEscape(data.message);
            btn.type      = 'submit';
            btn.className = 'btn btn-danger btn-sm';
            btn.innerHTML = '<i class="bi bi-exclamation-triangle me-1"></i>Save Anyway';
            btn.disabled  = false;
            btn.onclick   = null;
        }
    } catch (_) {
        status.style.display = '';
        status.className  = 'conn-status conn-fail';
        status.innerHTML  = '<i class="bi bi-x-circle-fill"></i>Network error';
        btn.disabled  = false;
        btn.innerHTML = '<i class="bi bi-plug me-1"></i>Test Connection';
        btn.onclick   = testAddConnection;
    }
}

// Reset test state when IP or port changes after a test
document.getElementById('addDevIp').addEventListener('input',   resetAddTestBtn);
document.getElementById('addDevPort').addEventListener('input',  resetAddTestBtn);

// Editing an existing device: Save is always available, the test is advisory
// and only reports status (unlike Add, where the test gates the submit).
function resetEditTestBtn() {
    const btn    = document.getElementById('editTestBtn');
    const status = document.getElementById('editTestStatus');
    btn.innerHTML = '<i class="bi bi-plug me-1"></i>Test Connection';
    btn.disabled  = false;
    status.style.display = 'none';
    status.className     = 'conn-status';
    status.textContent   = '';
}

async function testEditConnection() {
    const ipEl   = document.getElementById('editDevIp');
    const portEl = document.getElementById('editDevPort');
    const ip     = ipEl.value.trim();
    const port   = portEl.value.trim();
    const btn    = document.getElementById('editTestBtn');
    const status = document.getElementById('editTestStatus');

    if (!isAddress(ip)) { ipEl.classList.add('is-invalid'); return; }
    const portNum = parseInt(port, 10);
    if (!port || isNaN(portNum) || portNum < 1 || portNum > 65535) {
        portEl.classList.add('is-invalid'); return;
    }

    btn.disabled  = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Testing…';
    status.style.display = 'none';

    try {
        const resp = await fetch('/inventory/test_connection', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
            body:    JSON.stringify({ ip, port: portNum })
        });
        const data = await resp.json();
        const ok = data.status === 'ok';
        status.style.display = '';
        status.className  = 'conn-status ' + (ok ? 'conn-ok' : 'conn-fail');
        status.innerHTML  = '<i class="bi bi-' + (ok ? 'check' : 'x') + '-circle-fill"></i>' + nrEscape(data.message);
    } catch (_) {
        status.style.display = '';
        status.className  = 'conn-status conn-fail';
        status.innerHTML  = '<i class="bi bi-x-circle-fill"></i>Network error';
    }
    btn.disabled  = false;
    btn.innerHTML = '<i class="bi bi-plug me-1"></i>Test Connection';
}

document.getElementById('editDevIp').addEventListener('input',  resetEditTestBtn);
document.getElementById('editDevPort').addEventListener('input', resetEditTestBtn);
document.getElementById('editDeviceModal').querySelector('form')
    .addEventListener('submit', e => {
        if (!editPlatformSel.getValue()) {
            e.preventDefault();
            editPlatformSel.markError();
        }
    });

// Unticking Global on a global device deletes the other users' links, values
// and bindings on it (InventoryView.save_device): say so, and Save turns into
// a red Confirm until it's ticked again
let editWasGlobal = false;
function updatePrivateWarning() {
    const toggle = document.getElementById('editIsGlobal');
    if (!toggle) return;
    const losing = editWasGlobal && !toggle.checked;
    const btn = document.getElementById('editSaveBtn');
    document.getElementById('editPrivateWarning').hidden = !losing;
    btn.className = 'btn btn-sm edit-only ' + (losing ? 'btn-danger' : 'btn-primary');
    btn.innerHTML = losing
        ? '<i class="bi bi-exclamation-triangle me-1"></i>Confirm'
        : '<i class="bi bi-check-lg me-1"></i>Save';
}
document.getElementById('editIsGlobal')?.addEventListener('change', updatePrivateWarning);



// ════════════════════════════════════════
//  Tooltip
// ════════════════════════════════════════
const tooltip = document.getElementById('invTooltip');
let   ttTimer = null;

function showTooltip(card, device) {
    // a hostname as it's reached (a bare one with the DNS suffix) and its
    // address: the reachability check's on this page (at most a minute old),
    // else the stored one - core1.corp.local (10.0.0.7)
    const live = REACH[device.id], resolved = (live && live.address) || device.resolved_ip;
    const suffix = DNS_SUFFIX.replace(/^\.+|\.+$/g, '');
    const shown = suffix && !/[.:]/.test(device.ip) ? device.ip + '.' + suffix : device.ip;
    document.getElementById('ttIp').textContent      =
        resolved ? shown + ' (' + resolved + ')' : shown;
    document.getElementById('ttType').textContent    = device.device_type;
    document.getElementById('ttPort').textContent    = device.port;
    document.getElementById('ttProfile').textContent = device.sec_profile_label || '-';
    const reach = REACH[device.id];
    document.getElementById('ttReach').textContent = reach === undefined
        ? 'Checking…'
        : (reach ? (reach.reachable ? 'Reachable' : 'Not reachable') + nrReachWhy(reach) +
                   ' · checked ' + nrAgo(reach.checked_at)
                 : 'Unknown');

    const assignedMappings = device.assigned_mappings || [];
    const mappingsRow = document.getElementById('ttMappingsRow');
    const ttMappings   = document.getElementById('ttMappings');
    if (assignedMappings.length > 0) {
        ttMappings.innerHTML = assignedMappings.map(m => `
            <div class="mapping-chip">
                <span class="mapping-chip-token">${nrEscape(m.token)}</span>
                <span class="mapping-chip-prop">→ ${nrEscape(m.property)}</span>
            </div>`).join('');
        mappingsRow.style.display = '';
    } else {
        mappingsRow.style.display = 'none';
    }

    const rect = card.getBoundingClientRect();
    tooltip.style.width = Math.max(rect.width, 210) + 'px';
    tooltip.style.left  = rect.left + 'px';

    // Position below unless not enough space - use actual tooltip height
    const ttHeight = tooltip.offsetHeight || 160;
    if (window.innerHeight - rect.bottom < ttHeight + 10) {
        tooltip.style.top = (rect.top - ttHeight - 5) + 'px';
    } else {
        tooltip.style.top = (rect.bottom + 5) + 'px';
    }

    ttTimer = setTimeout(() => tooltip.classList.add('visible'), 110);
}

function hideTooltip() {
    clearTimeout(ttTimer);
    tooltip.classList.remove('visible');
}


// ════════════════════════════════════════
//  Reachability (from NetRollout, ip:port) - lazy, after first paint
// ════════════════════════════════════════
const REACH = {};  // device id -> status | null (unknown)

async function refreshReachability(refresh = false) {
    const els = [...document.querySelectorAll('[data-reach-for]')];
    const btn = document.getElementById('reachRecheck');
    if (!els.length) { btn.style.display = 'none'; return; }
    els.forEach(nrReachChecking);
    btn.disabled = true;
    const statuses = await nrCheckReachability(els.map(e => e.dataset.reachFor), refresh);
    els.forEach(el => {
        REACH[el.dataset.reachFor] = statuses[el.dataset.reachFor] || null;
        nrSetReach(el, statuses[el.dataset.reachFor]);
    });
    btn.disabled = false;
}
document.getElementById('reachRecheck').addEventListener('click',
    () => refreshReachability(true));
refreshReachability();


// ════════════════════════════════════════
//  Filters: name and IP as typed (case-insensitive, any part), platforms
//  ticked (any of them; none: all); all three together. A section with no match hides, its count follows.
// ════════════════════════════════════════
(function () {
    const nameIn = document.getElementById('filterName');
    if (!nameIn) return;                     // no devices: no filter bar
    const ipIn     = document.getElementById('filterIp');
    const platIn   = document.getElementById('filterPlatform');
    const platBoxes = [...document.querySelectorAll('#filterPlatformWrap input[type=checkbox]')];
    const plats    = () => platBoxes.filter(b => b.checked).map(b => b.value);
    const countEl  = document.getElementById('filterCount');
    const clearBtn = document.getElementById('filterClear');
    const emptyEl  = document.getElementById('filterEmpty');
    const cards = [...document.querySelectorAll('.inv-card')].map(card => {
        const d = JSON.parse(card.dataset.device);
        return { col: card.parentElement, label: (d.label || '').toLowerCase(),
                 ip: (d.ip || '').toLowerCase(), type: d.device_type };
    });

    // The filters outlive a reload in this tab (a save or delete reloads the
    // page): kept as typed in sessionStorage, restored before the first apply
    const KEPT = 'nr-inventory-filters';
    function keep() {
        try {
            const f = {name: nameIn.value, ip: ipIn.value, plats: plats()};
            if (f.name || f.ip || f.plats.length) sessionStorage.setItem(KEPT, JSON.stringify(f));
            else sessionStorage.removeItem(KEPT);
        } catch (e) { /* no storage: the filters just don't outlive a reload */ }
    }
    try {
        const f = JSON.parse(sessionStorage.getItem(KEPT) || 'null');
        if (f && !nameIn.value && !ipIn.value && !plats().length) {
            nameIn.value = f.name || ''; ipIn.value = f.ip || '';
            // a platform no device has any more isn't in the list: left out
            const kept = new Set(f.plats || []);
            platBoxes.forEach(b => { b.checked = kept.has(b.value); });
        }
    } catch (e) { /* unreadable: start unfiltered */ }

    function apply() {
        keep();
        const name = nameIn.value.trim().toLowerCase();
        const ip   = ipIn.value.trim().toLowerCase();
        const chosen = plats();
        const active = !!(name || ip || chosen.length);
        let shown = 0;
        cards.forEach(c => {
            const match = (!name || c.label.includes(name)) && (!ip || c.ip.includes(ip))
                          && (!chosen.length || chosen.includes(c.type));
            c.col.hidden = !match;
            if (match) shown++;
        });
        document.querySelectorAll('.inv-group').forEach(group => {
            const visible = [...group.querySelectorAll('.inv-card')]
                .filter(card => !card.parentElement.hidden).length;
            group.hidden = visible === 0;
            const count = group.querySelector('.nr-section-count');
            if (count) count.textContent = visible;
        });
        platIn.classList.toggle('is-set', chosen.length > 0);
        platIn.textContent = !chosen.length ? 'All platforms'
            : chosen.length === 1 ? platBoxes.find(b => b.checked).dataset.label
            : chosen.length + ' platforms';
        countEl.textContent = active ? `Showing ${shown} of ${cards.length}` : '';
        clearBtn.hidden = !active;
        emptyEl.hidden = !active || shown > 0;
    }
    function clearAll() {
        nameIn.value = ''; ipIn.value = ''; platBoxes.forEach(b => { b.checked = false; });
        apply();
        nameIn.focus();
    }
    [nameIn, ipIn].forEach(input => {
        input.addEventListener('input', apply);
        // Escape empties the box it's in (the browser's own "search" clear)
        input.addEventListener('keydown', e => {
            if (e.key === 'Escape' && input.value) { e.preventDefault(); input.value = ''; apply(); }
        });
    });
    platBoxes.forEach(b => b.addEventListener('change', apply));
    clearBtn.addEventListener('click', clearAll);
    document.getElementById('filterEmptyClear').addEventListener('click', clearAll);
    apply();   // a value the browser restored on Back
})();


// ════════════════════════════════════════
//  Card wiring
// ════════════════════════════════════════
document.querySelectorAll('.inv-card').forEach(card => {
    const device = JSON.parse(card.dataset.device);

    card.addEventListener('mouseenter', () => showTooltip(card, device));
    card.addEventListener('mouseleave', hideTooltip);
    const openBtn = card.querySelector('.inv-card-open');
    openBtn.addEventListener('focus', () => showTooltip(card, device));
    openBtn.addEventListener('blur', hideTooltip);

    card.addEventListener('click', () => {
        hideTooltip();
        openEdit(device);
    });

    const deleteBtn = card.querySelector('.card-delete-btn');
    if (deleteBtn) {
        deleteBtn.addEventListener('click', e => {
            e.stopPropagation();
            hideTooltip();
            openDelete(device);
        });
    }
});


// ════════════════════════════════════════
//  Edit modal
// ════════════════════════════════════════
function openEdit(device) {
    document.getElementById('editDevTitle').textContent = device.label;
    document.getElementById('editDevLabel').value       = device.label;
    document.getElementById('editDevIp').value          = device.ip;
    checkIpField(document.getElementById('editDevIp'));
    document.getElementById('editDevPort').value        = device.port;
    if (editPlatformSel) editPlatformSel.setValue(device.device_type);

    // Global devices the viewer can't edit open read-only: every field is
    // disabled except the mapping multi-select and the viewer's own (custom)
    // attributes, which post to /attributes. A disabled field isn't sent.
    const readonly = !device.editable;
    const modalEl  = document.getElementById('editDeviceModal');
    modalEl.classList.toggle('is-readonly', readonly);
    modalEl.querySelectorAll('#editDevLabel, #editDevIp, #editDevPort')
        .forEach(el => { el.disabled = readonly; });
    modalEl.querySelectorAll('[id^="attr_"]')
        .forEach(el => { el.disabled = readonly && SYS_PROP_NAMES.has(el.id.slice(5)); });
    document.getElementById('editDevForm').action =
        '/inventory/' + device.id + (readonly ? '/attributes' : '/edit');
    const editIsGlobal = document.getElementById('editIsGlobal');
    if (editIsGlobal) editIsGlobal.checked = !!device.is_global;
    editWasGlobal = !!device.is_global;
    updatePrivateWarning();

    // Reset expand section
    document.getElementById('varAttrToggle').classList.remove('open');
    document.getElementById('varAttrBody').classList.remove('open');

    // Populate optional attributes
    const vm = device.var_maps || {};
    PROP_DEFS.forEach(p => {
        const el = document.getElementById('attr_' + p.name);
        if (!el) return;
        el.value = p.is_list ? (vm[p.name] || []).join(', ') : (vm[p.name] || '');
    });

    if (editSecSelect) {
        editSecSelect.setValue(device.sec_profile_id, device.sec_profile_label);
    }

    // Populate mapping checkboxes
    const assignedIds = new Set((device.assigned_mappings || []).map(m => m.id));
    document.querySelectorAll('.edit-mapping-cb').forEach(cb => {
        cb.checked = assignedIds.has(cb.value);
    });

    resetDetect('edit');
    new bootstrap.Modal(document.getElementById('editDeviceModal')).show();
}

// Mapping search filter
const mappingSearch = document.getElementById('editMappingSearch');
if (mappingSearch) {
    mappingSearch.addEventListener('input', function() {
        const q = this.value.toLowerCase();
        document.querySelectorAll('.mapping-opt').forEach(opt => {
            opt.style.display = opt.dataset.token.toLowerCase().includes(q) ? '' : 'none';
        });
    });
}

document.getElementById('varAttrToggle').addEventListener('click', () => {
    document.getElementById('varAttrToggle').classList.toggle('open');
    document.getElementById('varAttrBody').classList.toggle('open');
});


// ════════════════════════════════════════
//  Delete modal
// ════════════════════════════════════════
function openDelete(device) {
    document.getElementById('deleteDevName').textContent = device.label;
    document.getElementById('deleteDevForm').action = '/inventory/' + device.id + '/delete';
    new bootstrap.Modal(document.getElementById('deleteDeviceModal')).show();
}


// ════════════════════════════════════════
//  Edit modal - reset search on close
// ════════════════════════════════════════
document.getElementById('editDeviceModal').addEventListener('hidden.bs.modal', () => {
    const search = document.getElementById('editMappingSearch');
    if (search) {
        search.value = '';
        document.querySelectorAll('.mapping-opt').forEach(o => o.style.display = '');
    }
    const form = document.getElementById('editDeviceModal').querySelector('form');
    form.querySelectorAll('.nr-submitted').forEach(el => el.classList.remove('nr-submitted'));
    form.querySelectorAll('.field-error').forEach(el => el.style.display = '');
    resetEditTestBtn();
    resetDetect('edit');
});

// ════════════════════════════════════════
//  Quick Create - Security Profile
// ════════════════════════════════════════
let _quickProfileTarget = 'add';

function openQuickCreateProfile(target) {
    _quickProfileTarget = target;
    ['qcpLabel','qcpUsername','qcpPassword','qcpSecret'].forEach(id => {
        document.getElementById(id).value = '';
    });
    const err = document.getElementById('qcpError');
    err.style.display = 'none';
    err.textContent = '';
    bootstrap.Modal.getOrCreateInstance(
        document.getElementById('quickCreateProfileModal')).show();
}

async function submitQuickCreateProfile() {
    const label    = document.getElementById('qcpLabel').value.trim();
    const username = document.getElementById('qcpUsername').value.trim();
    const password = document.getElementById('qcpPassword').value;
    const secret   = document.getElementById('qcpSecret').value.trim();
    const errEl    = document.getElementById('qcpError');

    if (!username || !password) {
        errEl.textContent = 'Username and password are required.';
        errEl.style.display = '';
        return;
    }

    const resp = await fetch('/security/quick_create', {
        method:  'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
        body:    JSON.stringify({label, username, password, enable_secret: secret || null})
    });
    const data = await resp.json();

    if (data.status !== 'ok') {
        errEl.textContent = data.message || 'Failed to create profile.';
        errEl.style.display = '';
        return;
    }

    // Inject new option into both selects
    [addSecSelect, editSecSelect].forEach(sel => { if (sel) sel.addOption(data.id, data.label); });
    // Auto-select in the triggering modal
    const target = _quickProfileTarget === 'add' ? addSecSelect : editSecSelect;
    if (target) target.setValue(data.id, data.label);

    bootstrap.Modal.getOrCreateInstance(
        document.getElementById('quickCreateProfileModal')).hide();
}

// ════════════════════════════════════════
//  Quick Create - Variable Mapping
// ════════════════════════════════════════
function openQuickCreateMapping() {
    document.getElementById('qcmToken').value = '';
    document.getElementById('qcmIndex').value = '';
    document.getElementById('qcmProperty').value = 'hostname';
    document.getElementById('qcmIndexRow').style.display = 'none';
    const err = document.getElementById('qcmError');
    err.style.display = 'none';
    err.textContent = '';
    bootstrap.Modal.getOrCreateInstance(
        document.getElementById('quickCreateMappingModal')).show();
}

async function submitQuickCreateMapping() {
    const token    = document.getElementById('qcmToken').value.trim().toUpperCase();
    const property = document.getElementById('qcmProperty').value;
    const indexVal = document.getElementById('qcmIndex').value.trim();
    const errEl    = document.getElementById('qcmError');

    if (!token) {
        errEl.textContent = 'Token is required.';
        errEl.style.display = '';
        return;
    }

    const resp = await fetch('/mappings/quick_create', {
        method:  'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
        body:    JSON.stringify({
            token_inner:   token,
            property_name: property,
            index:         indexVal !== '' ? parseInt(indexVal) : null
        })
    });
    const data = await resp.json();

    if (data.status !== 'ok') {
        errEl.textContent = data.message || 'Failed to create mapping.';
        errEl.style.display = '';
        return;
    }

    // Inject new checkbox into the list, pre-checked
    const list = document.getElementById('editMappingList');
    if (list) {
        const lbl = document.createElement('label');
        lbl.className = 'mapping-opt';
        lbl.dataset.token = data.token;
        const indexSuffix = data.index !== null ? '[' + data.index + ']' : '';
        lbl.innerHTML =
            '<input type="checkbox" name="mapping_ids" value="' + nrEscape(data.id) + '"' +
            ' class="nr-check edit-mapping-cb" checked>' +
            '<span style="font-family:\'JetBrains Mono\',monospace;font-size:0.72rem;color:#00bcd4;">' +
            nrEscape(data.token) + '</span>' +
            '<span style="font-size: 0.75rem;color:var(--nr-text-muted);margin-left:4px;">→ ' +
            nrEscape(data.property_name + indexSuffix) + '</span>';
        list.appendChild(lbl);
    }

    bootstrap.Modal.getOrCreateInstance(
        document.getElementById('quickCreateMappingModal')).hide();
}

//  Add modal - reset on close
// ════════════════════════════════════════
document.getElementById('addDeviceModal').addEventListener('hidden.bs.modal', () => {
    const form = document.getElementById('addDeviceModal').querySelector('form');
    form.reset();
    document.getElementById('addDevPort').value = '22';
    if (addSecSelect)   addSecSelect.reset();
    if (addPlatformSel) addPlatformSel.reset();
    form.querySelectorAll('.nr-submitted').forEach(el => el.classList.remove('nr-submitted'));
    form.querySelectorAll('.field-error').forEach(el => el.style.display = '');
    resetAddTestBtn();
    resetDetect('add');
});

//  Escape handling - close topmost modal tier, child before parent
// ════════════════════════════════════════
let _quickCreateActive = false;

['quickCreateProfileModal', 'quickCreateMappingModal', 'quickCreatePropertyModal'].forEach(id => {
    const el = document.getElementById(id);
    el.addEventListener('show.bs.modal',   () => { _quickCreateActive = true;  });
    el.addEventListener('hidden.bs.modal', () => { _quickCreateActive = false; });
});

// Escape closes the top modal: a quick create first, else Add / Edit device.
// Document-level so focus location doesn't matter (Add / Edit device and the
// profile quick create have data-bs-keyboard="false" - only this closes them;
// the mapping and property quick creates close on Bootstrap's own Escape too,
// and then this finds them already closed).
document.addEventListener('keydown', function(e) {
    if (e.key !== 'Escape') return;
    const ids = _quickCreateActive
        ? ['quickCreateProfileModal', 'quickCreateMappingModal', 'quickCreatePropertyModal']
        : ['addDeviceModal', 'editDeviceModal'];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el.classList.contains('show'))
            bootstrap.Modal.getOrCreateInstance(el).hide();
    });
});

// ════════════════════════════════════════
//  Icon pickers
// ════════════════════════════════════════
initIconPicker('qcPropIconPreview', 'qcPropIcon');

// ════════════════════════════════════════
//  Quick Create - Property
// ════════════════════════════════════════
function openQuickCreateProperty() {
    ['qcPropName','qcPropLabel'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('qcPropIcon').value = 'bi-tag';
    document.getElementById('qcPropIconPreview').className = 'bi bi-tag';
    document.getElementById('qcPropIsList').checked = false;
    document.getElementById('qcPropError').style.display = 'none';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('quickCreatePropertyModal')).show();
}

async function submitQuickCreateProperty() {
    let name    = document.getElementById('qcPropName').value.trim();
    let label   = document.getElementById('qcPropLabel').value.trim();
    let icon    = document.getElementById('qcPropIcon').value.trim() || 'bi-tag';
    let is_list = document.getElementById('qcPropIsList').checked;
    const errEl   = document.getElementById('qcPropError');
    errEl.style.display = 'none';

    const resp = await fetch('/properties/quick_create', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
        body: JSON.stringify({name, label, icon, is_list})
    });
    const data = await resp.json();
    if (data.status !== 'ok') {
        errEl.textContent = data.message;
        errEl.style.display = '';
        return;
    }
    // what the server saved: it normalises the name (lower case, _ for spaces)
    ({name, label, icon, is_list} = data);

    // Inject new field into the custom section of varAttrBody
    let grid = document.getElementById('customPropGrid');
    if (!grid) {
        // First custom property - create the separator + grid
        const body = document.getElementById('varAttrBody');
        const sep = document.createElement('div');
        sep.style.cssText = 'border-top:1px solid #1e1e1e; margin:10px 0 8px; display:flex; align-items:center; gap:8px;';
        sep.innerHTML = '<span style="font-size: 0.6875rem; color:var(--nr-text-muted); letter-spacing:1px; text-transform:uppercase; white-space:nowrap;">Custom</span>';
        grid = document.createElement('div');
        grid.id = 'customPropGrid';
        grid.style.cssText = 'display:grid; grid-template-columns:1fr 1fr; gap:8px 12px;';
        // Insert before the "New Property" button div
        const btn = body.querySelector('div[style*="margin-top:10px"]');
        body.insertBefore(sep, btn);
        body.insertBefore(grid, btn);
    }

    // Also update QCM_LIST_PROPS if this is a list property
    if (is_list) QCM_LIST_PROPS.add(name);

    const div = document.createElement('div');
    if (is_list) div.style.gridColumn = '1/-1';
    div.innerHTML =
        `<label class="nr-label">` +
        `<i class="bi ${nrEscape(icon)}" style="margin-right:4px; color:var(--nr-text-faint);"></i>${nrEscape(label.toUpperCase())}` +
        (is_list ? ' <span style="color:var(--nr-text-muted); text-transform:none; letter-spacing:0;">(comma-separated)</span>' : '') +
        `</label>` +
        `<input type="text" class="form-control form-control-sm" id="attr_${nrEscape(name)}" name="attr_${nrEscape(name)}" autocomplete="off">`;
    grid.appendChild(div);

    // Add to global PROP_DEFS so re-opening the edit modal populates the new field
    PROP_DEFS.push({name, label, icon, is_list});

    bootstrap.Modal.getOrCreateInstance(document.getElementById('quickCreatePropertyModal')).hide();
}

// What this page's elements name in data-nr-<event> (js/events.js)
nrHandlers({
    openQuickCreateProfile: (el, e) => { e.stopPropagation(); openQuickCreateProfile(el.dataset.which); },
    testSignIn: el => { testSignIn(el.dataset.which); },
    detectPlatform: el => { detectPlatform(el.dataset.which); },
    testEditConnection: () => { testEditConnection(); },
    openQuickCreateMapping: () => { openQuickCreateMapping(); },
    openQuickCreateProperty: () => { openQuickCreateProperty(); },
    submitQuickCreateProfile: () => { submitQuickCreateProfile(); },
    submitQuickCreateMapping: () => { submitQuickCreateMapping(); },
    submitQuickCreateProperty: () => { submitQuickCreateProperty(); },
    qcmPropertyPicked: el => {
        document.getElementById('qcmIndexRow').style.display = QCM_LIST_PROPS.has(el.value) ? '' : 'none';
    },
});
