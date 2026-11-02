// The CSRF token every request the page's scripts send carries (X-CSRFToken),
// from the page's <meta name="csrf-token"> (admin.html, _op_widgets_js.html).
const CSRF_TOKEN = document.querySelector('meta[name="csrf-token"]').content;
