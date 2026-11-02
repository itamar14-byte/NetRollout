// Event handlers without inline on...= attributes, which the pages'
// Content-Security-Policy forbids (script-src 'self'). An element names its
// handler in a data attribute - data-nr-click="saveServer",
// data-nr-change="...", data-nr-mouseenter="nrHover" - and the page's script
// registers it with nrHandlers({saveServer(el, event) {...}}). The handler is
// called as the inline one was: `this` and its first argument are the
// element, the second the event; returning false cancels the default.
//
// Bound to the element itself, like an inline handler, so bubbling and
// stopPropagation() behave as before: a MutationObserver started here, in
// <head>, sees every element as the page is parsed and as scripts insert
// HTML. A script that inserts HTML and clicks it in the same breath calls
// nrBind(root) first. 'error' and 'load' don't bubble and can fire before
// an element is bound (a broken image): a capturing listener on document.
// Loaded first in the <head> of operator_base.html and admin.html.
(() => {
    const BOUND_EVENTS = ['click', 'dblclick', 'change', 'input', 'submit', 'keydown', 'keyup',
                          'mouseenter', 'mouseleave', 'focus', 'blur'];
    const CAPTURED_EVENTS = ['error', 'load'];
    const SELECTOR = BOUND_EVENTS.map(e => `[data-nr-${e}]`).join(',');
    const handlers = Object.create(null);
    const bound = new WeakMap();        // element → the events bound on it

    // nrHandlers({name: fn, ...}): handlers the page's elements may name
    window.nrHandlers = map => Object.assign(handlers, map);

    function run(el, event) {
        const name = el.getAttribute('data-nr-' + event.type);
        if (name === null) return;          // taken away since it was bound
        const fn = handlers[name];
        if (!fn) { console.error(`NetRollout: no handler "${name}" for ${event.type}`); return; }
        if (fn.call(el, el, event) === false) event.preventDefault();
    }

    function bind(el) {
        let done = bound.get(el);
        for (const type of BOUND_EVENTS) {
            if (!el.hasAttribute('data-nr-' + type)) continue;
            if (!done) bound.set(el, done = new Set());
            if (done.has(type)) continue;
            done.add(type);
            el.addEventListener(type, e => run(el, e));
        }
    }

    // nrBind(root): bind root and what's inside it now (the observer would, a
    // moment later)
    window.nrBind = root => {
        if (!root || root.nodeType !== 1) return;
        if (root.matches(SELECTOR)) bind(root);
        root.querySelectorAll(SELECTOR).forEach(bind);
    };

    new MutationObserver(records => {
        for (const r of records) {
            if (r.type === 'attributes') { if (r.target.matches(SELECTOR)) bind(r.target); }
            else r.addedNodes.forEach(window.nrBind);
        }
    }).observe(document.documentElement, {childList: true, subtree: true, attributes: true,
                                          attributeFilter: BOUND_EVENTS.map(e => 'data-nr-' + e)});
    window.nrBind(document.documentElement);

    for (const type of CAPTURED_EVENTS) {
        document.addEventListener(type, e => {
            const el = e.target;
            if (el instanceof Element && el.hasAttribute('data-nr-' + type)) run(el, e);
        }, true);
    }

    // "color: #00bcd4; border-color: #00bcd4" set on the element's own style
    const setStyles = (el, text) => text.split(';').forEach(decl => {
        const at = decl.indexOf(':');
        if (at > 0) el.style.setProperty(decl.slice(0, at).trim(), decl.slice(at + 1).trim());
    });

    // What many elements did inline, shared
    nrHandlers({
        // a hover / focus look: data-nr-hover-style on, data-nr-rest-style back
        nrHover: el => { setStyles(el, el.dataset.nrHoverStyle); },
        nrRest: el => { setStyles(el, el.dataset.nrRestStyle); },
        // an image that doesn't load hides (and shows the fallback after it,
        // data-nr-fallback: its display)
        nrHide: el => {
            el.style.display = 'none';
            if (el.dataset.nrFallback) el.nextElementSibling.style.display = el.dataset.nrFallback;
        },
        nrStop: (el, e) => { e.stopPropagation(); },
        nrSubmitForm: el => { el.form.submit(); },
    });
})();
