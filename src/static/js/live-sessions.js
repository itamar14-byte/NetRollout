// Live Sessions - see templates/live_sessions.html.
  function kick(userId) {
    const row = document.getElementById(`row-${userId}`);
    row.classList.add('kicking');

    return fetch(`/admin/sessions/${userId}/kick`, {
      method: 'POST',
      headers: { 'X-CSRFToken': CSRF_TOKEN, 'X-Requested-With': 'XMLHttpRequest' }
    })
    .then(r => {
      row.classList.remove('kicking');
      if (r.ok || r.status === 404) {   // 404: the session had already ended
        row.classList.add('kicked');
        updateCounts();
      }
    })
    .catch(() => row.classList.remove('kicking'));
  }

  // everyone else in this list - never the admin's own row (Sign out ends that)
  async function kickAll(btn) {
    const table = btn.closest('.nr-card-body').querySelector('.session-table tbody');
    const rows = [...table.querySelectorAll('tr:not(.kicked):not([data-self])')];
    if (!rows.length) return;
    if (!await nrConfirm({title: `End the sessions of ${rows.length} user${rows.length !== 1 ? 's' : ''}?`,
                          message: 'They are signed out everywhere.', danger: true,
                          confirmLabel: 'End sessions'})) return;
    rows.forEach(row => kick(row.id.replace('row-', '')));
  }

  function updateCounts() {
    document.querySelectorAll('.session-count').forEach(el => {
      const table = el.closest('.nr-card-body').querySelector('.session-table tbody');
      if (!table) return;
      const visible = table.querySelectorAll('tr:not(.kicked)').length;
      el.textContent = visible;
    });
  }

// What this page's elements name in data-nr-<event> (js/events.js)
nrHandlers({
    kickAll: el => { kickAll(el); },
    kick: el => { kick(el.dataset.userId); },
});
