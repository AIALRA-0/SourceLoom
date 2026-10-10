// Workbench 2.3.1 shell adapter. Only UI identity/preferences live here;
// materials, versions, resources and saves stay in the SourceLoom services.
const KEY='sourceloom-workspace-layout/1';
const q=s=>document.querySelector(s);
const make=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!==undefined)n.textContent=text;return n;};
export function layoutBudget(area,mode,config){
  const left=mode!=='narrow'&&!config.hideLeft,right=mode==='wide'&&!config.hideRight;
  const gap=16,min=216,budget=Math.max(0,area-360-(left?gap:0)-(right?gap:0));
  let lw=left?Math.max(min,Math.min(400,config.leftWidth)):0,rw=right?Math.max(min,Math.min(400,config.rightWidth)):0;
  if(left&&right&&lw+rw>budget){const extra=Math.max(0,budget-2*min),sum=lw+rw-2*min;lw=min+(sum?extra*(lw-min)/sum:0);rw=min+(sum?extra*(rw-min)/sum:0);}
  else if(left)lw=Math.min(lw,Math.max(min,budget));else if(right)rw=Math.min(rw,Math.max(min,budget));
  return {left,right,lw,rw,columns:`${lw}px ${left?gap:0}px minmax(0,1fr) ${right?gap:0}px ${rw}px`};
}
export function normalizeLayout(raw={}){raw=raw&&typeof raw==='object'?raw:{};return {leftView:raw.leftView==='inspector'?'inspector':'explorer',hideLeft:raw.hideLeft===true,hideRight:raw.hideRight===true,leftWidth:Number.isFinite(raw.leftWidth)?Math.max(216,Math.min(400,raw.leftWidth)):256,rightWidth:Number.isFinite(raw.rightWidth)?Math.max(216,Math.min(400,raw.rightWidth)):272};}
export class TemplateWorkspace{
  constructor(deps){
    this.deps=deps;let raw={};try{raw=JSON.parse(localStorage.getItem(KEY)||'null');if(!raw){const old=JSON.parse(localStorage.getItem('sourceloom-file-tree/1')||'{}');raw={leftWidth:old.width,hideLeft:old.collapsed};}}catch{raw={};}
    this.config=normalizeLayout(raw);this.tabs=Array.isArray(raw.tabs)?raw.tabs.filter(t=>t&&typeof t.id==='string'&&typeof t.title==='string').slice(0,30):[];
    this.active=null;this.drawer=q('#panel-drawer');this.inspectorTab='properties';this.inspectorEpoch=0;
    this.bind();this.mountPanels();this.applyLayout();this.renderTabs();this.updateInspector();
    this.observer=new ResizeObserver(()=>this.applyLayout());this.observer.observe(q('#layout'));
  }
  mode(){return innerWidth>=1120?'wide':innerWidth>=760?'medium':'narrow';}
  panel(view){return q(view==='explorer'?'#sidebar':'#workbench-inspector-panel');}
  side(view){return this.config.leftView===view?'left':'right';}
  view(side){return side==='left'?this.config.leftView:this.config.leftView==='explorer'?'inspector':'explorer';}
  persist(){try{localStorage.setItem(KEY,JSON.stringify({...this.config,tabs:this.tabs}));}catch{this.deps.notify('浏览器未能保存布局设置，材料仍由原服务保存',true);}}
  configure(raw){this.closeDrawer();this.config=normalizeLayout(raw);this.changeLayout(()=>{});this.persist();}
  mountPanels(){for(const side of ['left','right']){const pane=this.panel(this.view(side));if(pane!==this.drawer.firstElementChild&&pane.parentElement!==q('#'+side+'-slot'))q('#'+side+'-slot').append(pane);}}
  applyLayout(){
    this.mountPanels();const mode=this.mode(),b=layoutBudget(q('#layout').clientWidth,mode,this.config);q('#app').dataset.layoutMode=mode;
    q('#layout').style.gridTemplateColumns=b.columns;document.documentElement.dataset.leftHidden=String(!b.left);document.documentElement.dataset.rightHidden=String(!b.right);
    for(const side of ['left','right']){const visible=b[side],split=q('#split-'+side);q('#'+side+'-slot').hidden=!visible;split.hidden=!visible;split.setAttribute('aria-valuenow',String(Math.round(side==='left'?b.lw:b.rw)));split.setAttribute('aria-valuemin','216');split.setAttribute('aria-valuemax',String(Math.min(400,Math.max(216,q('#layout').clientWidth-376))));q(side==='left'?'#workbench-sidebar':'#workbench-inspector').setAttribute('aria-pressed',String(visible));}
    const explorerVisible=this.drawer.open&&this.drawerView==='explorer'||b[this.side('explorer')];
    for(const id of ['reading-sidebar','sidebar-toggle'])q('#'+id)?.setAttribute('aria-expanded',String(!!explorerVisible));
  }
  changeLayout(fn){this.deps.reader.layoutChange(()=>{fn();this.applyLayout();});}
  toggleSide(side){const drawer=this.mode()==='narrow'||this.mode()==='medium'&&side==='right';if(drawer){this.openDrawer(this.view(side));return;}this.changeLayout(()=>this.config[side==='left'?'hideLeft':'hideRight']=!this.config[side==='left'?'hideLeft':'hideRight']);this.persist();}
  toggleView(view){const side=this.side(view),hidden=this.config[side==='left'?'hideLeft':'hideRight'];if(this.drawer.open&&this.drawerView===view){this.closeDrawer();return;}if(this.mode()==='narrow'||this.mode()==='medium'&&side==='right')this.openDrawer(view);else {this.changeLayout(()=>this.config[side==='left'?'hideLeft':'hideRight']=!hidden);this.persist();}}
  hide(view){if(this.drawerView===view)this.closeDrawer();else{this.changeLayout(()=>this.config[this.side(view)==='left'?'hideLeft':'hideRight']=true);this.persist();}}
  swap(view,target){this.closeDrawer();this.changeLayout(()=>{this.config.leftView=target==='left'?view:view==='explorer'?'inspector':'explorer';this.config.hideLeft=false;this.config.hideRight=false;this.mountPanels();});this.persist();}
  openDrawer(view){if(this.drawer.open)this.closeDrawer();this.drawerTrigger=document.activeElement;this.drawerView=view;this.drawer.dataset.side=this.side(view);this.drawer.setAttribute('aria-label',view==='explorer'?'材料面板':'检查器面板');this.drawer.append(this.panel(view));this.drawer.showModal();this.applyLayout();this.drawer.querySelector('button')?.focus();}
  closeDrawer(){if(!this.drawerView)return;const view=this.drawerView;this.drawerView=null;this.drawer.close();q('#'+this.side(view)+'-slot').append(this.panel(view));this.applyLayout();this.drawerTrigger?.focus({preventScroll:true});}
  opened(project){if(!project)return;this.active=project.id;const title=project.library?.display_name||project.title;const tab=this.tabs.find(t=>t.id===project.id);if(tab)tab.title=title;else this.tabs.push({id:project.id,title});this.persist();this.deps.library.tree.selected=new Set([project.id]);this.deps.library.tree.syncCurrent();this.renderTabs();this.updateInspector();this.syncStatus();if(this.drawer.open)this.closeDrawer();}
  async activate(id){
    try{await this.deps.open(id);if(this.deps.current()?.id!==id)return;q('#workspace').hidden=false;this.deps.library.view.hidden=true;q('#empty').hidden=true;this.deps.showTab();this.opened(this.deps.current());}catch(e){this.deps.notify(e.message||'材料未能打开',true);}
  }
  async closeTab(id){
    const index=this.tabs.findIndex(t=>t.id===id);if(index<0)return;
    if(id===this.active&&this.deps.isDirty()){this.deps.notify('先保存当前成稿，再关闭材料标签',true);return;}
    if(id===this.active&&this.tabs.length>1){const next=this.tabs[index===0?1:index-1];await this.activate(next.id);if(this.active!==next.id)return;}
    this.tabs=this.tabs.filter(t=>t.id!==id);
    if(this.active===id){this.deps.reader.persist();this.active=null;q('#workspace').hidden=true;q('#empty').hidden=false;document.body.classList.remove('reading-active');}
    this.persist();this.renderTabs();this.syncStatus();
  }
  renderTabs(){
    const host=q('#document-tabs');const focusId=document.activeElement?.closest('.doc-tab')?.dataset.documentId,focusClose=document.activeElement?.classList.contains('tab-close');
    host.replaceChildren();for(const tab of this.tabs){const row=make('div','doc-tab');row.dataset.documentId=tab.id;row.classList.toggle('active',tab.id===this.active);
      const open=make('button','tab-open',tab.title);open.type='button';open.title=tab.title;open.setAttribute('role','tab');open.setAttribute('aria-selected',String(tab.id===this.active));open.setAttribute('aria-controls','main-content');open.tabIndex=tab.id===this.active||!this.active&&tab===this.tabs[0]?0:-1;open.onclick=()=>this.activate(tab.id);
      const close=make('button','tab-close','×');close.type='button';close.setAttribute('aria-label','关闭标签：'+tab.title);close.onclick=()=>this.closeTab(tab.id);row.append(open,close);host.append(row);}
    if(focusId)host.querySelector(`[data-document-id="${CSS.escape(focusId)}"] .${focusClose?'tab-close':'tab-open'}`)?.focus({preventScroll:true});
  }
  syncStatus(){q('#workspace-save-state').textContent=this.active?q('#edit-state')?.textContent||'原件已保存':'尚未打开材料';const active=q('#document-tabs .doc-tab.active');if(active){active.classList.toggle('dirty',this.deps.isDirty());active.querySelector('.tab-open').setAttribute('aria-label',active.querySelector('.tab-open').textContent+(this.deps.isDirty()?'，未保存修改':''));}const p=this.deps.current();q('#workspace-main-meta').textContent=this.active?(p?.processor?.versions?.length||Object.keys(p?.processor?.versions||{}).length||0)+' 个成稿版本':'';}
  async updateInspector(){
    const tree=this.deps.library.tree,ids=[...tree.selected],nodes=ids.map(id=>tree.getNode(id)).filter(Boolean),current=this.deps.current();
    const target=nodes.length===1?nodes[0]:nodes.length?null:current;const signature=JSON.stringify([nodes.map(n=>[n.id,n.title,n.revision,n.path]),current?.id,current?.library?.display_name,current?.title,current?.processor?.active_version,Object.keys(current?.processor?.versions||{}),this.deps.library.mode,this.inspectorTab]);if(signature===this.inspectorSignature)return;this.inspectorSignature=signature;const epoch=++this.inspectorEpoch;
    q('#workspace-selection-state').textContent=nodes.length?`已选择 ${nodes.length} 项`:current?'正在阅读：'+(current.library?.display_name||current.title):'尚未选择材料';
    q('#workspace-space-state').textContent={mine:'我的材料',examples:'示例库',archives:'已归档',trash:'回收站'}[this.deps.library.mode]||'我的材料';
    const properties=q('#inspector-properties'),history=q('#inspector-versions');properties.replaceChildren();history.replaceChildren();
    const title=make('h3','',target?.library?.display_name||target?.title||(nodes.length?`${nodes.length} 项材料`:'选择材料查看属性'));properties.append(title);
    const dl=make('dl','property-group');const field=(label,value)=>dl.append(make('dt','',label),make('dd','',String(value??'—')));
    if(target){field('类型',target.kind==='folder'?'文件夹':'材料');field('位置',target.path||'材料库');field('身份',target.id);field('操作',target.id===current?.id?'正在阅读':'已选择，尚未打开');}
    else if(nodes.length){field('选择',nodes.map(n=>n.title).join('、'));field('管理','使用材料树菜单批量移动或回收，不影响当前正文');}
    properties.append(dl);history.append(make('p','muted',target?.kind==='folder'?'文件夹没有成稿版本':nodes.length>1?'选择一份材料查看版本':'正在读取版本…'));
    if(!target||target.kind==='folder')return;
    if(this.inspectorTab!=='versions'){history.replaceChildren(make('p','muted','选择版本页签查看保存记录'));return;}
    let detail=target.id===current?.id?current:null;
    try{if(!detail)detail=await this.deps.api(`/api/processor/projects/${encodeURIComponent(target.id)}/version-history`);if(epoch!==this.inspectorEpoch)return;}
    catch(e){if(epoch===this.inspectorEpoch){this.inspectorSignature=null;history.replaceChildren(make('p','muted',e.message||'版本未能读取'));}return;}
    history.replaceChildren();const versions=Object.values(detail.processor?.versions||detail.versions||{});
    if(!versions.length)history.append(make('p','muted','尚无成稿版本，原件已独立保存'));
    for(const version of versions.slice().reverse()){const b=make('button','version-row',version.label||version.title||`版本 ${version.id}`);b.type='button';b.onclick=async()=>{await this.activate(target.id);if(this.deps.current()?.id!==target.id)return;const select=q('#version-picker');if(![...select.options].some(o=>o.value===version.id))return;select.value=version.id;select.dispatchEvent(new Event('change',{bubbles:true}));};history.append(b);}
  }
  bind(){
    q('#workbench-sidebar').onclick=()=>this.toggleSide('left');q('#workbench-inspector').onclick=()=>this.toggleSide('right');q('#sidebar-toggle').onclick=()=>this.toggleView('explorer');q('#tab-import').onclick=()=>q('#new-material').click();
    document.addEventListener('workbench-panel-request',e=>e.detail.action==='hide'?this.hide(e.detail.view):this.toggleView(e.detail.view));
    let queued=false;document.addEventListener('workbench-tree-selection',()=>{if(queued)return;queued=true;requestAnimationFrame(()=>setTimeout(()=>{queued=false;this.updateInspector();},0));});
    for(const b of document.querySelectorAll('[data-panel-swap]'))b.onclick=()=>this.swap(b.dataset.panelSwap,this.side(b.dataset.panelSwap)==='left'?'right':'left');
    for(const b of document.querySelectorAll('[data-panel-hide]'))b.onclick=()=>this.hide(b.dataset.panelHide);
    for(const b of document.querySelectorAll('[data-inspector-tab]'))b.onclick=()=>{this.inspectorTab=b.dataset.inspectorTab;for(const t of document.querySelectorAll('[data-inspector-tab]'))t.setAttribute('aria-selected',String(t===b));q('#inspector-properties').hidden=this.inspectorTab!=='properties';q('#inspector-versions').hidden=this.inspectorTab!=='versions';void this.updateInspector();};
    this.drawer.addEventListener('cancel',e=>{e.preventDefault();this.closeDrawer();});this.drawer.addEventListener('click',e=>{if(e.target===this.drawer)this.closeDrawer();});
    window.addEventListener('resize',()=>{if(this.drawer.open)this.closeDrawer();this.changeLayout(()=>{});});
    for(const side of ['left','right'])this.installResize(side);
    for(const head of document.querySelectorAll('.pane-head'))this.installPanelMove(head);
    q('#document-tabs').addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key)||!e.target.matches('.tab-open'))return;const tabs=[...q('#document-tabs').querySelectorAll('.tab-open')],i=tabs.indexOf(e.target);e.preventDefault();const target=tabs[e.key==='Home'?0:e.key==='End'?tabs.length-1:(i+(e.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length];target.focus();});
    new MutationObserver(()=>this.syncStatus()).observe(q('#edit-state'),{childList:true,subtree:true,characterData:true});
  }
  installPanelMove(head){
    const view=head.closest('[data-view]').dataset.view;
    head.addEventListener('pointerdown',e=>{
      if(e.button!==0||e.target.closest('button,a,input,select,summary'))return;e.preventDefault();
      const origin={x:e.clientX,y:e.clientY};let moving=false,target=null;
      const clean=()=>{window.removeEventListener('pointermove',move);window.removeEventListener('pointerup',up);window.removeEventListener('pointercancel',cancel);window.removeEventListener('blur',cancel);document.removeEventListener('keydown',key);document.body.classList.remove('moving-view');document.querySelectorAll('.drop-target.hover').forEach(n=>n.classList.remove('hover'));};
      const move=event=>{if(!moving&&Math.abs(event.clientX-origin.x)+Math.abs(event.clientY-origin.y)<7)return;moving=true;document.body.classList.add('moving-view');const slot=document.elementFromPoint(event.clientX,event.clientY)?.closest('.slot');target=slot?.id==='left-slot'?'left':slot?.id==='right-slot'?'right':null;document.querySelectorAll('.drop-target').forEach(n=>n.classList.toggle('hover',n.parentElement===slot));};
      const up=()=>{clean();if(moving&&target)this.swap(view,target);};
      const cancel=()=>clean();
      const key=event=>{if(event.key==='Escape'){event.preventDefault();cancel();}};
      window.addEventListener('pointermove',move);window.addEventListener('pointerup',up);window.addEventListener('pointercancel',cancel);window.addEventListener('blur',cancel);document.addEventListener('keydown',key);
    });
  }
  installResize(side){const node=q('#split-'+side),key=side==='left'?'leftWidth':'rightWidth';let drag=null;
    const end=cancel=>{if(!drag)return;const start=drag;drag=null;if(cancel)this.changeLayout(()=>this.config[key]=start.width);document.body.classList.remove('panel-resizing');this.persist();};
    node.addEventListener('pointerdown',e=>{e.preventDefault();drag={x:e.clientX,width:this.config[key]};node.setPointerCapture(e.pointerId);document.body.classList.add('panel-resizing');});
    node.addEventListener('pointermove',e=>{if(!drag)return;this.changeLayout(()=>this.config[key]=Math.max(216,Math.min(400,drag.width+(e.clientX-drag.x)*(side==='left'?1:-1))));});
    node.addEventListener('pointerup',()=>end(false));node.addEventListener('pointercancel',()=>end(true));node.addEventListener('lostpointercapture',()=>end(false));window.addEventListener('blur',()=>end(true));
    document.addEventListener('keydown',e=>{if(e.key==='Escape'&&drag){e.preventDefault();e.stopPropagation();end(true);}},true);
    node.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();this.changeLayout(()=>this.config[key]=e.key==='Home'?216:e.key==='End'?400:Math.max(216,Math.min(400,this.config[key]+(e.key==='ArrowRight'?16:-16)*(side==='left'?1:-1))));this.persist();});
  }
}
