// User Management - see templates/admin_users.html.
  window.addEventListener('DOMContentLoaded', loadGroupRules);

  function loadGroupRules() {
    fetch('/admin/server/ldap')
      .then(r => r.json())
      .then(servers => {
        if (!servers.length) {
          document.getElementById('groupRulesContainer').innerHTML =
            '<div class="gr-empty">No LDAP servers configured</div>';
          return;
        }
        return Promise.all(
          servers.map(s =>
            fetch(`/admin/server/ldap/${s.id}/groups`)
              .then(r => r.json())
              .then(groups => ({ server: s, groups }))
          )
        ).then(results => renderGroupRules(results));
      })
      .catch(() => {
        document.getElementById('groupRulesContainer').innerHTML =
          '<div style="color:#ef9a9a; font-size:0.82rem;">Failed to load group rules</div>';
      });
  }

  function renderGroupRules(results) {
    const container = document.getElementById('groupRulesContainer');
    const allGroups = results.flatMap(r => r.groups.map(g => ({ ...g, server: r.server })));

    if (!allGroups.length) {
      container.innerHTML = '<div class="gr-empty">No group rules configured - use the LDAP Explorer to add groups</div>';
      return;
    }

    let html = '<table class="gr-table">';
    html += '<thead><tr><th>Label</th><th>Group DN</th><th>Role</th><th>Server</th><th>Status</th><th><span class="visually-hidden">Actions</span></th></tr></thead><tbody>';

    results.forEach(({ server, groups }) => {
      if (!groups.length) return;
      groups.forEach(g => {
        const roleSelect = `<select class="form-select form-select-sm gr-role ${g.role === 'admin' ? 'gr-role-admin' : 'gr-role-operator'}"
                  aria-label="Role for members of this group"
                  data-server="${nrEscape(server.id)}" data-group="${nrEscape(g.id)}"
                  data-role="${nrEscape(g.role)}" data-label="${nrEscape(g.label)}" data-nr-change="setGroupRole">
            <option value="operator"${g.role === 'admin' ? '' : ' selected'}>Operator</option>
            <option value="admin"${g.role === 'admin' ? ' selected' : ''}>Admin</option>
          </select>`;
        const toggleClass = g.is_active ? 'active' : 'inactive';
        const toggleLabel = g.is_active ? 'ACTIVE' : 'INACTIVE';
        html += `<tr id="gr-row-${g.id}">
          <td style="font-weight:500; color:#e0e0e0;">${nrEscape(g.label)}</td>
          <td><span class="gr-dn" title="${nrEscape(g.group_dn)}">${nrEscape(g.group_dn)}</span></td>
          <td>${roleSelect}</td>
          <td style="font-family:'JetBrains Mono',monospace; font-size:0.72rem; color:var(--nr-text-muted);">
            ${nrEscape(server.name)}
          </td>
          <td>
            <span class="gr-toggle ${toggleClass}" id="gr-toggle-${g.id}"
                  data-nr-click="toggleGroup" data-server-id="${server.id}" data-group-id="${g.id}">
              ${toggleLabel}
            </span>
          </td>
          <td>
            <button aria-label="Delete user" title="Delete user" class="btn btn-outline-danger btn-sm" style="font-size: 0.75rem; padding:0.15rem 0.5rem;"
                    data-nr-click="deleteGroup" data-server-id="${server.id}" data-group-id="${g.id}">
              <i class="bi bi-trash3"></i>
            </button>
          </td>
        </tr>`;
      });
    });

    html += '</tbody></table>';
    container.innerHTML = html;
  }

  // A rule's role: the members' roles follow at their next sign-in
  async function setGroupRole(el) {
    const role = el.value;
    if (role === 'admin' && !await nrConfirm({
          title: `Make ${el.dataset.label} administrators?`,
          message: 'Members of this group become administrators at their next sign-in: they get Administration, '
                 + "every user's rollouts and results, and System Settings and Server Management.",
          confirmLabel: 'Make administrators', danger: true})) {
      el.value = el.dataset.role;
      return;
    }
    const body = new URLSearchParams({ role });
    fetch(`/admin/server/ldap/${el.dataset.server}/groups/${el.dataset.group}/role`, {
      method: 'POST',
      headers: { 'X-CSRFToken': CSRF_TOKEN },
      body
    })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'ok') {
        el.dataset.role = data.role;
        el.classList.toggle('gr-role-admin', data.role === 'admin');
        el.classList.toggle('gr-role-operator', data.role !== 'admin');
      } else {
        el.value = el.dataset.role;
        nrAlert(data.message || 'The role was not changed.', 'Role not changed');
      }
    })
    .catch(() => { el.value = el.dataset.role; nrAlert('The role was not changed.', 'Role not changed'); });
  }

  function toggleGroup(serverId, groupId, el) {
    fetch(`/admin/server/ldap/${serverId}/groups/${groupId}/toggle`, {
      method: 'POST',
      headers: { 'X-CSRFToken': CSRF_TOKEN }
    })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'ok') {
        el.textContent = data.is_active ? 'ACTIVE' : 'INACTIVE';
        el.className = `gr-toggle ${data.is_active ? 'active' : 'inactive'}`;
      }
    });
  }

  async function deleteGroup(serverId, groupId) {
    if (!await nrConfirm({title: 'Remove this group rule?', danger: true, confirmLabel: 'Remove',
                          message: "Its members' roles no longer come from it at their next sign-in."})) return;
    fetch(`/admin/server/ldap/${serverId}/groups/${groupId}/delete`, {
      method: 'POST',
      headers: { 'X-CSRFToken': CSRF_TOKEN }
    })
    .then(r => r.json())
    .then(data => {
      if (data.status === 'ok') {
        const row = document.getElementById(`gr-row-${groupId}`);
        if (row) row.remove();
      }
    });
  }

// What the group rules' elements name in data-nr-<event> (js/events.js)
nrHandlers({
    setGroupRole: el => { setGroupRole(el); },
    toggleGroup: el => { toggleGroup(el.dataset.serverId, el.dataset.groupId, el); },
    deleteGroup: el => { deleteGroup(el.dataset.serverId, el.dataset.groupId); },
});
