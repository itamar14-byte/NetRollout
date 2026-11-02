// nrEscape(v) - see templates/_escape_js.html, which loads it.
function nrEscape(v) {
    if (v === null || v === undefined) return '';
    return String(v).replace(/[&<>"']/g, c =>
        ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
}
