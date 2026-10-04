// Establish shell geometry before parsing visible content; no data or API calls.
(() => {
  document.getElementById('processor-entry')?.addEventListener('error', () => {
    const status = document.getElementById('startup-status');
    if (status && !status.hidden) {
      status.querySelector('span').textContent = '界面组件未能加载，请重新加载。';
      status.setAttribute('role', 'alert');
    }
  });
  try {
    const state = JSON.parse(localStorage.getItem('sourceloom-file-tree/1') || '{}');
    const width = Number(state?.width);
    document.documentElement.style.setProperty('--material-tree-width',
      (Number.isFinite(width) && width > 0 ? Math.max(200, Math.min(460, width)) : 250) + 'px');
    if (state?.collapsed === true && matchMedia('(min-width:701px)').matches)
      document.body.classList.add('sidebar-collapsed');
  } catch { /* Unavailable or invalid preferences keep the same CSS defaults. */ }
})();
