// User Management - see templates/admin_users.html.
  // ── Data ──
  const ACTIONS = ["Approve","Enable","Disable","Promote","Demote","TerminateSession","ResetPassword","Reset2FA","Reconnect","Delete"];
  // the LDAP servers an account can be reconnected to: {id, name, active, service_account, sets_roles}
  const LDAP_SERVERS = nrData('nr-ldap-servers');
  const ADD_USER_URL = document.currentScript.dataset.addUserUrl;     // Add user posts there
  const usable = s => s.active && s.service_account;

  const rules = {
    Approve: sel => {
      if (!sel.length) return "No users selected";
      const n = sel.filter(u => u.approved).length;
      return n ? `${n} user(s) already approved` : noDirectory(sel);
    },
    Enable: sel => {
      if (!sel.length) return "No users selected";
      const lost = noDirectory(sel);
      if (lost) return lost;
      const notApproved = sel.filter(u => !u.approved).length;
      if (notApproved) return `${notApproved} user(s) not yet approved`;
      // a locked account is active, but Enable lifts its lock
      const active = sel.filter(u => u.active && !u.locked).length;
      return active ? `${active} user(s) already active` : null;
    },
    Disable: sel => {
      if (!sel.length) return "No users selected";
      if (sel.some(u => u.is_self)) return "Cannot disable your own account";
      const n = sel.filter(u => !u.active).length;
      if (n) return `${n} user(s) already inactive`;
      return noAdminLeft(sel);
    },
    Promote: sel => {
      if (!sel.length) return "No users selected";
      const n = sel.filter(u => u.role === "admin").length;
      if (n) return `${n} user(s) already admin`;
      return noDirectory(sel) || roleFromDirectory(sel);
    },
    Demote: sel => {
      if (!sel.length) return "No users selected";
      if (sel.some(u => u.is_factory)) return "The factory admin can't be demoted";
      if (sel.some(u => u.is_self)) return "Cannot demote your own account";
      const fromDirectory = roleFromDirectory(sel);
      if (fromDirectory) return fromDirectory;
      const n = sel.filter(u => u.role !== "admin").length;
      if (n) return `${n} user(s) are not admin`;
      return noAdminLeft(sel);
    },
    TerminateSession: sel => {
      if (!sel.length) return "No users selected";
      if (sel.some(u => u.is_factory)) return "The factory user's session cannot be terminated";
      const noSession = sel.filter(u => !u.has_session).length;
      return noSession ? `${noSession} user(s) have no active session` : null;
    },
    // One user at a time: one temporary password to hand over
    ResetPassword: sel => {
      if (sel.length !== 1) return sel.length ? "Select one user" : "No users selected";
      const u = sel[0];
      if (u.is_factory) return "The factory admin's password can't be reset";
      if (u.is_self) return "Use Change password (My Account) for your own";
      if (u.auth_type !== "local") return "LDAP passwords are managed in the directory";
      return null;
    },
    // Only local users have 2FA (the factory admin only when it turned it on:
    // a reset turns it off) - LDAP users don't use it
    Reset2FA: sel => {
      if (!sel.length) return "No users selected";
      if (sel.some(u => u.is_factory && u.is_self)) return "Turn your own two-factor sign-in off on My Account";
      const none = sel.filter(u => !u.has_2fa).length;
      return none ? `${none} user(s) have no 2FA set up` : null;
    },
    // directory accounts whose LDAP server was deleted, to another server
    Reconnect: sel => {
      if (!sel.length) return "No users selected";
      const n = sel.filter(u => !u.no_directory).length;
      if (n) return `${n} user(s) aren't directory accounts whose LDAP server was deleted`;
      return LDAP_SERVERS.some(usable) ? null
        : "No LDAP server that's on and has a service account to check the names with";
    },
    Delete: sel => {
      if (!sel.length) return "No users selected";
      if (sel.some(u => u.is_factory)) return "Factory user cannot be deleted";
      if (sel.some(u => u.is_self))    return "Cannot delete your own account";
      return noAdminLeft(sel);
    }
  };

  // An account whose LDAP server was deleted can't sign in: enabling it
  // waits until it's reconnected (the server refuses it too)
  function noDirectory(sel) {
    const n = sel.filter(u => u.no_directory).length;
    return n ? `${n} user(s) have no directory - reconnect them first` : null;
  }

  // A directory user whose role its group rules set (Group Rules below): the
  // next sign-in decides it, so the server refuses Promote / Demote
  function roleFromDirectory(sel) {
    const n = sel.filter(u => u.role_from_directory).length;
    return n ? `${n} user(s) get their role from directory group rules` : null;
  }

  // The server refuses taking away the last administrator who can sign in;
  // the page says so before (approved, active admins - the server also
  // leaves out directory admins whose LDAP server is off)
  function noAdminLeft(sel) {
    const leaving = new Set(sel.map(u => u.id));
    const left = Array.from(document.querySelectorAll("tr[data-user]"))
      .map(tr => JSON.parse(tr.dataset.user))
      .filter(u => u.role === "admin" && u.approved && u.active && !leaving.has(u.id));
    return left.length ? null : "No active administrator would be left";
  }

  // ── A Locked badge's tooltip counts the lock down (mm:ss); the badge goes
  // when the lock ends ──
  document.querySelectorAll(".lock-badge").forEach(badge => {
    const end = Date.now() + Number(badge.dataset.seconds) * 1000;
    const tick = () => {
      const left = Math.max(0, Math.ceil((end - Date.now()) / 1000));
      if (!left) { clearInterval(timer); badge.remove(); return; }
      const mmss = `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
      badge.title = `Locked after 5 wrong passwords in a row: ${mmss} left - Enable / Unlock lifts it now`;
    };
    const timer = setInterval(tick, 1000);
  });

  // ── Tooltip registry ──
  const tooltips = {};

  function getSelected() {
    return Array.from(document.querySelectorAll(".row-cb:checked"))
      .map(cb => JSON.parse(cb.closest("tr").dataset.user));
  }

  function updateToolbar() {
    const sel = getSelected();

    // Enable lifts a lock too: the button says which it does for the users
    // ticked - only locked ones (still active): Unlock; only disabled: Enable
    document.getElementById("enableLabel").textContent =
      sel.length && sel.every(u => u.active && u.locked) ? "Unlock"
      : sel.length && sel.every(u => !u.active) ? "Enable" : "Enable / Unlock";

    // Selection info label
    const info = document.getElementById("selectionInfo");
    info.textContent = sel.length === 0
      ? "SELECT USERS TO PERFORM ACTIONS"
      : `${sel.length} USER${sel.length !== 1 ? "S" : ""} SELECTED`;
    info.style.color = sel.length ? "#00bcd4" : "var(--nr-text-muted)";

    ACTIONS.forEach(name => {
      const btn  = document.getElementById("btn" + name);
      const wrap = document.getElementById("wrap" + name);
      const reason = rules[name](sel);

      btn.disabled = !!reason;

      // Dispose existing tooltip
      if (tooltips[name]) { tooltips[name].dispose(); delete tooltips[name]; }

      if (reason && sel.length > 0) {
        wrap.setAttribute("data-bs-toggle", "tooltip");
        wrap.setAttribute("data-bs-placement", "top");
        wrap.setAttribute("data-bs-title", reason);
        tooltips[name] = new bootstrap.Tooltip(wrap, { trigger: "hover" });
      } else {
        wrap.removeAttribute("data-bs-toggle");
        wrap.removeAttribute("data-bs-title");
      }
    });
  }

  // ── Checkboxes ──
  document.querySelectorAll(".row-cb").forEach(cb => {
    cb.addEventListener("change", function() {
      this.closest("tr").classList.toggle("row-selected", this.checked);
      updateToolbar();
    });
  });

  document.getElementById("selectAll").addEventListener("change", function() {
    const visible = Array.from(document.querySelectorAll("#userTable tbody tr"))
      .filter(r => r.style.display !== "none")
      .map(r => r.querySelector(".row-cb"))
      .filter(Boolean);
    visible.forEach(cb => {
      cb.checked = this.checked;
      cb.closest("tr").classList.toggle("row-selected", this.checked);
    });
    updateToolbar();
  });

  // ── Bulk action buttons (non-delete) ──
  ["Approve","Enable","Disable","Promote","Demote","TerminateSession"].forEach(name => {
    document.getElementById("btn" + name).addEventListener("click", () => {
      const sel = getSelected();
      document.getElementById("bulkIds").value = sel.map(u => u.id).join(",");
      document.getElementById("bulkForm").action = `/admin/users/bulk/${name === "TerminateSession" ? "terminate_session" : name.toLowerCase()}`;
      document.getElementById("bulkForm").submit();
    });
  });

  // ── Reconnect: the ticked accounts (their LDAP server was deleted) to a
  // server that can check their names; refused names show here, nothing
  // changed; after it, closing reloads the page ──
  (() => {
    const modalEl = document.getElementById("reconnectModal");
    const serverSel = document.getElementById("reconnectServer");
    const errorEl = document.getElementById("reconnectError");
    const goBtn = document.getElementById("reconnectGo");
    let picked = [], done = false;

    function roleNote() {
      const s = LDAP_SERVERS.find(x => x.id === serverSel.value);
      const admins = picked.filter(u => u.role === "admin").map(u => u.username);
      document.getElementById("reconnectRoleNote").textContent = !s ? "" : s.sets_roles
        ? `${s.name} sets roles from its group rules: they get the role their groups give them now.`
        : `${s.name} has no active group rules: they keep their roles.` +
          (admins.length ? ` ${admins.join(", ")} ${admins.length === 1 ? "is an administrator" : "are administrators"} - demote first if that's wrong.` : "");
    }

    document.getElementById("btnReconnect").addEventListener("click", () => {
      picked = getSelected();
      done = false;
      const list = document.getElementById("reconnectUsers");
      list.replaceChildren(...picked.map(u => {
        const row = document.createElement("div");
        row.className = "reconnect-user";
        const name = document.createElement("span");
        name.className = "ru-name";
        name.textContent = u.username;
        const role = document.createElement("span");
        role.className = "ru-role" + (u.role === "admin" ? " is-admin" : "");
        role.textContent = u.role === "admin" ? "Admin" : "Operator";
        row.append(name, role);
        return row;
      }));
      serverSel.replaceChildren(...LDAP_SERVERS.map(s => {
        const opt = document.createElement("option");
        opt.value = s.id;
        opt.disabled = !usable(s);
        opt.textContent = s.name + (!s.active ? " - switched off"
          : !s.service_account ? " - no service account to check names with" : "");
        return opt;
      }));
      const first = LDAP_SERVERS.find(usable);
      if (first) serverSel.value = first.id;
      roleNote();
      errorEl.hidden = true;
      document.getElementById("reconnectForm").hidden = false;
      document.getElementById("reconnectDone").hidden = true;
      goBtn.hidden = false;
      goBtn.disabled = false;
      document.getElementById("reconnectCancel").textContent = "Cancel";
      bootstrap.Modal.getOrCreateInstance(modalEl).show();
    });
    serverSel.addEventListener("change", () => { errorEl.hidden = true; roleNote(); });

    goBtn.addEventListener("click", async () => {
      goBtn.disabled = true;
      errorEl.hidden = true;
      try {
        const resp = await fetch("/admin/users/reconnect", {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-CSRFToken": CSRF_TOKEN},
          body: JSON.stringify({user_ids: picked.map(u => u.id), server_id: serverSel.value})
        });
        const data = await resp.json();
        if (!resp.ok || data.status !== "ok") throw new Error(data.message || `Not reconnected (${resp.status})`);
        const server = serverSel.options[serverSel.selectedIndex].text;
        let text = `Reconnected to ${server}: ${data.users.join(", ")}.`;
        if (data.sets_roles) {
          text += data.admins.length ? ` ${data.admins.join(", ")} ${data.admins.length === 1 ? "is an administrator" : "are administrators"} by the group rules.`
                                     : " The group rules make them operators.";
        }
        document.getElementById("reconnectDoneText").textContent = text;
        document.getElementById("reconnectForm").hidden = true;
        document.getElementById("reconnectDone").hidden = false;
        goBtn.hidden = true;
        document.getElementById("reconnectCancel").textContent = "Close";
        done = true;
      } catch (e) {
        errorEl.textContent = e.message;
        errorEl.hidden = false;
        goBtn.disabled = false;
      }
    });
    modalEl.addEventListener("hidden.bs.modal", () => { if (done) window.location.reload(); });
  })();

  // ── Reset password flow: confirm → POST → show the temporary password once ──
  let resetPwTarget = null, resetPwDone = false;
  document.getElementById("btnResetPassword").addEventListener("click", () => {
    resetPwTarget = getSelected()[0];
    resetPwDone = false;
    document.getElementById("resetPwUser").textContent = resetPwTarget.username;
    document.getElementById("resetPwConfirm").style.display = "";
    document.getElementById("resetPwResult").style.display = "none";
    document.getElementById("resetPwConfirmBtn").style.display = "";
    document.getElementById("resetPwConfirmBtn").disabled = false;
    document.getElementById("resetPwClose").textContent = "Cancel";
    bootstrap.Modal.getOrCreateInstance(document.getElementById("resetPwModal")).show();
  });

  document.getElementById("resetPwConfirmBtn").addEventListener("click", async () => {
    const btn = document.getElementById("resetPwConfirmBtn");
    btn.disabled = true;
    let data = {};
    try {
      const resp = await fetch(`/admin/users/${resetPwTarget.id}/reset_password`, {
        method: "POST",
        headers: {"X-CSRFToken": CSRF_TOKEN, "X-Requested-With": "XMLHttpRequest"},
      });
      data = await resp.json();
      if (!resp.ok) throw new Error(data.message || `server error (${resp.status})`);
    } catch (e) {
      btn.disabled = false;
      const err = document.getElementById("resetPwError");
      err.textContent = "Not reset: " + e.message;
      err.hidden = false;
      return;
    }
    resetPwDone = true;
    document.getElementById("resetPwUser2").textContent = data.username;
    document.getElementById("resetPwValue").textContent = data.temporary_password;
    document.getElementById("resetPwConfirm").style.display = "none";
    document.getElementById("resetPwResult").style.display = "";
    btn.style.display = "none";
    document.getElementById("resetPwClose").textContent = "Done";
  });

  /* ── Add user ── */
  (() => {
    const modal = document.getElementById("addUserModal");
    const form = document.getElementById("addUserForm");
    const fields = ["auUsername", "auEmail", "auFullName", "auPosition"].map(id => document.getElementById(id));
    const error = document.getElementById("addUserError");
    const submit = document.getElementById("addUserSubmit");
    let added = false;

    document.getElementById("btnAddUser").addEventListener("click", () => {
      form.reset(); added = false;
      error.style.display = "none";
      document.getElementById("addUserFields").style.display = "";
      document.getElementById("addUserResult").style.display = "none";
      submit.style.display = ""; submit.disabled = false;
      document.getElementById("addUserClose").textContent = "Cancel";
      bootstrap.Modal.getOrCreateInstance(modal).show();
    });
    modal.addEventListener("shown.bs.modal", () => { if (!added) fields[0].focus(); });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const [username, email, fullName, position] = fields.map(f => f.value.trim());
      const missing = [[username, "a username"], [email, "an email address"], [fullName, "a full name"]]
        .filter(([v]) => !v).map(([, what]) => what);
      if (missing.length) { error.textContent = "Enter " + missing.join(", ") + "."; error.style.display = ""; return; }
      submit.disabled = true;
      let resp, data = {};
      try {
        resp = await fetch(ADD_USER_URL, {
          method: "POST",
          headers: {"Content-Type": "application/json", "X-CSRFToken": CSRF_TOKEN,
                    "X-Requested-With": "XMLHttpRequest"},
          body: JSON.stringify({username, email, full_name: fullName, position,
                                role: document.getElementById("auRole").value}),
        });
        data = await resp.json();
      } catch (e) { data = {message: "No answer from NetRollout."}; }
      if (!resp || !resp.ok) {
        submit.disabled = false;
        error.textContent = data.message || `Server error (${resp ? resp.status : "no answer"}).`;
        error.style.display = "";
        return;
      }
      added = true;
      document.getElementById("addUserDone").textContent =
        `${data.username} is added as ${data.role === "admin" ? "an admin" : "an operator"}.`;
      document.getElementById("addUserPassword").textContent = data.temporary_password;
      document.getElementById("addUserFields").style.display = "none";
      document.getElementById("addUserResult").style.display = "";
      submit.style.display = "none";
      document.getElementById("addUserClose").textContent = "Done";
    });

    document.getElementById("addUserCopy").addEventListener("click", () => {
      navigator.clipboard.writeText(document.getElementById("addUserPassword").textContent)
        .then(() => { document.querySelector("#addUserCopy i").className = "bi bi-check2"; })
        .catch(() => {});
    });
    modal.addEventListener("hidden.bs.modal", () => {
      document.getElementById("addUserPassword").textContent = "";   // don't keep it in the page
      document.querySelector("#addUserCopy i").className = "bi bi-copy";
      if (added) window.location.reload();                          // the new user in the list
    });
  })();

  document.getElementById("resetPwCopy").addEventListener("click", () => {
    navigator.clipboard.writeText(document.getElementById("resetPwValue").textContent)
      .then(() => { document.querySelector("#resetPwCopy i").className = "bi bi-check2"; })
      .catch(() => {});
  });

  document.getElementById("resetPwModal").addEventListener("hidden.bs.modal", () => {
    document.getElementById("resetPwValue").textContent = "";   // don't keep it in the page
    document.querySelector("#resetPwCopy i").className = "bi bi-copy";
    if (resetPwDone) window.location.reload();                  // sessions / status changed
  });

  // ── Reset 2FA flow (confirmed: it lowers protection until re-enrollment) ──
  document.getElementById("btnReset2FA").addEventListener("click", () => {
    const sel = getSelected();
    document.getElementById("r2faCount").textContent = sel.length;
    document.getElementById("r2faNames").textContent = sel.map(u => u.username).join(", ");
    new bootstrap.Modal(document.getElementById("reset2faModal")).show();
  });

  document.getElementById("r2faConfirmBtn").addEventListener("click", () => {
    const sel = getSelected();
    document.getElementById("bulkIds").value = sel.map(u => u.id).join(",");
    document.getElementById("bulkForm").action = "/admin/users/bulk/reset_2fa";
    document.getElementById("bulkForm").submit();
  });

  // ── Delete flow ──
  document.getElementById("btnDelete").addEventListener("click", () => {
    const sel = getSelected();
    document.getElementById("delCount").textContent = sel.length;
    document.getElementById("delCountLabel").textContent = sel.length;
    document.getElementById("delNames").textContent = sel.map(u => u.username).join(", ");
    document.getElementById("delConfirmInput").value = "";
    document.getElementById("delConfirmBtn").disabled = true;
    new bootstrap.Modal(document.getElementById("deleteModal")).show();
  });

  document.getElementById("delConfirmInput").addEventListener("input", function() {
    const n = getSelected().length;
    document.getElementById("delConfirmBtn").disabled = parseInt(this.value) !== n;
  });

  document.getElementById("delConfirmBtn").addEventListener("click", () => {
    const sel = getSelected();
    document.getElementById("bulkIds").value = sel.map(u => u.id).join(",");
    document.getElementById("bulkForm").action = "/admin/users/bulk/delete";
    document.getElementById("bulkForm").submit();
  });

  // ── Search + status filter: a row shows when it passes both ──
  let statusFilter = "all";
  function applyUserFilters() {
    const q = document.getElementById("userSearch").value.toLowerCase();
    document.querySelectorAll("#userTable tbody tr").forEach(row => {
      const statusOk = statusFilter === "all" || row.dataset.status === statusFilter;
      const searchOk = row.textContent.toLowerCase().includes(q);
      row.style.display = statusOk && searchOk ? "" : "none";
    });
    updateToolbar();
  }

  document.getElementById("userSearch").addEventListener("input", applyUserFilters);

  document.querySelectorAll(".filter-btn").forEach(btn => {
    btn.addEventListener("click", function() {
      document.querySelectorAll(".filter-btn").forEach(b => b.classList.remove("active-filter"));
      this.classList.add("active-filter");
      statusFilter = this.dataset.filter;
      applyUserFilters();
    });
  });

  // ── Column sort ──
  let sortDir = {};
  document.querySelectorAll(".sortable").forEach(th => {
    th.addEventListener("click", function() {
      const col = parseInt(this.dataset.col);
      sortDir[col] = !sortDir[col];
      const tbody = document.querySelector("#userTable tbody");
      const rows  = Array.from(tbody.querySelectorAll("tr"));
      rows.sort((a, b) => {
        const aT = a.cells[col]?.textContent.trim().toLowerCase() || "";
        const bT = b.cells[col]?.textContent.trim().toLowerCase() || "";
        return sortDir[col] ? aT.localeCompare(bT) : bT.localeCompare(aT);
      }).forEach(r => tbody.appendChild(r));
      document.querySelectorAll(".sort-icon").forEach(i => i.textContent = "⇅");
      this.querySelector(".sort-icon").textContent = sortDir[col] ? "▲" : "▼";
      document.querySelectorAll(".sortable").forEach(h => h.removeAttribute("aria-sort"));
      this.setAttribute("aria-sort", sortDir[col] ? "ascending" : "descending");
    });
  });

  // ── Init ──
  updateToolbar();
