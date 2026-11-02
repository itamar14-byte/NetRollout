// Properties - see templates/properties.html.
initIconPicker('addPropIconPreview', 'addPropIcon');
initIconPicker('editPropIconPreview', 'editPropIcon');

function openAddProp() {
    ['addPropName','addPropLabel'].forEach(id => document.getElementById(id).value = '');
    document.getElementById('addPropIcon').value = 'bi-tag';
    document.getElementById('addPropIconPreview').className = 'bi bi-tag';
    document.getElementById('addPropIsList').checked = false;
    document.getElementById('addPropError').style.display = 'none';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('addPropModal')).show();
}

async function submitAddProp() {
    const name    = document.getElementById('addPropName').value.trim();
    const label   = document.getElementById('addPropLabel').value.trim();
    const icon    = document.getElementById('addPropIcon').value.trim() || 'bi-tag';
    const is_list = document.getElementById('addPropIsList').checked;
    const errEl   = document.getElementById('addPropError');
    errEl.style.display = 'none';

    const resp = await fetch('/properties/create', {
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
    bootstrap.Modal.getOrCreateInstance(document.getElementById('addPropModal')).hide();
    location.reload();
}

function openEditProp(id, label, icon, is_list) {
    document.getElementById('editPropId').value = id;
    document.getElementById('editPropLabel').value = label;
    document.getElementById('editPropIcon').value = icon;
    document.getElementById('editPropIconPreview').className = 'bi ' + icon;
    document.getElementById('editPropIsList').checked = is_list;
    document.getElementById('editPropError').style.display = 'none';
    bootstrap.Modal.getOrCreateInstance(document.getElementById('editPropModal')).show();
}

async function submitEditProp() {
    const id      = document.getElementById('editPropId').value;
    const label   = document.getElementById('editPropLabel').value.trim();
    const icon    = document.getElementById('editPropIcon').value.trim() || 'bi-tag';
    const is_list = document.getElementById('editPropIsList').checked;
    const errEl   = document.getElementById('editPropError');
    errEl.style.display = 'none';

    const resp = await fetch(`/properties/${id}/edit`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN},
        body: JSON.stringify({label, icon, is_list})
    });
    const data = await resp.json();
    if (data.status !== 'ok') {
        errEl.textContent = data.message;
        errEl.style.display = '';
        return;
    }
    bootstrap.Modal.getOrCreateInstance(document.getElementById('editPropModal')).hide();
    location.reload();
}

async function deleteProp(id, name) {
    if (!await nrConfirm({title: `Delete property "${name}"?`, danger: true, confirmLabel: 'Delete',
                          message: 'This will not affect existing device data.'})) return;
    const resp = await fetch(`/properties/${id}/delete`, {
        method: 'POST',
        headers: {'X-CSRFToken': CSRF_TOKEN}
    });
    const data = await resp.json();
    if (data.status !== 'ok') { nrAlert(data.message, 'Not deleted'); return; }
    document.getElementById('propCard-' + id)?.remove();
}

// Escape handling - same pattern as inventory
document.addEventListener('keydown', function(e) {
    if (e.key !== 'Escape') return;
    ['addPropModal','editPropModal'].forEach(id => {
        const el = document.getElementById(id);
        if (el.classList.contains('show'))
            bootstrap.Modal.getOrCreateInstance(el).hide();
    });
});

// What this page's elements name in data-nr-<event> (js/events.js)
nrHandlers({
    openAddProp: () => { openAddProp(); },
    openEditProp: el => { openEditProp(el.dataset.id, el.dataset.label, el.dataset.icon, el.dataset.isList === 'true'); },
    deleteProp: el => { deleteProp(el.dataset.id, el.dataset.name); },
    submitAddProp: () => { submitAddProp(); },
    submitEditProp: () => { submitEditProp(); },
});
