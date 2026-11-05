// Inventory's banner for admins (templates/inventory.html): a discovery
// running or paused, or its findings waiting, with its counts and a link to
// Discover devices. Asked as a background request, every few seconds while
// it runs; nothing shown when there is none.
const DISC_BANNER = document.currentScript.dataset;

(() => {
    const banner = document.getElementById('discBanner');
    if (!banner) return;
    const NF = new Intl.NumberFormat('en');
    const PHASES = {running: 'A discovery is running', paused: 'A discovery is paused after failed sign-ins',
                    cancelling: 'A discovery is being cancelled', done: 'A discovery finished',
                    cancelled: 'A discovery was cancelled'};

    async function ask() {
        let d = null;
        try {
            const r = await fetch('/inventory/discover/status?since=1000000000', {cache: 'no-store',
                headers: {'X-NR-Background': '1'}});
            if (r.ok) d = (await r.json()).discovery;
        } catch (e) { d = null; }
        show(d);
        if (d && !['done', 'cancelled'].includes(d.phase)) setTimeout(ask, 5000);
    }

    function show(d) {
        banner.hidden = !d;
        if (!d) return;
        const c = d.counts;
        const waiting = c.found - (d.created || []).length;
        banner.classList.toggle('is-paused', d.phase === 'paused');
        const text = document.createElement('span');
        text.textContent = `${PHASES[d.phase] || 'A discovery'} (${d.entries.join(', ')}, by ${d.by}): ` +
            `${NF.format(c.checked)} of ${NF.format(c.addresses)} checked, ${NF.format(c.found)} found` +
            (waiting > 0 && ['done', 'cancelled'].includes(d.phase) ? `, ${NF.format(waiting)} not added yet` : '') + '.';
        const link = document.createElement('a');
        link.href = DISC_BANNER.url;
        link.textContent = 'Open';
        banner.replaceChildren(text, link);
    }

    ask();
})();
