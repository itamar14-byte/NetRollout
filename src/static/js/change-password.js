// Change password - see templates/change_password.html (data-username).
(() => {
  const username = document.currentScript.dataset.username;
  const current = document.getElementById('current_password');
  const pw = document.getElementById('new_password');
  const confirm = document.getElementById('confirm_password');
  const submit = document.getElementById('submitBtn');

  // A field is judged once it's been left (or, for the confirmation, once
  // it's as long as the new password); from then on, live. It turns red and
  // shakes once, at the moment a problem appears - never while still blank.
  // What more typing can't fix (a non-ASCII character, the username) shows at
  // once, even before the field is left.
  const touched = new Set();


  function mark(input, errorId, message) {
    const was = input.classList.contains('is-invalid');
    input.classList.toggle('is-invalid', !!message);
    const el = document.getElementById(errorId);
    el.textContent = message || '';
    el.style.display = message ? 'block' : 'none';
    if (message && !was) nrShake(input);
  }

  function check() {
    // forced: no current password to compare (the server checks the stored one)
    const problem = pw.value ? nrPasswordProblem(pw.value, username, current ? current.value : undefined) : null;
    mark(pw, 'newError', touched.has(pw) ? problem : nrPasswordUnfixable(pw.value, username));
    const mismatch = confirm.value && confirm.value !== pw.value ? "The new passwords don't match." : null;
    const judgeConfirm = touched.has(confirm) || confirm.value.length >= pw.value.length;
    mark(confirm, 'confirmError', judgeConfirm ? mismatch : null);
    submit.disabled = !((!current || current.value) && pw.value && confirm.value && !problem && !mismatch);
  }

  [current, pw, confirm].filter(Boolean).forEach(el => el.addEventListener('blur', e => {
    if (el.value && nrLeftField(e)) { touched.add(el); check(); }
  }));

  document.querySelectorAll('.toggle-password').forEach(btn => btn.addEventListener('click', () => {
    const input = document.getElementById(btn.dataset.target);
    const show = input.type === 'password';
    input.type = show ? 'text' : 'password';
    const icon = btn.querySelector('i');
    icon.className = show ? 'bi bi-eye-slash' : 'bi bi-eye';
    btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    btn.title = show ? 'Hide password' : 'Show password';
    input.focus();
  }));

  [current, pw, confirm].filter(Boolean).forEach(el => el.addEventListener('input', check));
})();
