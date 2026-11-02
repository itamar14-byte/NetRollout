// Admin Restart - see templates/_restart_script.html, which loads it.
// the sidebar button this skeleton names on the tag: data-btn (its id), data-icon
// (its icon's class), data-label (its label's selector)
const NR_RESTART_BUTTON = document.currentScript.dataset;
let _restartTimer = null;       // the "Restart now" countdown
let _restartListTimer = null;   // the list's refresh
let _restartWaiting = false;    // "when finished" chosen: waiting for the rollouts

// {rollouts, running, queued}; null: no answer
async function nrRestartRollouts(background) {
    try {
        const r = await fetch('/admin/server/rollouts', {cache: 'no-store',
            headers: background ? {'X-NR-Background': '1'} : {}});
        if (!r.ok) return null;
        const d = await r.json();
        return d.status === 'ok' ? d : null;
    } catch (e) { return null; }
}

async function nrRestartClick() {
    const d = await nrRestartRollouts(false);
    // unknown: the server answers 409 if rollouts run
    if (!d || !(d.running + d.queued)) {
        if (await nrConfirm({title: 'Restart NetRollout?', danger: true, confirmLabel: 'Restart',
                             message: 'Everyone will be signed out and sign in again.'}))
            nrFireRestart();
        return;
    }
    nrShowRestartChoice(d);
}

function nrShowRestartList(d) {
    const plural = (n, word) => `${n} ${word}${n !== 1 ? 's' : ''}`;
    const running = d ? d.running : 0, queued = d ? d.queued : 0;
    const parts = [];
    if (running) parts.push(`${plural(running, 'rollout')} running`);
    if (queued) parts.push(`${plural(queued, 'rollout')} queued`);
    document.getElementById('restartJobWarning').textContent =
        parts.length ? parts.join(', ') + '.' : (_restartWaiting ? 'No rollout left.' : 'No rollout running now.');
    nrRolloutsTable(document.getElementById('restartRollouts'), d ? d.rollouts : []);
}

function nrRestartPoll() {
    clearInterval(_restartListTimer);
    _restartListTimer = setInterval(async () => {
        const d = await nrRestartRollouts(true);
        if (d) { nrShowRestartList(d); return; }
        if (_restartWaiting) {       // down: the restart is under way
            document.getElementById('restartJobWarning').textContent = '';
            nrRolloutsTable(document.getElementById('restartRollouts'), []);
            document.getElementById('restartWaitingTitle').textContent = 'NetRollout is restarting.';
            document.getElementById('restartWaitingText').textContent = 'This page reconnects when it\'s back.';
        }
    }, 3000);
}

function nrShowRestartChoice(d) {
    _restartWaiting = false;
    document.getElementById('restartChoice').style.display = '';
    document.getElementById('restartWaiting').style.display = 'none';
    document.getElementById('restartWhenDoneBtn').style.display = '';
    document.getElementById('restartConfirmBtn').style.display = '';
    document.getElementById('restartCloseBtn').textContent = 'Cancel';
    nrShowRestartList(d);
    if (!d.rollouts) nrRestartRollouts(false).then(full => { if (full) nrShowRestartList(full); });

    // "Now" cancels work: enabled after a short pause
    const nowBtn = document.getElementById('restartConfirmBtn');
    const countdown = document.getElementById('restartCountdown');
    nowBtn.disabled = true;
    document.getElementById('restartWhenDoneBtn').disabled = false;
    let secs = 3;
    countdown.textContent = `"Restart now" in ${secs}…`;
    clearInterval(_restartTimer);
    _restartTimer = setInterval(() => {
        secs--;
        if (secs > 0) { countdown.textContent = `"Restart now" in ${secs}…`; }
        else { clearInterval(_restartTimer); countdown.textContent = ''; nowBtn.disabled = false; }
    }, 1000);
    nrRestartPoll();
    bootstrap.Modal.getOrCreateInstance(document.getElementById('restartModal')).show();
}

// "when finished" accepted: the list stays, refreshing, with Cancel per row
function nrShowRestartWaiting() {
    _restartWaiting = true;
    clearInterval(_restartTimer);
    document.getElementById('restartCountdown').textContent = '';
    document.getElementById('restartChoice').style.display = 'none';
    document.getElementById('restartWaiting').style.display = '';
    document.getElementById('restartWhenDoneBtn').style.display = 'none';
    document.getElementById('restartConfirmBtn').style.display = 'none';
    document.getElementById('restartCloseBtn').textContent = 'Close';
    nrRestartPoll();
}

document.addEventListener('DOMContentLoaded', () => {
    const modal = document.getElementById('restartModal');
    if (!modal) return;
    modal.addEventListener('hidden.bs.modal', () => {
        clearInterval(_restartTimer);
        clearInterval(_restartListTimer);
        const nowBtn = document.getElementById('restartConfirmBtn');
        if (nowBtn) nowBtn.disabled = true;
        if (_restartWaiting) window.location.reload();   // the drain banner takes over
    });
});

function doRestart(mode) {
    if (mode === 'now')
        bootstrap.Modal.getOrCreateInstance(document.getElementById('restartModal')).hide();
    else
        document.getElementById('restartWhenDoneBtn').disabled = true;
    nrFireRestart(mode);
}

async function nrInstanceToken() {
    try {
        const r = await fetch('/_netrollout/instance', {cache: 'no-store'});
        return r.ok ? (await r.json()).instance : null;
    } catch (e) { return null; }
}

function nrSetRestartButton(restarting) {
    const btn = document.getElementById(NR_RESTART_BUTTON.btn);
    if (!btn) return;
    btn.querySelector('i').className = restarting
        ? 'bi bi-arrow-clockwise ' + NR_RESTART_BUTTON.icon + ' spin'
        : 'bi bi-arrow-clockwise ' + NR_RESTART_BUTTON.icon;
    btn.querySelector(NR_RESTART_BUTTON.label).textContent = restarting ? 'RESTARTING…' : 'RESTART';
    btn.style.color = restarting ? '#ef9a9a' : '';
    btn.onclick = restarting ? null : nrRestartClick;
}

async function nrFireRestart(mode) {
    nrSetRestartButton(true);
    const before = await nrInstanceToken();
    let resp, data = {};
    try {
        resp = await fetch('/admin/server/restart', {
            method: 'POST',
            headers: {'X-CSRFToken': CSRF_TOKEN, 'Content-Type': 'application/json'},
            body: JSON.stringify(mode ? {mode} : {}),
        });
        data = await resp.json();
    } catch (e) { resp = null; }
    if (resp && resp.status === 409 && data.running !== undefined) {
        // Rollouts started since the check: ask
        nrSetRestartButton(false);
        nrShowRestartChoice(data);
        return;
    }
    if (resp && !resp.ok) {
        nrSetRestartButton(false);
        const whenDone = document.getElementById('restartWhenDoneBtn');
        if (whenDone) whenDone.disabled = false;
        nrAlert(data.message || `Restart failed (${resp.status})`, 'Not restarted');
        return;
    }
    if (mode === 'when_finished' && (data.running || 0) > 0) nrShowRestartWaiting();
    else if (mode === 'when_finished')    // nothing running: it restarts at once
        bootstrap.Modal.getOrCreateInstance(document.getElementById('restartModal')).hide();
    // Back when a different instance answers
    const poll = setInterval(async () => {
        const now = await nrInstanceToken();
        if (now && now !== before) { clearInterval(poll); window.location.replace('/'); }
    }, 1000);
}
