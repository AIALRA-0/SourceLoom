const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const button = (action, label, primary = false) => `<button data-action="${action}"${primary ? ' class="primary"' : ''}>${label}</button>`;
const menu = (label, content) => `<details class="toolbar-menu"><summary>${label}<span aria-hidden="true">⌄</span></summary><div class="toolbar-popover">${content}</div></details>`;
export function documentToolbar(project, run, hasOutput, active, status) {
  if (project.trashed) return button('restore', '恢复材料');
  const id = encodeURIComponent(project.id);
  const actions = [];
  if (active) actions.push(button('cancel', '取消处理'));
  else if (!hasOutput && project.inventory && run?.status === 'not_started') actions.push(button('produce', '开始改写', true));
  else if (!hasOutput && project.inventory && run?.status === 'failed' && (['planner','plan_review','writer'].includes(run?.stage) || run?.pipeline === 'active_composition_v1')) actions.push(button('rewrite', '重新改写', true));
  if (hasOutput) {
    actions.push(button('edit', '编辑正文', true));
    if (project.draft) actions.push(button('readweave', '导入阅读器'));
    actions.push(menu('下载', `<a href="/api/projects/${id}/output?format=markdown">下载正文</a>${project.draft ? `<a href="/api/projects/${id}/export">下载阅读包</a>` : ''}${button('download-original', '下载当前原件')}`));
  }
  if (status === 'uncertain') {
    actions.push(button('recover', '查询原请求'));
    if (run.stage === 'fidelity') actions.push(button('recheck-fidelity', '重新独立核对'));
  }
  if (status === 'failed' && ['style','fidelity'].includes(run?.stage)) actions.push(button('resume', '继续核对'));
  if (run?.candidate_url && !active) actions.push(`<a data-preview href="${escape(run.candidate_url)}" target="_blank" rel="noopener">本次草稿 ↗</a>`);
  const management = [button('manage', '重命名 / 移动'), button('duplicate', '创建副本')];
  if (hasOutput) {
    management.push(button('history', '历史版本'));
    if (!active && status !== 'uncertain') management.push(button('rewrite', '重新改写'));
  }
  management.push(button('delete', '移到回收站'));
  actions.push(menu('更多', management.join('')));
  return actions.join('');
}

document.addEventListener('click', event => {
  const chosen = event.target.closest('.toolbar-menu');
  for (const menu of document.querySelectorAll('.toolbar-menu[open]')) {
    if (menu !== chosen || event.target.closest('[data-action],a')) menu.open = false;
  }
});
document.addEventListener('keydown', event => {
  if (event.key !== 'Escape') return;
  for (const menu of document.querySelectorAll('.toolbar-menu[open]')) {
    menu.open = false;
    menu.querySelector('summary').focus();
  }
});
