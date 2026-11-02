// The maintenance page's reload - see templates/maintenance.html.
(() => {
    const check = () => fetch('/_netrollout/health', {cache: 'no-store',
                               headers: {'X-NR-Background': '1'}})
        .then(r => r.json())
        .then(d => {
            if (!d.maintenance || d.maintenance.state !== 'locked') window.location.reload();
            else if (d.maintenance.progress)
                document.getElementById('progress').textContent = d.maintenance.progress;
        })
        .catch(() => {});   // restarting, or not answering for a moment: keep asking
    setInterval(check, 3000);
})();
