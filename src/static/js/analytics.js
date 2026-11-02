// Analytics - see templates/analytics.html.
const SCOPE_USER   = nrData('nr-scope-user');
let lastResults = [];
let lastColumns = [];

const FIELD_ICONS = {
  started_at:    'bi-calendar3',
  device_type:   'bi-cpu',
  status:        'bi-check-circle',
  commands_sent: 'bi-terminal',
  device_ip:     'bi-hdd-network',
  device_port:   'bi-plug'
};

function injectFieldIcon(rule) {
  if (!rule || !rule.$el) return;
  rule.$el.find('.nr-field-icon').remove();
  const id = rule.filter ? rule.filter.id : null;
  const icon = FIELD_ICONS[id] || 'bi-funnel';
  const color = id ? '#00bcd4' : '#444';
  rule.$el.find('.rule-filter-container').before(
    `<i class="bi ${icon} nr-field-icon" style="color:${color};"></i>`
  );
}

const PLATFORMS = [
  'cisco_ios','cisco_nxos','cisco_xe','cisco_xr',
  'juniper_junos','arista_eos','fortinet','paloalto_panos',
  'aruba_aoscx','checkpoint_gaia','hp_procurve','hp_comware'
];


$(function() {
  $('#queryBuilder').queryBuilder({
    allow_empty: true,
    lang: { add_rule: '+ Rule', add_group: '+ Group', delete_rule: '×', delete_group: '×' },
    filters: [
      {
        id: 'started_at', label: 'Started At', type: 'date',
        input: (rule, name) => `<input type="date" name="${name}" class="form-control form-control-sm">`,
        valueGetter: rule => rule.$el.find('input[type=date]').val(),
        valueSetter: (rule, val) => rule.$el.find('input[type=date]').val(val),
        operators: ['equal','less_or_equal','greater_or_equal']
      },
      {
        id: 'device_type', label: 'Platform', type: 'string',
        input: 'select',
        values: Object.fromEntries(PLATFORMS.map(p => [p, p])),
        operators: ['equal','not_equal']
      },
      {
        id: 'status', label: 'Status', type: 'string',
        input: 'select',
        values: { success:'Success', review:'Review', partial:'Partial', failed:'Failed',
                  'not verified':'Not verified', cancelled:'Cancelled' },
        operators: ['equal','not_equal']
      },
      {
        id: 'commands_sent', label: 'Commands Sent', type: 'integer',
        operators: ['equal','not_equal','greater_or_equal','less_or_equal']
      },
      {
        id: 'device_ip', label: 'Device IP', type: 'string',
        operators: ['equal','contains','begins_with']
      },
      {
        id: 'device_port', label: 'Device Port', type: 'integer',
        operators: ['equal','not_equal']
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

  $('#queryBuilder').on(
    'afterAddRule.queryBuilder afterDeleteRule.queryBuilder ' +
    'afterAddGroup.queryBuilder afterDeleteGroup.queryBuilder',
    () => scheduleRefresh()
  );

  scheduleRefresh();
});

function applyScope(val) {
  const url = new URL(window.location.href);
  url.searchParams.set('user', val);
  window.location.href = url.toString();
}

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
    const resp = await fetch('/analytics/query', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-CSRFToken': CSRF_TOKEN },
      body: JSON.stringify({ rules, user: SCOPE_USER })
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
      if (c === 'status') {
        const colors = { success:'#a5d6a7', review:'#ffcc80', failed:'#ef9a9a', partial:'#ffe082',
                         'not verified':'#b0bec5', cancelled:'#666' };
        display = `<span style="font-family:monospace; color:${colors[val]||'#aaa'};">${nrEscape(val)}</span>`;
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
  a.href = url; a.download = `netrollout_results_${Date.now()}.csv`;
  a.click(); URL.revokeObjectURL(url);
}

// What this page's elements name in data-nr-<event> (js/events.js)
nrHandlers({
    runQuery: () => { runQuery(); },
    exportCsv: () => { exportCsv(); },
    applyScope: el => { applyScope(el.value); },
});
