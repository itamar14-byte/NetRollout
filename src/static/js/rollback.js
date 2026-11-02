// The rollback window - see templates/_rollback.html, which loads it.
(function () {
    let rollbackModal = null;

    window.openRollback = function(jobId) {
        document.getElementById("rollbackJobId").value = jobId;
        document.getElementById("rollbackCommands").value = "";
        document.getElementById("rollbackVerify").checked = true;
        document.getElementById("rollbackVerbose").checked = false;
        const err = document.getElementById("rollbackError");
        err.style.display = "none";
        err.textContent = "";
        document.getElementById("rollbackProfile").style.display = "none";
        if (!rollbackModal) {
            rollbackModal = new bootstrap.Modal(
                document.getElementById("rollbackModal"));
        }
        rollbackModal.show();
    };

    window.submitRollback = function() {
        const jobId    = document.getElementById("rollbackJobId").value;
        const commands = document.getElementById("rollbackCommands").value.trim();
        if (!commands) {
            document.getElementById("rollbackCommands").focus();
            return;
        }
        const body = {
            commands,
            verify: document.getElementById("rollbackVerify").checked,
            verbose: document.getElementById("rollbackVerbose").checked
        };
        // the profile picked for devices without your profile (asked below)
        if (document.getElementById("rollbackProfile").style.display !== "none") {
            const picked = document.getElementById("rollbackProfileSelect").value;
            if (!picked) {
                document.getElementById("rollbackProfileSelect").focus();
                return;
            }
            body.profile_id = picked;
            body.link = document.getElementById("rollbackLink").checked;
        }
        fetch(`/rollout/rollback/${encodeURIComponent(jobId)}`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
                "X-CSRFToken": CSRF_TOKEN
            },
            body: JSON.stringify(body)
        })
        .then(r => r.json())
        .then(data => {
            const err = document.getElementById("rollbackError");
            if (data.status === "ok") {
                rollbackModal.hide();
                window.location.href = `/active_jobs?new=${encodeURIComponent(data.job_id)}`;
            } else if (data.needs_profile) {
                err.style.display = "none";
                askProfile(data.needs_profile, data.profiles || []);
            } else {
                err.textContent = data.message || "Rollback failed.";
                err.style.display = "block";
            }
        });
    };

    // Devices without your security profile: pick one of your profiles (as text:
    // labels and names are other people's input)
    function askProfile(devices, profiles) {
        document.getElementById("rollbackProfileDevices").textContent =
            devices.map(d => d.label || d.ip).join(", ");
        const select = document.getElementById("rollbackProfileSelect");
        select.replaceChildren();
        const prompt = document.createElement("option");
        prompt.value = "";
        prompt.textContent = profiles.length ? "Choose one of your profiles…" : "No profiles";
        select.append(prompt);
        profiles.forEach(p => {
            const option = document.createElement("option");
            option.value = p.id;
            option.textContent = p.label;
            select.append(option);
        });
        select.disabled = !profiles.length;
        document.getElementById("rollbackNoProfiles").style.display = profiles.length ? "none" : "block";
        const linkable = devices.filter(d => d.linkable).length;
        document.getElementById("rollbackLinkRow").style.display = linkable ? "flex" : "none";
        // named: the list above may hold another user's own devices too (as text)
        document.getElementById("rollbackLinkWhich").textContent =
            devices.filter(d => d.linkable).map(d => d.label || d.ip).join(", ");
        document.getElementById("rollbackLink").checked = true;
        document.getElementById("rollbackOnceNote").style.display =
            devices.some(d => !d.linkable) ? "block" : "none";
        document.getElementById("rollbackProfile").style.display = "block";
        if (profiles.length) select.focus();
    }
})();
