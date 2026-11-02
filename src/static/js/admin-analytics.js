// Admin Analytics - see templates/admin_analytics.html.
let lastResults = [];
let lastColumns = [];

const USERNAMES = nrData('nr-usernames');

const FIELD_ICONS = {
  timestamp:      'bi-clock',
  actor_username: 'bi-person',
  action:         'bi-activity',
  object_type:    'bi-box',
  success:        'bi-shield-check',
  ip_address:     'bi-hdd-network'
};

function injectFieldIcon(rule) {
  if (!rule || !rule.$el) return;
  rule.$el.find('.nr-field-icon').remove();
  const id = rule.filter ? rule.filter.id : null;
  const icon = FIELD_ICONS[id] || 'bi-funnel';
  const color = id ? '#ffe082' : '#444';
  rule.$el.find('.rule-filter-container').before(
    `<i class="bi ${icon} nr-field-icon" style="color:${color};"></i>`
  );
}

$(function() {
  $('#queryBuilder').queryBuilder({
    allow_empty: true,
    lang: { add_rule: '+ Rule', add_group: '+ Group', delete_rule: '×', delete_group: '×' },
    filters: [
      {
        id: 'timestamp', label: 'Timestamp', type: 'date',
        input: (rule, name) => `<input type="date" name="${name}" class="form-control form-control-sm">`,
        valueGetter: rule => rule.$el.find('input[type=date]').val(),
        valueSetter: (rule, val) => rule.$el.find('input[type=date]').val(val),
        operators: ['equal','less_or_equal','greater_or_equal']
      },
      {
        id: 'actor_username', label: 'Actor', type: 'string',
        operators: ['equal','not_equal','contains','begins_with'],
        input: function(rule, name) {
          const opts = USERNAMES.map(u => `<option value="${nrEscape(u)}">${nrEscape(u)}</option>`).join('');
          return `
            <select class="actor-select" name="${name}" style="display:none;">
              <option value="">- pick user -</option>${opts}
            </select>
            <input type="text" class="actor-text" name="${name}"
                   placeholder="username substring…" style="display:none;">`;
        },
        valueGetter: function(rule) {
          const op = rule.operator.type;
          return (op === 'equal' || op === 'not_equal')
            ? rule.$el.find('.actor-select').val()
            : rule.$el.find('.actor-text').val();
        },
        valueSetter: function(rule, val) {
          const op = rule.operator.type;
          if (op === 'equal' || op === 'not_equal') rule.$el.find('.actor-select').val(val);
          else rule.$el.find('.actor-text').val(val);
        }
      },
      {
        id: 'action', label: 'Action', type: 'string',
        placeholder: 'e.g. inventory.create',
        operators: ['equal','not_equal','contains','begins_with']
      },
      {
        id: 'object_type', label: 'Object Type', type: 'string',
        input: 'select',
        values: {
          'User':'User', 'Inventory':'Inventory', 'SecurityProfile':'SecurityProfile',
          'VariableMapping':'VariableMapping', 'Server':'Server'
        },
        operators: ['equal','not_equal']
      },
      {
        id: 'success', label: 'Result', type: 'boolean',
        input: 'select',
        values: { true: 'Success', false: 'Failed' },
        operators: ['equal']
      },
      {
        id: 'ip_address', label: 'IP Address', type: 'string',
        operators: ['equal','contains','begins_with']
      }
    ],
    operators: [
      { type:'equal',            nb_inputs:1, multiple:false, apply_to:['string','number','datetime','boolean'], label:'=' },
      { type:'not_equal',        nb_inputs:1, multiple:false, apply_to:['string','number','datetime','boolean'], label:'≠' },
      { type:'greater_or_equal', nb_inputs:1, multiple:false, apply_to:['number','datetime'], label:'≥' },
      { type:'less_or_equal',    nb_inputs:1, multiple:false, apply_to:['number','datetime'], label:'≤' },
      { type:'contains',         nb_inputs:1, multiple:false, apply_to:['string'], label:'contains' },
      { type:'begins_with',      nb_inputs:1, multiple:false, apply_to:['string'], label:'starts with' },
    ]
  });

  nrNameQueryBuilder(document.getElementById('queryBuilder'));

  $('#queryBuilder')
    .on('afterAddRule.queryBuilder', (e, rule) => injectFieldIcon(rule))
    .on('afterUpdateRuleFilter.queryBuilder', (e, rule) => injectFieldIcon(rule));

  function syncActorInput(rule) {
    if (rule.filter && rule.filter.id !== 'actor_username') return;
    const op = rule.operator ? rule.operator.type : null;
    const useSelect = (op === 'equal' || op === 'not_equal');
    rule.$el.find('.actor-select').toggle(useSelect);
    rule.$el.find('.actor-text').toggle(!useSelect);
  }

  let _qbRefreshTimer;
  function scheduleRefresh() {
    clearTimeout(_qbRefreshTimer);
    _qbRefreshTimer = setTimeout(() => {
      const $qb = $('#queryBuilder');
      $qb.find('.nr-qb-connector').remove();
      $qb.find('.rules-group-container').each(function() {
        const $group = $(this);
        const condition = $group.find('> .rules-group-header .group-conditions input:checked').val() || 'AND';
        const $list = $group.find('.rules-list').first();
        const $children = $list.children('.rule-container, .rules-group-container');
        $children.each(function(i) {
          if (i === $children.length - 1) return;
          $(this).after(
            `<div class="nr-qb-connector"><button class="nr-qb-pill" type="button">${condition}</button></div>`
          );
        });
      });
    }, 30);
  }

  $(document).on('click', '.nr-qb-pill', function() {
    const $group = $(this).closest('.rules-group-container');
    const $conditions = $group.find('> .rules-group-header .group-conditions');
    const $checked = $conditions.find('input:checked');
    const $other = $conditions.find('input:not(:checked)');
    $checked.prop('checked', false);
    $other.prop('checked', true).trigger('change');
    scheduleRefresh();
  });

  $('#queryBuilder')
    .on('afterAddRule.queryBuilder afterDeleteRule.queryBuilder ' +
        'afterAddGroup.queryBuilder afterDeleteGroup.queryBuilder',
        () => scheduleRefresh())
    .on('afterAddRule.queryBuilder', (e, rule) => syncActorInput(rule))
    .on('afterUpdateRuleOperator.queryBuilder', (e, rule) => syncActorInput(rule));

  scheduleRefresh();
});

async function runQuery() {
  const rules = $('#queryBuilder').queryBuilder('getRules');
  if (!rules) return;   // invalid (null): the builder already marks the rule

  const errorBox = document.getElementById('queryError');
  errorBox.style.display = 'none';

  document.getElementById('queryLoading').style.display     = '';
  document.getElementById('queryResultsWrap').style.display = 'none';
  document.getElementById('queryEmpty').style.display       = 'none';

  let data;
  try {
    const resp = await fetch('/admin/analytics/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
      body: JSON.stringify({ rules })
    });
    data = await resp.json().catch(() => ({}));
    if (!resp.ok || data.status === 'error') {
      errorBox.textContent = data.message || 'Query failed';
      errorBox.style.display = '';
      return;
    }
  } catch (e) {
    errorBox.textContent = 'Query failed';
    errorBox.style.display = '';
    return;
  } finally {
    document.getElementById('queryLoading').style.display = 'none';
  }

  if (!data.rows || data.rows.length === 0) {
    document.getElementById('queryEmpty').style.display = '';
    return;
  }

  lastResults = data.rows;
  lastColumns = data.columns;

  document.getElementById('queryResultsHead').innerHTML =
    '<tr>' + data.columns.map(c =>
      `<th style="color:var(--nr-text-muted); font-size: 0.6875rem; letter-spacing:1px; font-weight:600;
                  text-transform:uppercase; border-color:#1a1a1a; white-space:nowrap;">${nrEscape(c)}</th>`
    ).join('') + '</tr>';

  document.getElementById('queryResultsBody').innerHTML = data.rows.map(row =>
    '<tr style="border-color:#1a1a1a;">' +
    data.columns.map(c => {
      const val = row[c];
      let display = val === null || val === undefined
        ? '<span style="color:var(--nr-text-muted);">-</span>' : nrEscape(val);
      if (c === 'success') {
        const ok = val === true || val === 'true';
        display = ok
          ? '<span style="color:#a5d6a7; font-family:monospace;">✓ true</span>'
          : '<span style="color:#ef9a9a; font-family:monospace;">✗ false</span>';
      }
      return `<td style="font-size:0.75rem; color:#aaa; white-space:nowrap;">${display}</td>`;
    }).join('') + '</tr>'
  ).join('');

  document.getElementById('queryResultCount').textContent =
    `${data.rows.length} row${data.rows.length !== 1 ? 's' : ''}`;
  document.getElementById('queryResultsWrap').style.display = '';
}

function exportCsv() {
  if (!lastResults.length) return;
  const header = lastColumns.join(',');
  const rows   = lastResults.map(row =>
    lastColumns.map(c => {
      const v = row[c] ?? '';
      return String(v).includes(',') ? `"${String(v).replace(/"/g,'""')}"` : String(v);
    }).join(',')
  );
  const csv  = [header, ...rows].join('\n');
  const blob = new Blob([csv], { type:'text/csv' });
  const url  = URL.createObjectURL(blob);
  const a    = document.createElement('a');
  a.href = url; a.download = `netrollout_audit_${Date.now()}.csv`;
  a.click(); URL.revokeObjectURL(url);
}
