// The drain banner's reload - see templates/_drain_banner.html, which loads it.
(() => {
    let first = null;
    const check = () => fetch('/_netrollout/instance', {cache: 'no-store',
                               headers: {'X-NR-Background': '1'}})
        .then(r => r.ok ? r.json() : null)
        .then(d => {
            if (!d) return;
            if (first === null) first = d.instance;
            else if (d.instance !== first) window.location.reload();
        })
        .catch(() => {});   // down between the two instances - keep polling
    check();
    setInterval(check, 3000);
})();
