// nrRolloutsTable(container, rollouts) - see templates/_rollouts_table_js.html, which loads it.
const _nrCancelledRollouts = new Set();

function nrRolloutsTable(container, rollouts) {
    if (!container) return;
    if (!rollouts || !rollouts.length) { container.innerHTML = ''; return; }
    const rows = rollouts.map(j => {
        const asked = _nrCancelledRollouts.has(j.job_id);
        return `<tr><td>${nrEscape(j.user)}</td><td>${nrEscape(j.devices)}</td>
            <td>${nrEscape(j.state)}</td><td>${j.started ? nrEscape(String(j.started).replace('T', ' ')) : '-'}</td>
            <td class="text-end"><button type="button" class="btn btn-outline-danger btn-sm py-0" data-job="${nrEscape(j.job_id)}"${asked ? ' disabled' : ''}>${asked ? 'Cancelling…' : 'Cancel rollout'}</button></td></tr>`;
    }).join('');
    container.innerHTML = `<table class="nr-rollouts"><thead><tr><th>By</th><th>Devices</th><th>State</th><th>Started</th><th><span class="visually-hidden">Actions</span></th></tr></thead><tbody>${rows}</tbody></table>`;
    container.querySelectorAll('[data-job]').forEach(b => b.addEventListener('click', async () => {
        if (!await nrConfirm({title: 'Cancel this rollout?', danger: true, confirmLabel: 'Cancel rollout',
                              cancelLabel: 'Keep it running',
                              message: 'Devices it has not reached are skipped.'})) return;
        b.disabled = true;
        b.textContent = 'Cancelling…';
        let resp = null;
        try {
            resp = await fetch('/rollout/cancel', {method: 'POST', headers: {'X-CSRFToken': CSRF_TOKEN, 'X-Requested-With': 'XMLHttpRequest'},
                                                   body: new URLSearchParams({job_id: b.dataset.job})});
        } catch (e) { /* no answer: allowed again below */ }
        if (resp && (resp.ok || resp.status === 404)) {   // 404: it ended meanwhile
            _nrCancelledRollouts.add(b.dataset.job);
            return;
        }
        b.disabled = false;
        b.textContent = 'Cancel rollout';
        let message = 'The rollout could not be cancelled - try again.';
        try { message = (await resp.json()).message || message; } catch (e) { /* not JSON */ }
        nrAlert(message, 'Not cancelled');
    }));
}
