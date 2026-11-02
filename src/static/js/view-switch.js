// The Operations | Administration switch - see templates/_view_switch.html, which loads it (data-view: this page's view).
(() => {
    // this page is the last of its view; the other side goes back to its last
    // (a path on this site, of that view: never another host)
    const here = document.currentScript.dataset.view;
    const ofView = (view, path) => typeof path === 'string' && path.startsWith('/') && !path.startsWith('//')
        && (view === 'admin') === (path === '/admin' || path.startsWith('/admin/'));
    try {
        const url = new URL(window.location.href);
        url.searchParams.delete('_bg');
        localStorage.setItem('nr-view-last-' + here, url.pathname + url.search);
        document.querySelectorAll('.nr-view-switch a[data-view]').forEach(a => {
            const last = localStorage.getItem('nr-view-last-' + a.dataset.view);
            if (a.dataset.view !== here && ofView(a.dataset.view, last)) a.href = last;
        });
    } catch (e) { /* no storage: the views' first pages */ }
})();
