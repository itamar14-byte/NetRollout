// Screen-reader helpers - see templates/_a11y_js.html, which loads it.
// nrSyncExpanded(panel, buttons): each button's aria-expanded follows whether
// the panel has the class "expanded", whatever opened or closed it.
function nrSyncExpanded(panel, buttons) {
    const sync = () => {
        const open = panel.classList.contains("expanded");
        buttons.forEach(b => b && b.setAttribute("aria-expanded", open ? "true" : "false"));
    };
    new MutationObserver(sync).observe(panel, {attributes: true, attributeFilter: ["class"]});
    sync();
}

// Enter or Space on an element marked data-nr-activate clicks it, as on a
// <button>: for a role="button" that can't be one (it holds block content).
document.addEventListener("keydown", e => {
    if ((e.key === "Enter" || e.key === " ") && e.target instanceof Element
            && e.target.matches("[data-nr-activate]")) {
        e.preventDefault();
        e.target.click();
    }
});

// nrNameQueryBuilder(root): names the selects and inputs QueryBuilder builds
// without a label (a rule's field, condition, value) by their part of the rule,
// so a screen reader doesn't read a bare "combo box" - now and whenever the
// library adds or rebuilds them.
function nrNameQueryBuilder(root) {
    const parts = [["_filter", "Field"], ["_operator", "Condition"], ["_value_", "Value"]];
    const name = () => root.querySelectorAll("select:not([aria-label]), input:not([aria-label]):not([type=hidden])")
        .forEach(el => {
            const hit = parts.find(([part]) => (el.name || "").includes(part));
            if (hit && !(el.labels && el.labels.length)) el.setAttribute("aria-label", hit[1]);
        });
    new MutationObserver(name).observe(root, {childList: true, subtree: true});
    name();
}
