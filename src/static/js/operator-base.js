// operator_base.html's own scripts: the sidebar, and invalid fields shake on submit.
    // ── Sidebar toggle ──
    const sidebar  = document.getElementById("opSidebar");
    const toggle   = document.getElementById("sbToggle");

    // on a phone it slides in over the page, opened from the topbar's ☰, and
    // isn't remembered (it would cover the next page)
    const phone = window.matchMedia("(max-width: 767.98px)");

    function expandSidebar()  { sidebar.classList.add("expanded"); }
    function collapseSidebar(){ sidebar.classList.remove("expanded"); }
    function toggleSidebar()  { sidebar.classList.toggle("expanded"); saveSidebarState(); }

    function saveSidebarState() {
        if (phone.matches) return;
        try { localStorage.setItem("opSidebarExpanded", sidebar.classList.contains("expanded")); } catch (e) { /* none */ }
    }

    // Restore state on load
    try {
        if (!phone.matches && localStorage.getItem("opSidebarExpanded") === "true") expandSidebar();
    } catch (e) { /* none */ }

    // the ☰ buttons say whether the sidebar is open, however it was opened
    nrSyncExpanded(sidebar, [toggle, document.getElementById("phoneMenu")]);

    document.getElementById("phoneMenu").addEventListener("click", function(e) {
        e.stopPropagation();
        expandSidebar();
    });

    toggle.addEventListener("click", function(e) {
        e.stopPropagation();
        toggleSidebar();
    });

    // Click on collapsed sidebar body to expand
    sidebar.addEventListener("click", function(e) {
        if (!sidebar.classList.contains("expanded") && !e.target.closest(".sb-toggle")) {
            expandSidebar();
            saveSidebarState();
        }
    });

    // Click outside to collapse
    document.addEventListener("click", function(e) {
        if (sidebar.classList.contains("expanded") && !sidebar.contains(e.target)) {
            collapseSidebar();
            saveSidebarState();
        }
    });

// Suppress browser validation tooltips - shake invalid fields on submit instead
document.addEventListener('DOMContentLoaded', () => {
    // Mark selects as touched on first interaction so CSS error styling activates
    document.querySelectorAll('select.form-select[required]').forEach(sel => {
        sel.addEventListener('change', () => sel.classList.add('nr-touched'));
    });

    document.querySelectorAll('form[novalidate]').forEach(form => {
        form.addEventListener('submit', e => {
            if (!form.checkValidity()) {
                e.preventDefault();
                form.querySelectorAll(':invalid').forEach(field => {
                    // Stamp touched/submitted so CSS error state activates
                    field.classList.add('nr-touched', 'nr-submitted');
                    field.style.animation = 'none';
                    void field.offsetHeight; // force reflow to restart animation
                    field.style.animation = 'shake 0.4s ease';
                    field.addEventListener('animationend',
                        () => { field.style.animation = ''; }, { once: true });
                });
            }
        });
    });
});
