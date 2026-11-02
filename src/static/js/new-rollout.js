// New Rollout - see templates/new_rollout.html (data-held-url).
(function () {
    /* ── Vendor logo URLs (the page's nr-vendor-logos) ── */
    const VENDOR_LOGOS = nrData('nr-vendor-logos');

    /* ── Per device: the tokens you bound on it → null (it fills in) or why
       it can't. Devices without your mappings are absent. ── */
    const TOKEN_STATES = nrData('nr-token-states');

    /* ── Devices in a rollout now: device id → whose (holds()); Recheck
       asks again ── */
    const HELD = nrData('nr-held');
    const HELD_URL = document.currentScript.dataset.heldUrl;     // Recheck asks it
    // a $$TOKEN$$ in a command - the engine's TOKEN_IN_COMMAND, exact case
    const TOKEN_RE = /\$\$[A-Za-z0-9_]{1,64}\$\$/g;

    /* ── State ── */
    let duplicateCount = 0;       // clashes found by markDuplicateTargets

    /* ── Elements ── */
    const form          = document.getElementById("rolloutForm");
    const submitBtn     = document.getElementById("submitBtn");
    const selectAll     = document.getElementById("selectAll");
    const deviceSearch  = document.getElementById("deviceSearch");
    const selCount      = document.getElementById("selectionCount");
    const platformSumEl = document.getElementById("platformSummary");
    const mpWarning     = document.getElementById("mpWarning");
    const cmdBlocks     = document.getElementById("commandBlocks");
    const pcInput       = document.getElementById("platformCommandsInput");
    const noProfileWarn = document.getElementById("noProfileWarning");

    /* ── Show no-profile warning if any disabled devices exist ── */
    if (document.querySelector('.device-row[data-no-profile="1"]')) {
        noProfileWarn.classList.add("show");
    }

    /* ── Helpers ── */
    function checkedBoxes() {
        return [...document.querySelectorAll(".device-cb:checked")];
    }

    function getSelectedPlatforms() {
        const map = {}; // platform → [label, ...]
        checkedBoxes().forEach(cb => {
            const p = cb.dataset.platform;
            if (!map[p]) map[p] = [];
            map[p].push(cb.dataset.label);
        });
        return map;
    }

    /* ── Build a single command block (its header's name: rebuildCommandBlocks) ── */
    function buildCmdBlock(platform) {
        const blockId = "cb_" + platform.replace(/[^a-z0-9]/g, "_");
        const taId    = "ta_" + platform.replace(/[^a-z0-9]/g, "_");
        const fnId    = "fn_" + platform.replace(/[^a-z0-9]/g, "_");
        const fiId    = "fi_" + platform.replace(/[^a-z0-9]/g, "_");

        const logoUrl = VENDOR_LOGOS[platform];
        const logoHtml = logoUrl
            ? `<img src="${nrEscape(logoUrl)}" alt="${nrEscape(platform)}"
                    style="width:16px;height:16px;object-fit:contain;
                           filter:brightness(0) invert(1);opacity:0.5;
                           margin-right:0.45rem;"
                    data-nr-error="nrHide">`
            : `<i class="bi bi-hdd-network" style="font-size:0.85rem;color:var(--nr-text-faint);margin-right:0.45rem;"></i>`;

        const wrap = document.createElement("div");
        wrap.className = "cmd-section mb-3";
        wrap.id = blockId;
        wrap.innerHTML = `
            <div class="cmd-section-header">
                <span class="cmd-platform-label" style="display:flex;align-items:center;">
                    ${logoHtml}<span class="cmd-platform-name"></span>
                </span>
                <label class="cmd-load-btn" for="${fiId}"
                       title="Put a .txt file's commands in the box - you see and can edit them before Launch">
                    <i class="bi bi-file-earmark-text"></i>Load from file…
                </label>
                <input type="file" id="${fiId}" accept=".txt,text/plain" hidden>
            </div>
            <textarea class="cmd-textarea" id="${taId}"
                      placeholder="interface GigabitEthernet0/1&#10;  description uplink&#10;  no shutdown"
                      spellcheck="false" autocapitalize="off" autocorrect="off" autocomplete="off"
                      rows="7"></textarea>
            <div class="cmd-loaded" id="${fnId}" hidden></div>
        `;

        const ta = wrap.querySelector(`#${taId}`);
        const fi = wrap.querySelector(`#${fiId}`);
        const fn = wrap.querySelector(`#${fnId}`);
        ta.addEventListener("input", () => { fn.hidden = true; updateLaunch(); });

        /* A command file goes into the box - seen, and editable, before Launch:
           UTF-8 (a BOM dropped, as Notepad writes one), anything else refused */
        fi.addEventListener("change", async () => {
            const file = fi.files[0];
            fi.value = "";              // the same file can be loaded again
            if (!file) return;
            let text;
            try {
                text = new TextDecoder("utf-8", {fatal: true}).decode(await file.arrayBuffer());
            } catch (err) {
                fn.textContent = `${file.name} isn't UTF-8 text - save it as UTF-8 and load it again.`;
                fn.className = "cmd-loaded is-error";
                fn.hidden = false;
                return;
            }
            if (ta.value.trim() && ta.value.trim() !== text.trim()
                    && !await nrConfirm({title: "Replace the commands?",
                                         message: `The box already holds commands. Replace them with ${file.name}'s?`,
                                         confirmLabel: "Replace"})) return;
            ta.value = text.trim();
            const count = ta.value.split("\n").filter(line => line.trim()).length;
            fn.textContent = `Loaded ${file.name} - ${count} command${count !== 1 ? "s" : ""}. ` +
                             "Check them, edit if needed, then Launch.";
            fn.className = "cmd-loaded";
            fn.hidden = false;
            updateLaunch();
        });

        return wrap;
    }

    /* ── Rebuild command blocks from current selection ── */
    function rebuildCommandBlocks() {
        const platforms = getSelectedPlatforms();
        const keys = Object.keys(platforms);
        const isMulti = keys.length > 1;

        mpWarning.classList.toggle("show", isMulti);

        /* A platform still selected keeps its block element as it is - the
           typed (or loaded) commands, the listeners; only its name changes. Others go; new ones are built. */
        const kept = {};
        cmdBlocks.querySelectorAll(".cmd-section").forEach(el => { kept[el.id] = el; });

        cmdBlocks.replaceChildren();

        keys.forEach(platform => {
            const blockId = "cb_" + platform.replace(/[^a-z0-9]/g, "_");
            const block   = kept[blockId] || buildCmdBlock(platform);
            block.querySelector(".cmd-platform-name").textContent =
                isMulti ? platform : "Commands";
            cmdBlocks.appendChild(block);
        });
        updateLaunch();     // the commands each device gets may have changed
    }

    /* ── Update footer count + platform pills ── */
    function updateFooter() {
        const checked = checkedBoxes();
        const count   = checked.length;
        selCount.textContent = count === 0
            ? "0 selected"
            : `${count} device${count !== 1 ? "s" : ""} selected`;

        const platforms = [...new Set(checked.map(cb => cb.dataset.platform))];
        platformSumEl.innerHTML = platforms
            .map(p => `<span class="platform-pill">${nrEscape(p)}</span>`)
            .join("");

        duplicateCount = markDuplicateTargets(checked);
        updateLaunch();
    }

    /* ── Launch: held with nothing ticked, a target twice, or a token that
       can't be filled in ── */
    function updateLaunch() {
        const checked = checkedBoxes();
        const blocked = markUnresolvedTokens(checked);
        submitBtn.disabled = checked.length === 0 || duplicateCount > 0 || blocked > 0;
    }

    /* ── The commands a platform's devices get: its block's box (typed or
       loaded from a file) ── */
    function platformCommands(platform) {
        const suffix = platform.replace(/[^a-z0-9]/g, "_");
        const box    = document.getElementById("ta_" + suffix);
        return box ? box.value : "";
    }

    /* ── A $$TOKEN$$ in the commands a ticked device gets (its platform's)
       that isn't bound on it, or has no value there (TOKEN_STATES): flag the
       row, name it in the footer and hold Launch until fixed or unticked.
       The server refuses it too. Returns the number of devices held. ── */
    const tokenWarn = document.getElementById("tokenWarning");
    function markUnresolvedTokens(checked) {
        document.querySelectorAll(".device-row").forEach(row => {
            row.classList.remove("token-blocked");
            const note = row.querySelector(".token-note");
            if (note) { note.hidden = true; note.title = ""; }
        });
        const tokensOf = {};    // platform → its commands' tokens, each once
        const blocked = [];     // [row, problems]
        checked.forEach(cb => {
            const platform = cb.dataset.platform;
            if (!(platform in tokensOf)) {
                tokensOf[platform] = [...new Set(platformCommands(platform).match(TOKEN_RE) || [])];
            }
            const row = cb.closest(".device-row");
            const bound = TOKEN_STATES[row.dataset.deviceId] || {};
            const problems = tokensOf[platform].flatMap(token =>
                !(token in bound) ? [token + ": no mapping on this device"]
                : bound[token] !== null ? [token + ": " + bound[token]]
                : []);
            if (problems.length) blocked.push([row, problems]);
        });
        blocked.forEach(([row, problems]) => {
            const note = row.querySelector(".token-note");
            note.textContent = "unresolved token";   // the footer says which
            note.title = problems.join("\n");
            note.hidden = false;
            row.classList.add("token-blocked");
        });
        tokenWarn.textContent = blocked.length
            ? "Tokens that can't be filled in - fix the commands or the " +
              "device's mappings and attributes, or untick: " +
              blocked.map(([row, problems]) =>
                  row.querySelector(".device-label").textContent.trim() +
                  " (" + row.dataset.endpoint + "): " + problems.join(", ")).join("; ")
            : "";
        tokenWarn.classList.toggle("show", blocked.length > 0);
        return blocked.length;
    }

    /* ── Same ip:port ticked twice = the same box twice (e.g. your own entry
       and a global one): flag both rows and hold Start until one is
       unticked. The server refuses it too. Returns the number of clashes. ── */
    const dupWarn = document.getElementById("dupWarning");
    function markDuplicateTargets(checked) {
        const byEndpoint = {};
        checked.forEach(cb => {
            const row = cb.closest(".device-row");
            (byEndpoint[row.dataset.endpoint] ||= []).push(row);
        });
        document.querySelectorAll(".device-row").forEach(row => {
            row.classList.remove("dup-conflict");
            const note = row.querySelector(".dup-note");
            if (note) note.hidden = true;
        });
        const clashes = Object.entries(byEndpoint).filter(([, rows]) => rows.length > 1);
        clashes.forEach(([, rows]) => rows.forEach(row => {
            const others = rows.filter(r => r !== row)
                .map(r => r.querySelector(".device-label").textContent.trim());
            const note = row.querySelector(".dup-note");
            note.textContent = "same target";   // the footer names them
            note.title = "Same ip:port as " + others.join(", ") +
                         " - untick one";
            note.hidden = false;
            row.classList.add("dup-conflict");
        }));
        dupWarn.textContent = clashes.length
            ? "The same target is selected more than once - untick one of: " +
              clashes.map(([ep, rows]) => rows.map(r =>
                  r.querySelector(".device-label").textContent.trim()).join(" / ") +
                  " (" + ep + ")").join("; ")
            : "";
        dupWarn.classList.toggle("show", clashes.length > 0);
        return clashes.length;
    }

    /* ── Selection events ── */
    document.querySelectorAll(".device-cb").forEach(cb => {
        cb.addEventListener("change", () => {
            const row = cb.closest(".device-row");
            row.classList.toggle("selected", cb.checked);
            updateFooter();
            rebuildCommandBlocks();
            syncSelectAll();
        });
    });

    function syncSelectAll() {
        const visible = [...document.querySelectorAll(".device-row")]
            .filter(r => r.style.display !== "none" && !r.classList.contains("disabled"));
        const checked = visible.filter(r => r.querySelector(".device-cb").checked);
        selectAll.indeterminate = checked.length > 0 && checked.length < visible.length;
        selectAll.checked = visible.length > 0 && checked.length === visible.length;
    }

    /* The device list (select-all, search, its footer, Recheck) is drawn
       only when the user sees devices; without one there is nothing to
       wire up (and the page must not stop on a missing element). */
    const hasDeviceList = selectAll !== null;

    if (hasDeviceList) selectAll.addEventListener("change", () => {
        const visible = [...document.querySelectorAll(".device-row")]
            .filter(r => r.style.display !== "none" && !r.classList.contains("disabled"));
        visible.forEach(row => {
            const cb = row.querySelector(".device-cb");
            cb.checked = selectAll.checked;
            row.classList.toggle("selected", cb.checked);
        });
        updateFooter();
        rebuildCommandBlocks();
    });

    /* ── Search ── */
    if (hasDeviceList) deviceSearch.addEventListener("input", () => {
        const q = deviceSearch.value.toLowerCase();
        document.querySelectorAll(".device-row").forEach(row => {
            const match = row.dataset.label.includes(q) || row.dataset.ip.includes(q);
            row.style.display = match ? "" : "none";
        });
        // Hide a group (and its header) when search filters out all its rows
        document.querySelectorAll(".device-group").forEach(group => {
            const anyVisible = [...group.querySelectorAll(".device-row")]
                .some(r => r.style.display !== "none");
            group.style.display = anyVisible ? "" : "none";
        });
        syncSelectAll();
    });

    /* ── Form submit ── */
    form.addEventListener("submit", e => {
        e.preventDefault();
        doSubmit();
    });

    /* A platform's commands missing or unreadable: said in its own block (in
       red, under the commands), which is scrolled to and focused; the next
       Launch clears it */
    function commandsError(block, message) {
        const note = document.createElement("div");
        note.className = "cb-error";
        note.setAttribute("role", "alert");
        note.innerHTML = '<i class="bi bi-exclamation-circle me-1"></i>';
        note.append(message);
        block.appendChild(note);
        block.scrollIntoView({behavior: "smooth", block: "center"});
        const field = block.querySelector("textarea");
        if (field) field.focus({preventScroll: true});
    }

    async function doSubmit() {
        if (submitBtn.disabled) return;     // held: see updateLaunch
        const platforms = getSelectedPlatforms();
        const keys = Object.keys(platforms);
        const isMulti = keys.length > 1;
        document.querySelectorAll(".cb-error").forEach(e => e.remove());

        if (isMulti) {
            /* Multi-platform: collect per-platform commands into JSON */
            const platformCommands = {};
            let valid = true;

            for (const platform of keys) {
                const blockId = "cb_" + platform.replace(/[^a-z0-9]/g, "_");
                const taId    = "ta_" + platform.replace(/[^a-z0-9]/g, "_");
                const block   = document.getElementById(blockId);
                const text = block.querySelector(`#${taId}`)?.value?.trim();
                if (!text) {
                    commandsError(block, `No commands provided for ${platform}.`);
                    valid = false;
                    break;
                }
                platformCommands[platform] = text;
            }

            if (!valid) return;
            pcInput.value = JSON.stringify(platformCommands);
        } else {
            /* Single-platform: the box's text as manual_commands */
            pcInput.value = "";
            const platform = keys[0];
            const blockId  = "cb_" + platform.replace(/[^a-z0-9]/g, "_");
            const taId     = "ta_" + platform.replace(/[^a-z0-9]/g, "_");
            const block    = document.getElementById(blockId);
            const text = block.querySelector(`#${taId}`)?.value?.trim();
            if (!text) { commandsError(block, "No commands provided."); return; }
            let mcField = form.querySelector("textarea[name='manual_commands']");
            if (!mcField) {
                mcField = document.createElement("textarea");
                mcField.name = "manual_commands";
                mcField.style.display = "none";
                form.appendChild(mcField);
            }
            mcField.value = text;
        }

        form.submit();
    }

    /* ── Reachability: unreachable devices can't be selected (blocked on the
       server too, which re-probes at submit) ── */
    const recheckBtn  = document.getElementById("reachRecheck");
    const unreachWarn = document.getElementById("unreachWarning");

    // Only devices confirmed reachable are selectable: rows start disabled
    // ("checking") and are enabled one by one as their status arrives.
    function setRowTip(row, text) {
        const tip = row.querySelector(".reach-tip");
        if (tip) tip.textContent = text;
    }

    /* A device in another rollout can't be selected until it ends (refused
       on the server too): its chip and tooltip say whose, the footer counts */
    const heldWarn = document.getElementById("heldWarning");
    function applyHeld(held) {
        let n = 0;
        document.querySelectorAll(".device-row[data-device-id]").forEach(row => {
            const hold = held[row.dataset.deviceId];
            row.dataset.held = hold ? "1" : "";
            row.dataset.heldText = hold ? hold.text : "";
            row.querySelector(".state-chip--held").hidden = !hold;
            if (hold) n++;
        });
        heldWarn.textContent = n
            ? n + " device" + (n !== 1 ? "s are" : " is") + " in another rollout - " +
              (n !== 1 ? "they" : "it") + " can't be selected until that rollout ends."
            : "";
        heldWarn.classList.toggle("show", n > 0);
    }

    async function refreshReachability(refresh = false) {
        if (refresh) {
            try {
                const resp = await fetch(HELD_URL);
                if (resp.ok) applyHeld((await resp.json()).held);
            } catch (e) { /* the launch checks again */ }
        }
        const rows = [...document.querySelectorAll(".device-row[data-device-id]")];
        rows.forEach(row => {
            nrReachChecking(row.querySelector(".reach"));
            if (row.dataset.noProfile === "1") return;
            const cb = row.querySelector(".device-cb");
            row.dataset.wasChecked = cb.checked ? "1" : "";  // restore if still up
            cb.checked = false;
            cb.disabled = true;
            row.classList.remove("selected");
            row.classList.add("disabled");
            setRowTip(row, "Checking reachability…");
        });
        updateFooter(); rebuildCommandBlocks(); syncSelectAll();
        recheckBtn.disabled = true;

        const statuses = await nrCheckReachability(
            rows.map(r => r.dataset.deviceId), refresh);
        let down = 0, unknown = 0;
        rows.forEach(row => {
            const status = statuses[row.dataset.deviceId];
            const state  = nrSetReach(row.querySelector(".reach"), status);
            row.querySelector(".state-chip--down").hidden = state !== "down";
            if (row.dataset.noProfile === "1") return;   // stays disabled
            if (row.dataset.held === "1") {              // stays disabled too
                setRowTip(row, row.dataset.heldText);
                return;
            }
            const cb = row.querySelector(".device-cb");
            if (state === "up") {
                cb.disabled = false;
                cb.checked = row.dataset.wasChecked === "1";
                row.classList.toggle("selected", cb.checked);
                row.classList.remove("disabled");
            } else if (state === "down") {
                down++;
                setRowTip(row, "Not reachable from NetRollout" + nrReachWhy(status) +
                               " · checked " + nrAgo(status.checked_at));
            } else {
                unknown++;
                setRowTip(row, "Couldn't check reachability - try Recheck");
            }
        });
        const parts = [];
        if (down)    parts.push(down + " device" + (down !== 1 ? "s" : "") +
                                " not reachable from NetRollout");
        if (unknown) parts.push(unknown + " couldn't be checked");
        unreachWarn.textContent = parts.length
            ? parts.join(", ") + " - they can't be selected. Recheck once back online."
            : "";
        unreachWarn.classList.toggle("show", parts.length > 0);
        recheckBtn.disabled = false;
        updateFooter();
        rebuildCommandBlocks();
        syncSelectAll();
    }

    if (hasDeviceList) {
        applyHeld(HELD);
        recheckBtn.addEventListener("click", () => refreshReachability(true));
        refreshReachability();
    }
})();
