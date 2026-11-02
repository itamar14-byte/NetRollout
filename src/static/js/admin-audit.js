// Audit Log - see templates/admin_audit.html.
  let _activeRow = null;
  const popover = document.getElementById('gearPopover');

  function openGear(event, btn) {
    event.stopPropagation();
    _activeRow = btn.closest('tr');
    const rect = btn.getBoundingClientRect();
    popover.style.display = 'block';
    // Anchor to bottom-right of gear button; flip up if near bottom of viewport
    let top = rect.bottom + 4;
    if (top + 130 > window.innerHeight) top = rect.top - 134;
    let left = rect.right - popover.offsetWidth;
    if (left < 4) left = 4;
    popover.style.top  = top  + 'px';
    popover.style.left = left + 'px';
  }

  // A click anywhere on a row (or Enter on a focused one) opens its detail;
  // the gear keeps its menu (it stops the click itself)
  document.querySelectorAll('tr.audit-row').forEach(row => {
    row.addEventListener('click', () => { _activeRow = row; viewDetail(); });
    row.addEventListener('keydown', e => {
      if (e.key === 'Enter') { _activeRow = row; viewDetail(); }
    });
  });

  document.addEventListener('click', () => { popover.style.display = 'none'; });
  popover.addEventListener('click', e => e.stopPropagation());

  function viewDetail() {
    popover.style.display = 'none';
    if (!_activeRow) return;
    const ts     = _activeRow.dataset.ts;
    const rawDetail = _activeRow.dataset.detail;
    let parsed = null;
    try { parsed = JSON.parse(rawDetail); } catch(e) { parsed = rawDetail; }

    document.getElementById('detailModalTitle').textContent = 'Entry - ' + ts;

    // Build metadata rows from table cells (their text, set as text)
    const cells = _activeRow.querySelectorAll('td');
    const fields = ['Timestamp','Actor','Action','Object','Status','IP'];
    const rows = fields.map((field, i) => {
      const tr = document.createElement('tr');
      const th = document.createElement('th');
      th.style.cssText = 'width:110px;color:var(--nr-text-muted);font-weight:500;';
      th.textContent = field;
      const td = document.createElement('td');
      td.style.color = '#ccc';
      td.textContent = cells[i].innerText.trim();
      tr.append(th, td);
      return tr;
    });
    document.getElementById('detailMetaRows').replaceChildren(...rows);

    const jsonEl = document.getElementById('detailJson');
    if (parsed && typeof parsed === 'object' && Object.keys(parsed).length > 0) {
      jsonEl.textContent = JSON.stringify(parsed, null, 2);
      jsonEl.style.display = 'block';
    } else {
      jsonEl.style.display = 'none';
    }

    // one instance, reused: rows open it often now (a new one per open stacks)
    bootstrap.Modal.getOrCreateInstance(document.getElementById('detailModal')).show();
  }

  function copyRow() {
    popover.style.display = 'none';
    if (!_activeRow) return;
    const cells = _activeRow.querySelectorAll('td');
    const parts = Array.from(cells).slice(0, 6).map(c => c.innerText.trim());
    navigator.clipboard.writeText(parts.join('\t'));
  }

  function filterByActor() {
    popover.style.display = 'none';
    if (!_activeRow) return;
    const actor = _activeRow.querySelectorAll('td')[1].innerText.trim();
    document.querySelector('input[name="actor"]').value = actor;
    document.getElementById('filterForm').submit();
  }
