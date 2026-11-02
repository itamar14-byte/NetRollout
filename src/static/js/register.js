// Request access - see templates/register.html.
  const usernameInput = document.getElementById("username");
  const emailInput    = document.getElementById("email");
  const fullNameInput = document.getElementById("full_name");
  const passwordInput = document.getElementById("password");
  const confirmInput  = document.getElementById("confirm_password");
  const submitBtn     = document.getElementById("submitBtn");

  const usernameError = document.getElementById("usernameError");
  const emailError    = document.getElementById("emailError");
  const fullNameError = document.getElementById("fullNameError");
  const passwordError = document.getElementById("passwordError");
  const confirmError  = document.getElementById("confirmError");

  // --- Validators ---

  function validateUsername() {
    const val = usernameInput.value.trim();
    if (!val) {
      showError(usernameInput, usernameError, "Username is required.");
      return false;
    }
    clearError(usernameInput, usernameError);
    return true;
  }

  function validateEmail() {
    const val = emailInput.value.trim();
    const emailRegex = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
    if (!val) {
      showError(emailInput, emailError, "Email is required.");
      return false;
    }
    if (!emailRegex.test(val)) {
      showError(emailInput, emailError, "Please enter a valid email address.");
      return false;
    }
    clearError(emailInput, emailError);
    return true;
  }

  function validateFullName() {
    const val = fullNameInput.value.trim();
    if (!val) {
      showError(fullNameInput, fullNameError, "Full name is required.");
      return false;
    }
    clearError(fullNameInput, fullNameError);
    return true;
  }

  // Passwords are judged once the field is left (the confirmation also once
  // it's as long as the password); what more typing can't fix shows at once.
  // A field turns red and shakes when a problem appears.
  const pwTouched = new Set();

  function passwordProblem() {
    const v = passwordInput.value;
    return v ? nrPasswordProblem(v, usernameInput.value.trim()) : "Password is required.";
  }

  function confirmProblem() {
    if (!confirmInput.value) return "Please confirm your password.";
    return confirmInput.value !== passwordInput.value ? "Passwords do not match." : null;
  }

  function markPassword(input, errorEl, message) {
    const was = input.classList.contains("is-invalid");
    if (message) { showError(input, errorEl, message); if (!was) nrShake(input); }
    else clearError(input, errorEl);
  }

  function validatePassword() {
    // The server enforces the same rule (src/passwords.py)
    const v = passwordInput.value;
    const shown = pwTouched.has(passwordInput) ? (v ? passwordProblem() : null)
                                               : nrPasswordUnfixable(v, usernameInput.value.trim());
    markPassword(passwordInput, passwordError, shown);
    return !passwordProblem();
  }

  function validateConfirm() {
    const judge = pwTouched.has(confirmInput) ||
                  (confirmInput.value && confirmInput.value.length >= passwordInput.value.length);
    markPassword(confirmInput, confirmError, judge && confirmInput.value ? confirmProblem() : null);
    return !confirmProblem();
  }

  [passwordInput, confirmInput].forEach(el => el.addEventListener("blur", e => {
    if (el.value && nrLeftField(e)) {
      pwTouched.add(el);
      validatePassword(); if (confirmInput.value) validateConfirm(); checkAllValid();
    }
  }));

  // --- Helpers ---

  function showError(input, errorEl, message) {
    input.classList.add("is-invalid");
    errorEl.textContent = message;
    errorEl.style.display = "block";
  }

  function clearError(input, errorEl) {
    input.classList.remove("is-invalid");
    errorEl.textContent = "";
    errorEl.style.display = "none";
  }

  function checkAllValid() {
    const allFilled =
      usernameInput.value.trim() &&
      emailInput.value.trim() &&
      fullNameInput.value.trim() &&
      passwordInput.value &&
      confirmInput.value;

    // the passwords by their rule, not by what's shown yet (shown errors wait)
    const noErrors = document.querySelectorAll("#registerForm .is-invalid").length === 0
                     && !passwordProblem() && !confirmProblem();

    submitBtn.disabled = !(allFilled && noErrors);
  }

  // --- Live validation listeners ---

  usernameInput.addEventListener("input", () => { validateUsername(); validatePassword(); checkAllValid(); });
  emailInput.addEventListener("input",    () => { validateEmail();    checkAllValid(); });
  fullNameInput.addEventListener("input", () => { validateFullName(); checkAllValid(); });
  passwordInput.addEventListener("input", () => { validatePassword(); if (confirmInput.value) validateConfirm(); checkAllValid(); });
  confirmInput.addEventListener("input",  () => { validateConfirm();  checkAllValid(); });

  // --- Password toggle ---

  document.querySelectorAll(".toggle-password").forEach(btn => {
    btn.addEventListener("click", function () {
      const input = document.getElementById(this.dataset.target);
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      const icon = this.querySelector('i');
      icon.className = show ? 'bi bi-eye-slash' : 'bi bi-eye';
      this.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      this.title = show ? 'Hide password' : 'Show password';
    });
  });
