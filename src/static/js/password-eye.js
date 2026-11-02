// Show / hide on every password field - see templates/_password_eye.html, which loads it.
(() => {
    function show(input, button, on) {
        input.type = on ? 'text' : 'password';
        const label = on ? 'Hide password' : 'Show password';
        button.title = label;
        button.setAttribute('aria-label', label);
        button.querySelector('i').className = on ? 'bi bi-eye-slash' : 'bi bi-eye';
    }
    function addEye(input) {
        if (input.dataset.eye || 'noEye' in input.dataset) return;
        input.dataset.eye = '1';
        const group = input.parentElement;
        if (group.querySelector(':scope > .pw-toggle, :scope > .toggle-password')) return;
        let wrap = group;
        if (!group.classList.contains('pw-group')) {
            wrap = document.createElement('div');
            wrap.className = 'pw-group';
            input.replaceWith(wrap);
            wrap.appendChild(input);
        }
        input.style.paddingRight = '2.4rem';
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'pw-toggle';
        button.innerHTML = '<i aria-hidden="true"></i>';
        show(input, button, false);
        button.addEventListener('click', () => show(input, button, input.type === 'password'));
        wrap.appendChild(button);
    }
    const scan = root => root.querySelectorAll('input[type=password]').forEach(addEye);
    scan(document);
    new MutationObserver(changes => changes.forEach(change => change.addedNodes.forEach(node => {
        if (node.nodeType !== 1) return;
        if (node.matches('input[type=password]')) addEye(node);
        scan(node);
    }))).observe(document.body, {childList: true, subtree: true});
})();
