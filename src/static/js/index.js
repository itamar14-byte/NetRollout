// The sign-in page - see templates/index.html.
  // A stopped name's message (users.lock_message: "... Try again in mm:ss.")
  // counts down to the second the stop ends, then says so
  document.querySelectorAll(".alert").forEach(alert => {
    const text = [...alert.childNodes].find(n => n.nodeType === Node.TEXT_NODE && /Try again in \d+:\d\d\./.test(n.textContent));
    if (!text) return;
    const [, before, mm, ss] = text.textContent.match(/^([\s\S]*Try again in )(\d+):(\d\d)\./);
    const end = Date.now() + (Number(mm) * 60 + Number(ss)) * 1000;
    const lead = document.createTextNode(before.trimStart());
    const clock = document.createElement("span");
    const dot = document.createTextNode(".");
    clock.style.fontVariantNumeric = "tabular-nums";
    text.replaceWith(lead, clock, dot);
    const tick = () => {
      const left = Math.max(0, Math.round((end - Date.now()) / 1000));
      clock.textContent = `${String(Math.floor(left / 60)).padStart(2, "0")}:${String(left % 60).padStart(2, "0")}`;
      if (!left) {
        clearInterval(timer);
        alert.classList.replace("alert-danger", "alert-success");
        lead.textContent = "You can try again now.";
        clock.remove();
        dot.remove();
      }
    };
    const timer = setInterval(tick, 1000);
    tick();
  });

  document.querySelectorAll(".toggle-password").forEach(btn => {
    btn.addEventListener("click", function () {
      // by id, not "the element before the button": a browser extension may
      // put its own element there, and the click then flipped that one
      const input = document.getElementById(this.dataset.target);
      const show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      const icon = this.querySelector('i');
      icon.className = show ? 'bi bi-eye-slash' : 'bi bi-eye';
      this.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
      this.title = show ? 'Hide password' : 'Show password';
    });
  });

