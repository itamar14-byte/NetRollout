// Variable Mappings - see templates/variable_mappings.html.
// ── Static data ──
const DEVICES_ALL  = nrData('nr-devices');
const VENDOR_LOGOS = nrData('nr-vendor-logos');
const ATTR_ICONS   = nrData('nr-attr-icons');
const LIST_PROPS   = new Set(nrData('nr-list-props'));

// ── Active mapping context (set when devices modal opens) ──
let activeMappingId    = null;
let activeMappingProp  = null;
let activeMappingIndex = null;
let mapBoard           = null;

// ── Wire up card buttons ──
document.querySelectorAll('.mapping-card').forEach(card => {
    const mapping = JSON.parse(card.dataset.mapping);
    const devices = JSON.parse(card.dataset.devices);
    card.querySelector('.edit-btn').addEventListener('click',    () => openEdit(mapping));
    card.querySelector('.delete-btn').addEventListener('click',  () => openDelete(mapping));
    card.querySelector('.devices-btn').addEventListener('click', () => openDevices(mapping, devices));
});


// ══════════════════════════════════════
//  ATTRIBUTE NrSelect FACTORY
// ══════════════════════════════════════
function initAttrSelect(containerId, hiddenId, indexWrapId) {
    const container = document.getElementById(containerId);
    const hidden    = document.getElementById(hiddenId);
    const indexWrap = indexWrapId ? document.getElementById(indexWrapId) : null;
    const list      = container.querySelector('.nr-select-list');
    const options   = () => container.querySelectorAll('.nr-select-option');   // live: a quick create adds one
    const display   = container.querySelector('.nr-select-display');
    const shell     = nrSelectShell(container, filterOptions, null);

    function renderDisplay(value, icon) {
        if (!value) {
            display.innerHTML = '<span class="nr-select-placeholder">Select an attribute…</span>';
        } else {
            const listTag = LIST_PROPS.has(value)
                ? '<span class="opt-sub" style="margin-left:0.3rem;">list</span>' : '';
            display.innerHTML =
                `<i class="bi ${nrEscape(icon)}" style="color:#00bcd4; font-size:0.85rem; flex-shrink:0;"></i>` +
                `<span class="nr-select-display-label">${nrEscape(value)}</span>` + listTag;
        }
    }

    // the index only for a list property; hidden, it's emptied (else it's posted)
    function showIndex(show) {
        if (!indexWrap) return;
        indexWrap.classList.toggle('visible', show);
        if (!show) indexWrap.querySelectorAll('input').forEach(i => { i.value = ''; });
    }

    function selectOption(value, icon) {
        hidden.value = value;
        renderDisplay(value, icon);
        options().forEach(o => o.classList.toggle('selected', o.dataset.value === value));
        showIndex(LIST_PROPS.has(value));
        shell.close();
    }

    function filterOptions(q) {
        const lq = q.toLowerCase();
        options().forEach(o => {
            o.style.display = !q || o.dataset.value.includes(lq) ? '' : 'none';
        });
    }

    function wireOption(opt) {
        shell.wire(opt, o => selectOption(o.dataset.value, o.dataset.icon));
    }

    options().forEach(wireOption);
    shell.listen();

    return {
        getValue: ()      => hidden.value,
        setValue: (value) => {
            const opt = Array.from(options()).find(o => o.dataset.value === value);
            if (opt) selectOption(value, opt.dataset.icon);
        },
        reset: () => {
            hidden.value = '';
            renderDisplay('', '');
            options().forEach(o => o.classList.remove('selected'));
            showIndex(false);
        },
        // a property made by the quick create: listed (before the "New property…"
        // footer) and clickable like the others
        add: (option) => {
            list.insertBefore(option, list.querySelector('div[style*="border-top"]'));
            wireOption(option);
        }
    };
}

const addAttrSel  = initAttrSelect('addAttrSelect',  'addAttrHidden',  'addIndexWrap');
const editAttrSel = initAttrSelect('editAttrSelect', 'editAttrHidden', 'editIndexWrap');


// ══════════════════════════════════════
//  EDIT MODAL
// ══════════════════════════════════════
function openEdit(mapping) {
    document.getElementById('editModalTitle').textContent = 'Edit - ' + mapping.token;
    document.getElementById('editLabel').value      = mapping.label || '';
    // Strip outer $$ to show just the inner name
    document.getElementById('editTokenInner').value =
        mapping.token.replace(/^\$\$|\$\$$/g, '');
    editAttrSel.setValue(mapping.property_name);
    document.getElementById('editIndex').value =
        mapping.index !== null && mapping.index !== undefined ? mapping.index : '';
    document.getElementById('editForm').action = '/mappings/' + mapping.id + '/edit';
    new bootstrap.Modal(document.getElementById('editModal')).show();
}


// ══════════════════════════════════════
//  DELETE MODAL
// ══════════════════════════════════════
function openDelete(mapping) {
    document.getElementById('deleteMappingToken').textContent = mapping.token;
    document.getElementById('deleteForm').action = '/mappings/' + mapping.id + '/delete';
    new bootstrap.Modal(document.getElementById('deleteModal')).show();
}


// ══════════════════════════════════════
//  DEVICES MODAL
// ══════════════════════════════════════
function openDevices(mapping, assignedDevices) {
    activeMappingId    = mapping.id;
    activeMappingProp  = mapping.property_name;
    activeMappingIndex = mapping.index;

    document.getElementById('devicesModalTitle').textContent =
        mapping.token + ' - Assignments';
    document.getElementById('devicesError').style.display = 'none';

    // A bound device can be missing from DEVICES_ALL (no longer visible to
    // this user); keep it on the board so it can still be unassigned
    const known    = new Set(DEVICES_ALL.map(d => d.id));
    const assigned = new Set(assignedDevices.map(d => d.id));
    mapBoard = nrAssignBoard(document.getElementById('assignBoard'), {
        devices:  DEVICES_ALL.concat(assignedDevices.filter(d => !known.has(d.id))),
        assigned: [...assigned],
        pool:     DEVICES_ALL.filter(d => isResolvable(d) && !assigned.has(d.id)).map(d => d.id),
        logos:    VENDOR_LOGOS,
        legend:   document.getElementById('boardLegend'),
        search:   document.querySelector('#devicesModal .nr-board-search'),
        meta:     d => getDeviceVal(d),
        warn:     d => isResolvable(d) ? null
                     : 'No value for ' + activeWhere() + ': this device is skipped at rollout',
        emptyIn:  'Nothing assigned yet. Drag devices here, or click them.',
        emptyOut: emptyPoolText,
        onChange: b => nrBoardSaveLabel(document.getElementById('saveAssignBtn'), b),
    });

    new bootstrap.Modal(document.getElementById('devicesModal')).show();
}

function activeWhere() {
    return activeMappingIndex === null || activeMappingIndex === undefined
        ? activeMappingProp : activeMappingProp + '[' + activeMappingIndex + ']';
}

// Can the active mapping resolve on this device? Mirrors the server rule
// (core.mapping_resolvable): value set and non-empty; for indexed mappings
// the list is long enough. Anything else the server would refuse to bind.
function isResolvable(device) {
    const val = (device.var_maps || {})[activeMappingProp];
    if (!val || (Array.isArray(val) && !val.length)) return false;
    if (activeMappingIndex === null || activeMappingIndex === undefined) return true;
    return Array.isArray(val) && activeMappingIndex >= 0 && activeMappingIndex < val.length;
}

// Why the Eligible column is empty - tells the user what to do instead of
// showing a blank column
function emptyPoolText() {
    return DEVICES_ALL.some(isResolvable)
        ? 'All devices with a value for ' + activeWhere() + ' are assigned.'
        : 'No devices have a value for ' + activeWhere() + '. Set it in Inventory: ' +
          'open a device, then Variable Attributes.';
}

// Get the resolved attribute value string for a device given the active mapping context
function getDeviceVal(device) {
    const vm  = device.var_maps || {};
    let   val = vm[activeMappingProp];
    if (val === undefined || val === null) return '-';
    if (Array.isArray(val)) {
        if (activeMappingIndex !== null && activeMappingIndex !== undefined) {
            const v = val[activeMappingIndex];
            return v !== undefined ? String(v) : '?';
        }
        // Show abbreviated list
        const shown = val.slice(0, 3).join(', ');
        return val.length > 3 ? shown + '…' : shown;
    }
    return String(val);
}

// One call: device_ids are assigned, remove_ids unassigned
async function saveAssignments() {
    const btn    = document.getElementById('saveAssignBtn');
    const errBox = document.getElementById('devicesError');
    btn.disabled  = true;
    btn.innerHTML = '<span class="spinner-border spinner-border-sm me-2" role="status"></span>Saving…';
    errBox.style.display = 'none';

    let message = 'Network error. Try again.';
    try {
        const resp = await fetch('/mappings/bulk_assign', {
            method:  'POST',
            headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
            body:    JSON.stringify({ mapping_id: activeMappingId,
                                      device_ids: mapBoard.added(),
                                      remove_ids: mapBoard.removed() })
        });
        const data = await resp.json();
        if (data.status === 'ok') { location.reload(); return; }
        message = data.message || 'Saving failed.';
    } catch { /* keep the network message */ }
    errBox.innerHTML = '<i class="bi bi-x-circle-fill"></i><span></span>';
    errBox.querySelector('span').textContent = message;
    errBox.style.display = '';
    nrBoardSaveLabel(btn, mapBoard);
}


// ══════════════════════════════════════
//  FORM SUBMIT VALIDATION
// ══════════════════════════════════════
document.querySelectorAll('form[novalidate]').forEach(form => {
    form.addEventListener('submit', e => {
        let valid = true;

        // Standard required inputs
        form.querySelectorAll('input[required]').forEach(el => {
            if (!el.value.trim()) {
                el.classList.add('nr-submitted');
                valid = false;
            }
        });

        // Attribute NrSelect (hidden input, show custom error)
        const attrHidden = form.querySelector('input[name="property_name"]');
        const attrError  = form.querySelector('[id$="AttrError"]');
        if (attrHidden && !attrHidden.value) {
            if (attrError) attrError.style.display = 'block';
            valid = false;
        }

        if (!valid) {
            e.preventDefault();
            form.style.animation = 'none';
            void form.offsetHeight;
            form.style.animation = 'shake 0.45s ease';
        }
    });
});

// ── Clear validation state on modal close ──
['addModal', 'editModal'].forEach(id => {
    document.getElementById(id).addEventListener('hidden.bs.modal', () => {
        const form = document.getElementById(id).querySelector('form');
        if (!form) return;
        form.querySelectorAll('.nr-submitted').forEach(el => el.classList.remove('nr-submitted'));
        form.querySelectorAll('[id$="AttrError"]').forEach(el => el.style.display = 'none');
        form.style.animation = '';
    });
});


// ── Quick Create Property ─────────────────────────────────────────────────────
initIconPicker('mqpIconPreview', 'mqpIcon');

// Escape closes the top modal: the quick create first, else Add / Edit Mapping
// (as on Inventory). Document-level so focus location doesn't matter: Bootstrap
// doesn't stack modals - opening the quick create left focus in Add / Edit
// Mapping (Esc closed that one, behind it) and closing it leaves focus on
// <body> (Esc did nothing). Add / Edit Mapping have data-bs-keyboard="false" -
// only this closes them; the quick create closes on Bootstrap's own Escape too,
// and then this finds it already closed.
let _mappingQuickPropActive = false;
const _mappingQuickPropModal = document.getElementById('mappingQuickPropModal');
_mappingQuickPropModal.addEventListener('show.bs.modal',   () => { _mappingQuickPropActive = true;  });
_mappingQuickPropModal.addEventListener('hidden.bs.modal', () => { _mappingQuickPropActive = false; });

document.addEventListener('keydown', function(e) {
    if (e.key !== 'Escape') return;
    const ids = _mappingQuickPropActive ? ['mappingQuickPropModal'] : ['addModal', 'editModal'];
    ids.forEach(id => {
        const el = document.getElementById(id);
        if (el.classList.contains('show'))
            bootstrap.Modal.getOrCreateInstance(el).hide();
    });
});

function openMappingQuickProp() {
    ['mqpName','mqpLabel'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('mqpIcon').value = 'bi-tag';
    document.getElementById('mqpIconPreview').className = 'bi bi-tag';
    document.getElementById('mqpIsList').checked = false;
    document.getElementById('mqpError').style.display = 'none';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('mappingQuickPropModal')).show();
}

async function submitMappingQuickProp() {
    let name    = document.getElementById('mqpName').value.trim();
    let label   = document.getElementById('mqpLabel').value.trim();
    let icon    = document.getElementById('mqpIcon').value.trim() || 'bi-tag';
    let is_list = document.getElementById('mqpIsList').checked;
    const errEl   = document.getElementById('mqpError');
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

    if (is_list) LIST_PROPS.add(name);
    ATTR_ICONS[name] = icon;
    // the new property in both pickers, and chosen in the mapping it was made for
    [addAttrSel, editAttrSel].forEach(sel => sel.add(attrOption(name, icon, is_list)));
    for (const [modalId, sel] of [['addModal', addAttrSel], ['editModal', editAttrSel]]) {
        if (document.getElementById(modalId).classList.contains('show')) sel.setValue(name);
    }

    bootstrap.Modal.getOrCreateInstance(document.getElementById('mappingQuickPropModal')).hide();
}

// An attribute picker's option, as the page renders them (text set, not parsed)
function attrOption(name, icon, isList) {
    const opt = document.createElement('div');
    opt.className = 'nr-select-option';
    Object.assign(opt.dataset, {value: name, icon: icon, isList: String(isList)});
    const glyph = document.createElement('i');
    glyph.className = 'bi ' + icon;
    glyph.style.cssText = 'color:#00bcd4; font-size:0.82rem; width:16px; text-align:center; flex-shrink:0;';
    const label = document.createElement('span');
    label.className = 'opt-label';
    label.textContent = name;
    opt.append(glyph, label);
    if (isList) {
        const sub = document.createElement('span');
        sub.className = 'opt-sub';
        sub.textContent = 'list';
        opt.append(sub);
    }
    const check = document.createElement('i');
    check.className = 'bi bi-check2 opt-check';
    opt.append(check);
    return opt;
}

// What this page's elements name in data-nr-<event> (js/events.js)
nrHandlers({
    openMappingQuickProp: () => { openMappingQuickProp(); },
    saveAssignments: () => { saveAssignments(); },
    submitMappingQuickProp: () => { submitMappingQuickProp(); },
});
