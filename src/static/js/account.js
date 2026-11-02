// My Account - see templates/account.html (data-created-at).
  const createdAt = new Date(document.currentScript.dataset.createdAt);

  function updateCounter() {
    const now = new Date();
    let diff = Math.floor((now - createdAt) / 1000);

    const seconds = diff % 60; diff = Math.floor(diff / 60);
    const minutes = diff % 60; diff = Math.floor(diff / 60);
    const hours   = diff % 24; diff = Math.floor(diff / 24);
    const days    = diff % 30; diff = Math.floor(diff / 30);
    const months  = diff % 12;
    const years   = Math.floor(diff / 12);

    const parts = [];
    if (years)   parts.push(`${years}y`);
    if (months)  parts.push(`${months}mo`);
    if (days)    parts.push(`${days}d`);
    if (hours)   parts.push(`${hours}h`);
    if (minutes) parts.push(`${minutes}m`);
    parts.push(`${seconds}s`);

    document.getElementById("ageCounter").textContent = parts.join(" ");
  }

  updateCounter();
  setInterval(updateCounter, 1000);
