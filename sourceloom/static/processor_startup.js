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
    const prefs=JSON.parse(localStorage.getItem('sourceloom-workbench/1')||'{}');
    document.documentElement.dataset.theme=prefs.theme==='dark'||prefs.theme!=='light'&&matchMedia('(prefers-color-scheme:dark)').matches?'dark':'light';
    document.documentElement.dataset.density=prefs.density==='compact'?'compact':'comfortable';
  } catch { /* Appearance failure must not prevent independent tree restoration. */ }
  try {
    const saved=JSON.parse(localStorage.getItem('sourceloom-workspace-layout/1')||'null');
    const legacy=saved?{}:JSON.parse(localStorage.getItem('sourceloom-file-tree/1')||'{}');
    const state=saved||{leftWidth:legacy.width,hideLeft:legacy.collapsed};
    const clamp=(n,fallback)=>Number.isFinite(Number(n))&&Number(n)>0?Math.max(216,Math.min(400,Number(n))):fallback;
    document.documentElement.style.setProperty('--workspace-left-width',clamp(state.leftWidth,256)+'px');
    document.documentElement.style.setProperty('--workspace-right-width',clamp(state.rightWidth,272)+'px');
    document.documentElement.dataset.leftHidden=String(state.hideLeft===true);
    document.documentElement.dataset.rightHidden=String(state.hideRight===true);
    document.documentElement.dataset.leftView=state.leftView==='inspector'?'inspector':'explorer';
  } catch { /* Unavailable layout preferences retain the template defaults. */ }
})();
