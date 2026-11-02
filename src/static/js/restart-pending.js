// System Settings: the sidebar's restart-pending dot (admin.html, after the Restart script).
// System Settings: labels of restart-only settings saved with a new value
function setSettingsRestartPending(labels) {
  const dot = document.getElementById('restartPendingDot');
  const btn = document.getElementById('admRestartBtn');
  dot.style.display = labels.length ? 'inline-block' : 'none';
  btn.title = labels.length ? 'Restart required - ' + labels.join(', ') + ' changed'
                            : 'Restart server';
}
