// base.html's own script: invalid fields shake on submit.
// Suppress browser validation tooltips - shake invalid fields on submit instead
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('form[novalidate]').forEach(form => {
        form.addEventListener('submit', e => {
            if (!form.checkValidity()) {
                e.preventDefault();
                form.querySelectorAll(':invalid').forEach(field => {
                    field.classList.add('nr-submitted');
                    field.style.animation = 'none';
                    void field.offsetHeight;
                    field.style.animation = 'shake 0.4s ease';
                    field.addEventListener('animationend',
                        () => { field.style.animation = ''; }, { once: true });
                });
            }
        });
    });
});
