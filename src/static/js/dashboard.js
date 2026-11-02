// Dashboard - see templates/dashboard.html (data-created-at, data-job-start).
    // the values this page hands its script: data-created-at, data-job-start
    const DASHBOARD = document.currentScript.dataset;

    // ── Admin KPI scope ──
    function applyKpiScope(val) {
        const url = new URL(window.location.href);
        url.searchParams.set('user', val);
        window.location.href = url.toString();
    }

    // ── Live clock ──
    function updateClock() {
        const now = new Date();
        const d = now.toLocaleDateString('en-GB', { weekday:'short', day:'2-digit', month:'short', year:'numeric' });
        const t = now.toTimeString().slice(0, 8);
        const el = document.getElementById('dashClock');
        if (el) el.textContent = d + '  ' + t;
    }
    updateClock();
    setInterval(updateClock, 1000);

    // ── Tenure counter ──
    const createdAt = new Date(DASHBOARD.createdAt);
    function updateTenure() {
        const now = new Date();
        let diff = Math.floor((now - createdAt) / 1000);
        const seconds = diff % 60; diff = Math.floor(diff / 60);
        const minutes = diff % 60; diff = Math.floor(diff / 60);
        const hours   = diff % 24; diff = Math.floor(diff / 24);
        const days    = diff % 30; diff = Math.floor(diff / 30);
        const months  = diff % 12;
        const years   = Math.floor(diff / 12);
        const parts = [];
        if (years)   parts.push(`${years}y`);
        if (months)  parts.push(`${months}mo`);
        if (days)    parts.push(`${days}d`);
        if (hours)   parts.push(`${hours}h`);
        if (minutes) parts.push(`${minutes}m`);
        parts.push(`${seconds}s`);
        const el = document.getElementById('tenureCounter');
        if (el) el.textContent = parts.join(' ');
    }
    updateTenure();
    setInterval(updateTenure, 1000);

    // ── Active job elapsed timer ──
    if (DASHBOARD.jobStart !== undefined) {      // a job running (not queued)
    const jobStart = new Date(JSON.parse(DASHBOARD.jobStart));
    function updateElapsed() {
        const secs = Math.floor((new Date() - jobStart) / 1000);
        const m = String(Math.floor(secs / 60)).padStart(2, '0');
        const s = String(secs % 60).padStart(2, '0');
        const el = document.getElementById('elapsedTimer');
        if (el) el.textContent = m + ':' + s;
    }
    updateElapsed();
    setInterval(updateElapsed, 1000);
    }

// What this page's elements name in data-nr-<event> (js/events.js)
nrHandlers({
    applyKpiScope: el => { applyKpiScope(el.value); },
});
