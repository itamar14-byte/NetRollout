// admin.html's own script: the sidebar.
    const admSidebar = document.getElementById("adminSidebar");
    const admToggle  = document.getElementById("admToggle");

    // on a phone it slides in over the page, opened from the topbar's ☰, and
    // isn't remembered (it would cover the next page)
    const admPhone = window.matchMedia("(max-width: 767.98px)");
    function admRemember(open) {
        if (admPhone.matches) return;
        try { localStorage.setItem("admSidebarExpanded", open ? "true" : "false"); } catch (e) { /* none */ }
    }
    function admExpand()   { admSidebar.classList.add("expanded"); admRemember(true); }
    function admCollapse() { admSidebar.classList.remove("expanded"); admRemember(false); }

    try {
        if (!admPhone.matches && localStorage.getItem("admSidebarExpanded") === "true") admExpand();
    } catch (e) { /* none */ }

    // the ☰ buttons say whether the sidebar is open, however it was opened
    nrSyncExpanded(admSidebar, [admToggle, document.getElementById("phoneMenu")]);

    document.getElementById("phoneMenu").addEventListener("click", function(e) {
        e.stopPropagation();
        admExpand();
    });

    admToggle.addEventListener("click", function(e) {
        e.stopPropagation();
        admSidebar.classList.contains("expanded") ? admCollapse() : admExpand();
    });

    admSidebar.addEventListener("click", function(e) {
        if (!admSidebar.classList.contains("expanded") && !e.target.closest("a")) admExpand();
    });

    document.addEventListener("click", function(e) {
        if (admSidebar.classList.contains("expanded") && !admSidebar.contains(e.target)) admCollapse();
    });
