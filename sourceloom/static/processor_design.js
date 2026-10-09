// Workbench presentation only. Business state and document bytes stay in their
// existing controllers. Preferences never enter model tasks or exported drafts.
import './processor_icons.js';
import './processor_select.js';
import { TemplateWorkspace } from './processor_workspace.js?v=apcf-ui-20261009-1';
import {readPreferences, applyTheme, appearanceKey as KEY} from './processor_appearance.js';

export function initializeDesign(deps) {
  const $ = s => document.querySelector(s);
  let prefs = readPreferences(), draft = null, trigger = null;
  const media = matchMedia('(prefers-color-scheme: dark)');
  const workspace = new TemplateWorkspace(deps);
  $('#workbench-command').insertAdjacentHTML('afterbegin',globalThis.WIcons('search'));$('.workspace-brand').insertAdjacentHTML('afterbegin',globalThis.WIcons('workspace'));
  const labels = { mine: ['folder', '我的材料'], examples: ['components', '示例库'], archives: ['history', '已归档'], trash: ['trash', '回收站'] };
  for (const button of $('.library-nav').querySelectorAll('button')) {
    const [name, label] = labels[button.dataset.libraryMode];
    button.innerHTML = globalThis.WIcons(name);button.setAttribute('aria-label', label);button.dataset.tooltip = label;
  }
  for(const [id,icon] of [['workbench-sidebar','leftPanel'],['workbench-inspector','rightPanel'],['workbench-settings','settings'],['sidebar-toggle','folder']]) $('#'+id).innerHTML=globalThis.WIcons(icon);
  for(const b of document.querySelectorAll('[data-panel-swap]'))b.innerHTML=globalThis.WIcons('swap');
  for(const b of document.querySelectorAll('[data-panel-hide]'))b.innerHTML=globalThis.WIcons('close');
  const apply = () => {
    applyTheme(document.documentElement, prefs, media.matches);
    document.dispatchEvent(new CustomEvent('workbench-preferences', { detail: prefs }));
    stylePreview();
  };
  const stylePreview = () => {
    const doc = $('#result-preview')?.contentDocument;
    if (!doc?.head) return;
    const dark = document.documentElement.dataset.theme === 'dark';
    let style = doc.getElementById('workbench-view-theme');
    if (!style) { style = doc.createElement('style'); style.id = 'workbench-view-theme'; doc.head.append(style); }
    style.textContent = dark ? ':root,body{background:#1e1e1e;color:#eeeeee}a{color:#eeeeee}h1,h2,h3,h4,blockquote,figcaption{color:inherit}pre,code,th,td,table,.source-ref,details{color:inherit;border-color:#383838}pre,code,th,details,blockquote{background:#262626}.source-focused{background:#303030;outline-color:#dedede}caption,figcaption,.original-view-tools,details>summary{color:#b8b8b8}summary:hover{color:#eeeeee;background:#303030}.table-view-action{color:#eeeeee;background:#262626;border-color:#747474}' : '';
  };
  $('#result-preview').addEventListener('load', stylePreview);
  media.addEventListener('change', () => { if (prefs.theme === 'system') apply(); });
  const importButton = $('#new-material');
  importButton.innerHTML = globalThis.WIcons('plus')+'<span>导入材料</span>';
  const settings = document.createElement('dialog'); settings.id = 'workbench-settings-dialog'; settings.className = 'workbench-settings';
  settings.setAttribute('aria-labelledby','workbench-settings-title');
  settings.innerHTML = `<header class="dialog-heading"><h2 id="workbench-settings-title">工作台设置</h2><button type="button" data-dismiss aria-label="关闭设置">${globalThis.WIcons('close')}</button></header>
    <nav class="settings-tabs" aria-label="设置分类"><button type="button" data-settings-tab="appearance" aria-current="true">外观</button><button type="button" data-settings-tab="layout">布局</button><button type="button" data-settings-tab="workspace">工作区</button></nav>
    <form id="workbench-settings-form"><section data-settings-panel="appearance"><label>主题<select name="theme"><option value="system">跟随系统</option><option value="light">浅色</option><option value="dark">深色</option></select></label><label>控件密度<select name="density"><option value="comfortable">舒适</option><option value="compact">紧凑</option></select></label></section>
    <section data-settings-panel="layout" hidden><label>左侧面板<select name="leftView"><option value="explorer">材料</option><option value="inspector">检查器</option></select></label><label class="check-label"><input name="hideLeft" type="checkbox">收起左面板</label><label class="check-label"><input name="hideRight" type="checkbox">收起右面板</label><p class="muted">拖动分隔条调整宽度；拖动面板标题或点击交换移动面板。窄屏使用同一面板的抽屉，阅读位置保持。</p></section>
    <section data-settings-panel="workspace" hidden><p>工作台显示设置保存在此浏览器。材料、原件和成稿由原有保存服务管理，不随主题切换改变。</p></section>
    <p class="settings-message" role="status"></p><footer><button type="button" data-dismiss>取消</button><button type="submit" class="primary" disabled>应用设置</button></footer></form>`;
  document.body.append(settings);
  const form = settings.querySelector('form'), message = settings.querySelector('.settings-message');
  $('#workbench-settings').onclick = () => {
    trigger = document.activeElement; draft = {...prefs,...workspace.config}; form.elements.theme.value = draft.theme; form.elements.density.value = draft.density;form.elements.leftView.value=draft.leftView;form.elements.hideLeft.checked=draft.hideLeft;form.elements.hideRight.checked=draft.hideRight;
    form.querySelector('[type=submit]').disabled = true; message.textContent = ''; settings.showModal(); globalThis.WBSelect.enhance(settings);
  };
  settings.querySelectorAll('[data-dismiss]').forEach(b => b.onclick = () => settings.close());
  settings.addEventListener('close',()=>{draft=null;globalThis.WBSelect.close();trigger?.focus({preventScroll:true});});
  settings.querySelector('nav').onclick = e => {
    const tab = e.target.closest('[data-settings-tab]')?.dataset.settingsTab; if (!tab) return;
    for (const p of settings.querySelectorAll('[data-settings-panel]')) p.hidden = p.dataset.settingsPanel !== tab;
    for (const b of settings.querySelectorAll('[data-settings-tab]')) b.setAttribute('aria-current',String(b.dataset.settingsTab===tab));
  };
  form.onchange = () => { draft = {...workspace.config,theme:form.elements.theme.value,density:form.elements.density.value,leftView:form.elements.leftView.value,hideLeft:form.elements.hideLeft.checked,hideRight:form.elements.hideRight.checked};form.querySelector('[type=submit]').disabled=false; };
  form.onsubmit = e => {
    e.preventDefault(); if (!draft) return;
    try { localStorage.setItem(KEY, JSON.stringify(draft)); prefs = {theme:draft.theme,density:draft.density};workspace.configure(draft);apply(); settings.close(); }
    catch { message.textContent = '浏览器未能保存设置。当前阅读保留，请检查存储权限后重试。'; }
  };
  const palette = document.createElement('dialog'); palette.id = 'workbench-palette'; palette.setAttribute('aria-label','查找工作台操作');
  palette.innerHTML = '<label>查找操作<input type="search" placeholder="输入操作名称" autocomplete="off"></label><div class="command-results"></div><footer class="muted">Enter 执行 · Escape 关闭</footer>';
  document.body.append(palette);
  const commands = [['导入材料','new-material'],['查找材料名称','material-search'],['原件与资源','tab:material'],['交给模型','tab:handoff'],['成稿与导出','tab:result'],['自由阅读','reader-unlock'],['连锁对照','reader-link'],['检查成稿问题','issue-trigger'],['导入 Markdown 成稿','import-markdown'],['工作台设置','workbench-settings']];
  const renderCommands = () => {
    const query = palette.querySelector('input').value.trim(); const list = palette.querySelector('.command-results'); list.replaceChildren();
    for (const [label,id] of commands.filter(([label])=>label.includes(query))) {
      const target = id.startsWith('tab:') ? $(`.steps [data-tab="${id.slice(4)}"]`) : $('#'+id);
      const b = document.createElement('button'); b.type='button';b.textContent=label;b.disabled=!target||target.disabled||$('#workspace').hidden&&id!=='new-material'&&id!=='material-search'&&id!=='workbench-settings';
      b.onclick=()=>{palette.close();if(target.tagName==='INPUT')target.focus();else target.click();};list.append(b);
    }
    for(const material of (deps.list?.()||[]).filter(p=>query&&(p.library?.display_name||p.title||'').toLowerCase().includes(query.toLowerCase())).slice(0,12)){
      const b=document.createElement('button');b.type='button';b.textContent='打开材料：'+(material.library?.display_name||material.title);b.onclick=()=>{palette.close();workspace.activate(material.id);};list.append(b);
    }
    if (!list.children.length) list.textContent = '没有匹配的材料或操作';
  };
  let commandTrigger;
  const openCommands = () => { globalThis.WBSelect.close();document.querySelectorAll('.tool-menu[open]').forEach(d=>d.open=false);commandTrigger=document.activeElement; palette.querySelector('input').value='';renderCommands();palette.showModal();palette.querySelector('input').focus(); };
  $('#workbench-command').onclick = openCommands;
  palette.querySelector('input').oninput=renderCommands;
  palette.addEventListener('close',()=>commandTrigger?.focus({preventScroll:true}));
  palette.addEventListener('keydown',e=>{if(e.key==='Escape'&&palette.querySelector('input').value){e.preventDefault();palette.querySelector('input').value='';renderCommands();}else if(e.key==='Enter'&&e.target.tagName==='INPUT'){e.preventDefault();palette.querySelector('button:not(:disabled)')?.click();}else if(e.key==='ArrowDown'&&e.target.tagName==='INPUT'){e.preventDefault();palette.querySelector('button:not(:disabled)')?.focus();}});
  document.addEventListener('keydown', e=>{if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'&&!document.querySelector('dialog[open]')){e.preventDefault();openCommands();}});
  // Native form controls remain the data owners. Shared comboboxes present them
  // consistently, including controls inserted by the issue/version controllers.
  globalThis.WBSelect.configure({host:()=>document.querySelector('dialog[open]')||document.querySelector('.issue-drawer.is-open')||document.fullscreenElement||document.body,beforeOpen:()=>{}});
  let queued = false;
  const enhance = () => { queued=false;globalThis.WBSelect.enhance(document); };
  new MutationObserver(records=>{if(queued||!records.some(r=>r.target.closest?.('select')||[...r.addedNodes].some(n=>n.nodeType===1&&(n.matches('select')||n.querySelector('select')))))return;queued=true;queueMicrotask(enhance);}).observe(document.body,{childList:true,subtree:true});
  document.addEventListener('pdf-state',enhance);
  enhance(); apply();
  installTooltips();
  installMenus();
  return workspace;
}

function installMenus() {
  const position = details => {
    const panel=[...details.children].find(n=>n.classList.contains('menu-panel'));
    if(!panel)return;
    const trigger=details.querySelector('summary'),rect=trigger.getBoundingClientRect();
    panel.style.position='fixed';panel.style.right='auto';panel.style.maxHeight=Math.max(80,innerHeight-32)+'px';
    panel.style.left=Math.max(8,Math.min(rect.right-panel.offsetWidth,innerWidth-panel.offsetWidth-8))+'px';
    panel.style.top=Math.max(8,Math.min(rect.bottom+8,innerHeight-panel.offsetHeight-8))+'px';
  };
  document.addEventListener('toggle',e=>{
    if(!e.target.matches('.tool-menu'))return;
    if(e.target.open){
      globalThis.WBSelect.close();
      for(const menu of document.querySelectorAll('.tool-menu[open]'))if(menu!==e.target&&!menu.contains(e.target)&&!e.target.contains(menu))menu.open=false;
      position(e.target);
    }
  },true);
  // A click outside dismisses the open menu before another command can run.
  document.addEventListener('click',e=>{
    const open=[...document.querySelectorAll('.tool-menu[open]')];
    if(open.length&&!open.some(d=>d.contains(e.target))&&!e.target.closest('.select-popup')){
      open.forEach(d=>d.open=false);e.preventDefault();e.stopImmediatePropagation();
    }
  },true);
  document.addEventListener('click',e=>{
    const action=e.target.closest('button,a'),menu=action?.closest('.tool-menu');
    if(menu&&action.getAttribute('role')!=='combobox'&&!menu.querySelector('input,textarea'))menu.open=false;
  });
  document.addEventListener('keydown',e=>{
    if(e.key!=='Escape')return;
    const menus=[...document.querySelectorAll('.tool-menu[open]')];
    const last=menus.at(-1);
    if(last){last.open=false;last.querySelector('summary')?.focus({preventScroll:true});e.preventDefault();e.stopImmediatePropagation();}
  },true);
  window.addEventListener('resize',()=>document.querySelectorAll('.tool-menu[open]').forEach(d=>position(d)));
}

function installTooltips() {
  const tip = document.createElement('div');tip.id='workbench-tooltip';tip.className='workbench-tooltip';tip.setAttribute('role','tooltip');tip.hidden=true;document.body.append(tip);
  let owner=null;
  const hide = () => { owner?.removeAttribute('aria-describedby');owner=null;tip.hidden=true; };
  const show = node => {
    if(!node||node.closest('dialog:not([open])'))return; hide();owner=node;node.removeAttribute('title');tip.textContent=node.dataset.tooltip;tip.hidden=false;node.setAttribute('aria-describedby',tip.id);
    const host=node.closest('dialog[open]')||document.querySelector('.issue-drawer.is-open')||document.body;host.append(tip);
    const box=node.getBoundingClientRect(),width=tip.offsetWidth,height=tip.offsetHeight;
    tip.style.left=Math.max(8,Math.min(box.left,innerWidth-width-8))+'px';tip.style.top=Math.max(8,Math.min(box.bottom+8,innerHeight-height-8))+'px';
  };
  document.addEventListener('pointerover',e=>{const node=e.target.closest('[data-tooltip]');if(node!==owner)show(node);});
  document.addEventListener('pointerout',e=>{if(!owner?.contains(e.relatedTarget)&&!tip.contains(e.relatedTarget))hide();});
  document.addEventListener('focusin',e=>show(e.target.closest('[data-tooltip]')));
  document.addEventListener('focusout',hide);
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&!tip.hidden){hide();e.preventDefault();if(!document.querySelector('.tool-menu[open]'))e.stopImmediatePropagation();}},true);
  document.addEventListener('scroll',hide,true);
}
