// nrAccessStatus / nrNginxLine - see templates/_access_status.html, which loads it.
(() => {
  const when = iso => { const d = new Date(iso); return isNaN(d) ? '' : d.toLocaleString(); };
  const day = d => isNaN(d) ? '' : d.toLocaleDateString();

  window.nrNginxLine = n => {
    if (!n) return '<span class="dim">no NetRollout nginx reports here - with your own reverse proxy, check it there</span>';
    const at = n.time ? ` <span class="dim">· ${nrEscape(when(n.time))}</span>` : '';
    if (n.state === 'applied') return `<span class="ok">✓ applied</span> <span class="dim">${nrEscape(n.message)}</span>${at}`;
    if (n.state === 'rejected')
      return `<span class="fail">✗ rejected: ${nrEscape(n.message || 'no message')}</span>${at}<br>` +
             '<span class="k"></span><span class="dim">nginx keeps serving the last good site</span>';
    return `<span class="warn">? ${nrEscape(n.state)}</span> <span class="dim">- check <code>netrollout status</code> on the server</span>`;
  };

  window.nrAccessStatus = (el, a) => {
    const rows = [`<span class="k">nginx</span>${nrNginxLine(a.nginx)}`];
    const c = a.certificate;
    if (!c) {
      rows.push('<span class="k">certificate</span><span class="fail">✗ none in the certs folder</span>');
    } else {
      const end = c.not_after ? new Date(c.not_after) : null;
      const left = end ? Math.floor((end - Date.now()) / 86400000) : null;
      rows.push(`<span class="k">certificate</span>${c.selfsigned ? 'self-signed' : "your organisation's"}` +
        (end ? ` <span class="dim">· valid until ${nrEscape(day(end))} (${left} days)</span>` : ''));
      rows.push(`<span class="k">covers</span>${nrEscape(c.names.join(', ') || '-')}`);
      for (const o of c.old_names)
        rows.push(`<span class="k"></span><span class="dim">${nrEscape(o.name)}: previous hostname, ` +
                  `leaves on ${nrEscape(day(new Date(o.until * 1000)))}</span>`);
      for (const p of c.problems) rows.push(`<span class="k"></span><span class="fail">✗ ${nrEscape(p)}</span>`);
      for (const w of c.warnings) rows.push(`<span class="k"></span><span class="warn">⚠ ${nrEscape(w)}</span>`);
    }
    el.innerHTML = rows.join('<br>');
  };
})();
