// Active Jobs - see templates/active_jobs.html (data-has-jobs, data-show-others).
(function () {

    /* ── Live clock ── */
    function updateClock() {
        const now = new Date();
        document.getElementById("liveClock").textContent =
            now.toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit", hourCycle:"h23"});
    }
    updateClock();
    setInterval(updateClock, 1000);

    /* ── Elapsed timers ── */
    function updateElapsed() {
        document.querySelectorAll(".elapsed-timer").forEach(el => {
            const iso = el.dataset.started;
            if (!iso) return;
            const diff = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
            const h = String(Math.floor(diff / 3600)).padStart(2, "0");
            const m = String(Math.floor((diff % 3600) / 60)).padStart(2, "0");
            const s = String(diff % 60).padStart(2, "0");
            el.textContent = `${h}:${m}:${s}`;
        });
    }
    updateElapsed();
    setInterval(updateElapsed, 1000);

    /* ── Auto-refresh (page reloads every 15s while jobs are active) ── */
    const hasJobs = document.currentScript.dataset.hasJobs === 'true';
    const dot     = document.getElementById("refreshDot");
    const label   = document.getElementById("refreshLabel");

    if (hasJobs) {
        dot.classList.add("ticking");
        let countdown = 15;
        let refreshPaused = false;
        label.textContent = `refresh in ${countdown}s`;
        const iv = setInterval(() => {
            if (refreshPaused) return;
            countdown--;
            label.textContent = `refresh in ${countdown}s`;
            if (countdown <= 0) {
                clearInterval(iv);
                const url = new URL(window.location.href);
                url.searchParams.delete("new");
                url.searchParams.set("_bg", "1");   // an automatic reload: not activity
                window.location.href = url.toString();
            }
        }, 1000);

        window._pauseRefresh = function() {
            refreshPaused = true;
            label.textContent = `refresh paused`;
            dot.classList.remove("ticking");
        };
        window._resumeRefresh = function() {
            if (Object.keys(openStreams).length > 0) return; // other panels still open
            if (rollbackOpen) return;   // a reload would lose the typed commands
            refreshPaused = false;
            dot.classList.add("ticking");
            label.textContent = `refresh in ${countdown}s`;
        };
    } else {
        label.textContent = "idle";
        window._pauseRefresh  = function() {};
        window._resumeRefresh = function() {};
    }

    /* ── Cancel a job: asked first; no automatic reload while it asks.
          On window: the Cancel form's onsubmit calls it (inside this
          function it was unreachable - every Cancel did nothing) ── */
    window.confirmCancel = async function(form) {
        _pauseRefresh();
        const yes = await nrConfirm({title: 'Cancel this job?', danger: true,
            message: 'Devices it has not reached are skipped. A device already being ' +
                     'configured finishes its push, save and verification - never cut off halfway.',
            confirmLabel: 'Cancel job', cancelLabel: 'Keep it running'});
        if (yes) form.submit();
        else _resumeRefresh();
    };

    /* ── No automatic reload while the rollback dialog is open ── */
    let rollbackOpen = false;
    const rollbackEl = document.getElementById("rollbackModal");
    rollbackEl.addEventListener("shown.bs.modal", () => {
        rollbackOpen = true;
        _pauseRefresh();
    });
    rollbackEl.addEventListener("hidden.bs.modal", () => {
        rollbackOpen = false;
        _resumeRefresh();
    });

    /* ── Log panel toggle ── */
    const openStreams = {}; // job_id → EventSource

    window.toggleLog = function(btn) {
        const jobId  = btn.dataset.jobId;
        // the panel under this button's row: an admin's own jobs are in both
        // views, so their ids are not unique
        const panel  = btn.closest("tr").nextElementSibling;
        const logOut = panel.querySelector(".log-terminal");
        const isOpen = panel.classList.contains("open");

        if (isOpen) {
            panel.classList.remove("open");
            btn.classList.remove("active");
            if (openStreams[jobId]) {
                openStreams[jobId].close();
                delete openStreams[jobId];
            }
            _resumeRefresh();
            return;
        }

        panel.classList.add("open");
        btn.classList.add("active");
        logOut.innerHTML = "";
        _pauseRefresh();

        const es = new EventSource(`/rollout/stream/${jobId}`);
        openStreams[jobId] = es;
        let streamClosedCleanly = false;

        es.onmessage = function(e) {
            if (!e.data.trim()) return;
            const line = document.createElement("div");
            line.innerHTML = e.data;
            logOut.appendChild(line);
            logOut.scrollTop = logOut.scrollHeight;
        };

        es.addEventListener('done', async function() {
            streamClosedCleanly = true;
            es.close();
            delete openStreams[jobId];
            _resumeRefresh();
            // nothing came: the job ended before the log opened - its live lines
            // are gone, so show its log file instead
            if (!logOut.childElementCount) await showLogFile(jobId, logOut);
            showCompletionCard(jobId, logOut);
        });

        es.onerror = function() {
            es.close();
            delete openStreams[jobId];
            _resumeRefresh();
            if (!streamClosedCleanly) {
                const line = document.createElement("div");
                line.innerHTML = "<span style='color:#ef5350;'>⚠ Stream ended.</span>";
                logOut.appendChild(line);
            }
        };
    };

    /* ── A finished job's log file (View Log's), in place of its live log ── */
    async function showLogFile(jobId, logOut) {
        let data = null;
        try {
            const resp = await fetch(`/results/log/${jobId}`);
            if (resp.ok) data = await resp.json();
        } catch (e) { /* the completion card still comes */ }
        if (!data || !data.lines.length) return;
        const note = document.createElement("div");
        note.className = "log-file-note";
        note.textContent = "The rollout ended before its log opened - its log file:";
        logOut.appendChild(note);
        data.lines.forEach(line => {
            const row = document.createElement("div");
            const tab = line.indexOf("	");
            if (tab > 0) {
                const time = document.createElement("span");
                time.className = "log-file-time";
                time.textContent = line.slice(0, tab);
                row.append(time, line.slice(tab + 1));
            } else {
                row.textContent = line;
            }
            if (line.includes("ACTION NEEDED")) row.className = "log-file-action";
            logOut.appendChild(row);
        });
        logOut.scrollTop = logOut.scrollHeight;
    }

    /* ── Completion card: the outcome, without having to open Results ── */
    async function showCompletionCard(jobId, logOut) {
        let data = null;
        // the results are stored a moment after the stream ends: retry briefly
        for (let i = 0; i < 15 && !data; i++) {
            try {
                const r = await fetch(`/results/summary/${jobId}`,
                                      {headers: {'X-NR-Background': '1'}});
                if (r.ok) data = await r.json();
            } catch (e) { /* retry */ }
            if (!data) await new Promise(res => setTimeout(res, 1000));
        }
        if (!data) return;
        const parts = ['success', 'review', 'partial', 'failed', 'not verified', 'cancelled']
            .filter(s => data.counts[s]).map(s => `${data.counts[s]} ${s}`);
        let html =
            `<div class="d-flex align-items-center justify-content-between flex-wrap gap-2">` +
            // which job - several can finish in parallel
            `<span><strong>Rollout finished - ${data.comment ? nrEscape(data.comment) + ' ' : ''}` +
            `<span title="${nrEscape(data.job_id)}" style="font-family:'JetBrains Mono',monospace;">` +
            `[${nrEscape(data.job_id.slice(0, 8))}]</span></strong> ` +
            `<span class="status-pill ${nrEscape(data.status.replace(' ', '-'))}">${nrEscape(data.status)}</span> ` +
            `<span class="term muted" style="font-size:0.78rem;">${nrEscape(parts.join(', '))}` +
            ` (of ${data.device_count} device${data.device_count !== 1 ? 's' : ''})</span></span>` +
            `<span class="d-flex gap-2">` +
            // a rollback targets the devices it configured, fully or in part
            (data.counts.success || data.counts.partial
                ? `<button type="button" class="btn btn-sm completion-rollback" ` +
                  `data-job-id="${nrEscape(data.job_id)}" data-nr-click="openRollback">` +
                  `<i class="bi bi-arrow-counterclockwise me-1"></i>Rollback</button>`
                : '') +
            // devices with commands in review or rejected: decided in Results
            (data.to_decide
                ? `<a class="btn btn-sm btn-outline-warning" href="${nrEscape(data.results_url)}">` +
                  `<i class="bi bi-question-circle me-1"></i>Decide ${data.to_decide} ` +
                  `device${data.to_decide !== 1 ? 's' : ''}</a>`
                : '') +
            `<a class="btn btn-sm btn-outline-info" href="${nrEscape(data.results_url)}">` +
            `<i class="bi bi-clock-history me-1"></i>View in Results</a></span></div>`;
        if (data.action_needed.length) {
            html += `<div class="completion-action"><div class="completion-action-title">` +
                `<i class="bi bi-exclamation-triangle-fill me-1"></i>Action needed on ` +
                `${data.action_needed.length} device${data.action_needed.length !== 1 ? 's' : ''}</div>` +
                data.action_needed.map(a =>
                    `<div class="completion-action-item"><span class="completion-action-device">` +
                    `${nrEscape(a.device)}</span>` +
                    a.text.split('\n').map(t => `<div>${nrEscape(t)}</div>`).join('') + `</div>`
                ).join('') + `</div>`;
        }
        const card = document.createElement('div');
        card.className = 'completion-card' + (data.action_needed.length ? ' needs-action' : '');
        card.innerHTML = html;
        logOut.after(card);
        card.scrollIntoView({block: 'nearest'});
    }

    /* ── Admin: all-users toggle ── */
    window.toggleAllUsers = function() {
        const flat  = document.getElementById("flat-view");
        const split = document.getElementById("split-view");
        const btn   = document.getElementById("allUsersToggle");
        if (!split) return;
        const on = split.style.display === "none";
        flat.style.display  = on ? "none" : "";
        split.style.display = on ? ""     : "none";
        btn.style.borderColor = on ? "rgba(0,188,212,0.35)" : "";
        btn.style.color       = on ? "#00bcd4"               : "";
        btn.style.background  = on ? "rgba(0,188,212,0.06)"  : "";
    };

    /* ── Admin: collapsible sections ── */
    window.toggleSection = function(header, bodyId) {
        const body = document.getElementById(bodyId);
        const open = header.classList.toggle("open");
        header.setAttribute("aria-expanded", open ? "true" : "false");
        body.style.display = open ? "" : "none";
    };

    /* ── Admin: a job of yours queued while other users' run (or finish a
          cancel - still in their slot) - what it waits for is theirs, so they
          are shown; their queued ones alone don't ── */
    if (document.currentScript.dataset.showOthers) toggleAllUsers();

    /* ── Arrived to watch one rollout - the dashboard's Watch live, or just
          launched (New Rollout lands here): its log open, in the view shown ── */
    const watchId = new URLSearchParams(window.location.search).get("watch")
                 || new URLSearchParams(window.location.search).get("new");
    if (watchId) {
        const watchBtn = [...document.querySelectorAll(`.btn-log[data-job-id="${CSS.escape(watchId)}"]`)]
            .find(b => b.offsetParent !== null);
        if (watchBtn && !watchBtn.classList.contains("active")) {
            toggleLog(watchBtn);
            watchBtn.closest("tr").scrollIntoView({block: "center"});
        }
    }

})();

// What this page's elements name in data-nr-<event> (js/events.js)
nrHandlers({
    toggleAllUsers: () => { toggleAllUsers(); },
    toggleLog: el => { toggleLog(el); },
    confirmCancel: (el, e) => { e.preventDefault(); confirmCancel(el); },
    toggleSection: el => { toggleSection(el, el.dataset.section); },
});
