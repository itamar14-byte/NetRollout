// Security Profiles - see templates/security.html.
let activeProfileId = null;

// All inventory devices - used for the split assign view
const DEVICES_ALL = nrData('nr-devices');
// Profile id → display name, to show which profile a device would move from
const PROFILE_LABELS = nrData('nr-profile-labels');
const VENDOR_LOGOS = nrData('nr-vendor-logos');

// ── Show/hide password toggle ──
function togglePw(inputId, btn) {
    const input = document.getElementById(inputId);
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    const icon = btn.querySelector('i');
    icon.className = show ? 'bi bi-eye-slash' : 'bi bi-eye';
    btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    btn.title = show ? 'Hide password' : 'Show password';
}

// ── Wire up card buttons ──
document.querySelectorAll('.profile-card').forEach(card => {
    const profile = JSON.parse(card.dataset.profile);
    const devices = JSON.parse(card.dataset.devices);

    card.querySelector('.edit-btn').addEventListener('click', () => openEdit(profile));
    card.querySelector('.delete-btn').addEventListener('click', () => openDelete(profile, devices));
    card.querySelector('.devices-btn').addEventListener('click', () => openDevices(profile));
    card.querySelector('.test-btn').addEventListener('click', () => openTest(profile));
});

// ── Edit modal ──
function openEdit(profile) {
    document.getElementById('editModalTitle').textContent =
        'Edit - ' + (profile.label || profile.username);
    document.getElementById('editLabel').value    = profile.label || '';
    document.getElementById('editUsername').value = profile.username;
    document.getElementById('editPassword').value       = '';
    document.getElementById('editEnableSecret').value   = '';
    document.getElementById('clearEnable').checked      = false;

    // Show "clear enable secret" checkbox only if profile has one
    document.getElementById('clearEnableWrap').style.display =
        profile.has_enable ? 'block' : 'none';

    document.getElementById('editForm').action =
        '/security/' + profile.id + '/edit';

    new bootstrap.Modal(document.getElementById('editModal')).show();
}

// ── Delete modal ──
// The devices it's linked to lose it with it (security_delete unlinks them)
function openDelete(profile, devices) {
    document.getElementById('deleteProfileName').textContent =
        profile.label || profile.username;
    const n = devices.length;
    document.getElementById('deleteModalNote').textContent = n
        ? "This cannot be undone. It's linked to " + (n === 1 ? '1 device' : n + ' devices') +
          ": you'll need another profile on " + (n === 1 ? 'it' : 'them') +
          ' before you can roll out to ' + (n === 1 ? 'it' : 'them') + ' again.'
        : 'This cannot be undone.';
    document.getElementById('deleteForm').action =
        '/security/' + profile.id + '/delete';

    new bootstrap.Modal(document.getElementById('deleteModal')).show();
}

// ── Devices modal: assign board ──
let activeDevicesProfileId = null;
let secBoard  = null;
let partSaved = false;  // a save half-applied: reload on close to show the truth

function openDevices(profile) {
    activeDevicesProfileId = profile.id;
    partSaved = false;
    document.getElementById('devicesModalTitle').textContent =
        (profile.label || profile.username) + ' - Attached Devices';
    document.getElementById('devicesError').style.display = 'none';

    const mine = d => d.sec_profile_id === profile.id;
    secBoard = nrAssignBoard(document.getElementById('assignBoard'), {
        devices:  DEVICES_ALL,
        assigned: DEVICES_ALL.filter(mine).map(d => d.id),
        pool:     DEVICES_ALL.filter(d => !mine(d)).map(d => d.id),
        logos:    VENDOR_LOGOS,
        legend:   document.getElementById('boardLegend'),
        search:   document.querySelector('#devicesModal .nr-board-search'),
        // Which of your profiles you log in to the device with today
        meta:     d => mine(d) ? '' : (PROFILE_LABELS[d.sec_profile_id] || 'no profile'),
        emptyIn:  'Nothing linked yet. Drag devices here, or click them.',
        emptyOut: () => 'No other devices you can see.',
        onChange: b => {
            nrBoardSaveLabel(document.getElementById('saveAssignBtn'), b);
            const n = b.removed().length;
            const warn = document.getElementById('unassignWarn');
            warn.style.display = n ? '' : 'none';
            warn.querySelector('span').textContent = n === 1
                ? '1 device will be left without your security profile. You can\'t roll out to it until you link one.'
                : n + ' devices will be left without your security profile. You can\'t roll out to them until you link one.';
        },
    });

    new bootstrap.Modal(document.getElementById('devicesModal')).show();
}

// Added devices move to this profile; removed ones are left with none.
// Two calls - both are idempotent, so retrying after a partial save is safe.
async function saveAssignments() {
    const btn    = document.getElementById('saveAssignBtn');
    const errBox = document.getElementById('devicesError');
    const calls  = [];
    if (secBoard.added().length)
        calls.push({ profile_id: activeDevicesProfileId, device_ids: secBoard.added() });
    if (secBoard.removed().length)
        calls.push({ profile_id: null, device_ids: secBoard.removed() });

    btn.disabled  = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Saving…';
    errBox.style.display = 'none';

    let message = 'Network error. Try again.';
    try {
        for (const body of calls) {
            const resp = await fetch('/inventory/bulk_assign', {
                method:  'POST',
                headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
                body:    JSON.stringify(body)
            });
            const data = await resp.json();
            if (data.status !== 'ok') { message = data.message || 'Saving failed.'; throw 0; }
            partSaved = true;
        }
        location.reload();
        return;
    } catch { /* message set above */ }
    if (partSaved) message += ' Some changes were saved; saving again is safe.';
    errBox.innerHTML = '<i class="bi bi-x-circle-fill"></i><span></span>';
    errBox.querySelector('span').textContent = message;
    errBox.style.display = '';
    nrBoardSaveLabel(btn, secBoard);
}

document.getElementById('devicesModal').addEventListener('hidden.bs.modal', () => {
    if (partSaved) location.reload();
});

// ── Clear validation state on modal close ──
['addModal', 'editModal'].forEach(id => {
    document.getElementById(id).addEventListener('hidden.bs.modal', () => {
        const form = document.getElementById(id).querySelector('form');
        if (!form) return;
        form.querySelectorAll('.nr-submitted').forEach(el => el.classList.remove('nr-submitted'));
        form.querySelectorAll('.field-error').forEach(el => el.style.display = '');
    });
});

// ── Test device NrSelect ──
let testDevSelect = null;

function initTestDevSelect() {
    const container = document.getElementById('testDevSelect');
    if (!container) return;

    const options  = container.querySelectorAll('.nr-select-option');
    const hidden   = document.getElementById('testDeviceHidden');
    const display  = container.querySelector('.nr-select-display');
    const shell    = nrSelectShell(container, filterOptions, document.getElementById('testModal'));

    function renderDisplay(label, ip, deviceType) {
        if (!label) {
            display.innerHTML = '<span class="nr-select-placeholder">Select a device…</span>';
        } else {
            const logoUrl = VENDOR_LOGOS[deviceType] || '';
            const logoHtml = logoUrl
                ? `<img alt="" src="${nrEscape(logoUrl)}" width="16" height="16"
                        style="border-radius:3px;opacity:0.85;flex-shrink:0;"
                        onerror="this.style.display='none'">`
                : '<i class="bi bi-router" style="font-size:0.85rem;color:var(--nr-text-faint);flex-shrink:0;"></i>';
            display.innerHTML =
                logoHtml +
                `<span class="nr-select-display-label">${nrEscape(label)}</span>` +
                `<span class="opt-sub" style="margin-left:0.3rem;">${nrEscape(ip)}</span>`;
        }
    }

    function selectOption(value, label, ip, deviceType) {
        hidden.value = value;
        renderDisplay(label, ip, deviceType);
        options.forEach(o => o.classList.toggle('selected', o.dataset.value === value));
        shell.close();
    }

    function filterOptions(q) {
        const lq = q.toLowerCase();
        options.forEach(o => {
            const match = !q
                || (o.dataset.label || '').toLowerCase().includes(lq)
                || (o.dataset.ip   || '').toLowerCase().includes(lq);
            o.style.display = match ? '' : 'none';
        });
    }

    options.forEach(opt => shell.wire(opt, o => selectOption(o.dataset.value, o.dataset.label, o.dataset.ip, o.dataset.type)));
    shell.listen();

    testDevSelect = {
        getValue: () => hidden.value,
        reset()   { selectOption('', '', '', ''); }
    };
}

document.addEventListener('DOMContentLoaded', initTestDevSelect);

// ── Test connection modal ──
function openTest(profile) {
    activeProfileId = profile.id;
    document.getElementById('testModalTitle').textContent =
        'Test - ' + (profile.label || profile.username);

    const result = document.getElementById('testResult');
    result.className = 'test-result';
    result.textContent = '';
    result.style.display = 'none';

    if (testDevSelect) testDevSelect.reset();

    new bootstrap.Modal(document.getElementById('testModal')).show();
}

// ── Reset test result when modal closes ──
document.getElementById('testModal').addEventListener('hidden.bs.modal', () => {
    const result = document.getElementById('testResult');
    result.className = 'test-result';
    result.textContent = '';
    result.style.display = 'none';
    activeProfileId = null;
});

// ── Run connection test ──
async function runTest() {
    const deviceId = testDevSelect ? testDevSelect.getValue() : '';
    if (!deviceId) return;

    const btn = document.getElementById('testBtn');
    const result = document.getElementById('testResult');

    // Spinner
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Testing…';
    result.style.display = 'none';
    result.className = 'test-result';

    try {
        const resp = await fetch(`/security/${activeProfileId}/test`, {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json',
                'X-CSRFToken': CSRF_TOKEN
            },
            body: JSON.stringify({ device_id: deviceId })
        });
        const data = await resp.json();

        if (data.status === 'ok') {
            result.className = 'test-result success';
            result.innerHTML = '<i class="bi bi-check-circle me-2"></i>' + nrEscape(data.message);
        } else {
            result.className = 'test-result error';
            result.innerHTML = '<i class="bi bi-x-circle me-2"></i>' + nrEscape(data.message);
        }
        result.style.display = 'block';

    } catch (e) {
        result.className = 'test-result error';
        result.innerHTML = '<i class="bi bi-x-circle me-2"></i>Request failed - server unreachable';
        result.style.display = 'block';
    } finally {
        btn.disabled = false;
        btn.innerHTML = '<i class="bi bi-plug me-1"></i>Test Connection';
    }
}
