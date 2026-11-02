// The 2FA code boxes - see templates/_otp_boxes.html, which loads it (data-autofocus, data-shake).
  const boxes = document.querySelectorAll(".otp-box");
  const codeInput = document.getElementById("codeInput");
  const submitBtn = document.getElementById("submitBtn");
  const otpBoxes = document.getElementById("otpBoxes");
  if (document.currentScript.dataset.autofocus) boxes[0].focus();
  // --- OTP box navigation ---
  boxes.forEach((box, i) => {
    box.addEventListener("input", () => {
      box.value = box.value.replace(/[^0-9]/g, "");
      if (box.value && i < boxes.length - 1) boxes[i + 1].focus();
      syncCode();
    });
    box.addEventListener("keydown", (e) => {
      if (e.key === "Backspace" && !box.value && i > 0) {
        boxes[i - 1].focus();
        boxes[i - 1].value = "";
        syncCode();
      }
    });
    box.addEventListener("paste", (e) => {
      e.preventDefault();
      const pasted = e.clipboardData.getData("text").replace(/[^0-9]/g, "").slice(0, 6);
      pasted.split("").forEach((char, idx) => {
        if (boxes[idx]) boxes[idx].value = char;
      });
      const next = Math.min(pasted.length, boxes.length - 1);
      boxes[next].focus();
      syncCode();
    });
  });

  function syncCode() {
    const code = Array.from(boxes).map(b => b.value).join("");
    codeInput.value = code;
    submitBtn.disabled = code.length !== 6;
  }

  // --- Flash shake on page load if there was an error ---
  if (document.currentScript.dataset.shake) {
  document.addEventListener("DOMContentLoaded", () => {
    otpBoxes.classList.add("shake");
    otpBoxes.addEventListener("animationend", () => otpBoxes.classList.remove("shake"), { once: true });
  });
  }

  // --- TOTP 30-second countdown timer ---
  const circle = document.getElementById("timerCircle");
  const timerText = document.getElementById("timerText");
  const circumference = 2 * Math.PI * 22;

  function updateTimer() {
    const now = Math.floor(Date.now() / 1000);
    const secondsLeft = 30 - (now % 30);
    const fraction = secondsLeft / 30;

    timerText.textContent = secondsLeft;
    circle.style.strokeDashoffset = circumference * (1 - fraction);

    if (secondsLeft <= 10) {
      circle.style.stroke = "#dc3545"; // red
    } else if (secondsLeft <= 20) {
      circle.style.stroke = "#ffc107"; // amber
    } else {
      circle.style.stroke = "#28a745"; // green
    }
  }

  updateTimer();
  setInterval(updateTimer, 1000);
