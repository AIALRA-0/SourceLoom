import {readJSON,readBlob} from './processor_read.js?v=loading-reliability-20261004';

const labels = {
  repair_table:'预览修正', confirm_manual:'对照这张表', confirm_table:'查看核对结果',
  add_page_reference:'添加原页参考', replace_page_reference:'用完整原页替换正文表格',
  table_image:'用完整原页替换正文表格', insert_resource:'预览补入原图',
  restore_resource:'恢复原图文件',
  source_only:'本次仅作回查', exclude_scope:'明确排除本次正文范围',
  retain_difference:'保留当前差异（不标记核对一致）', format:'预览格式整理',
};
const categories = {repair:'需修复', confirm:'待确认', format:'可选整理', service:'服务异常'};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const readLocal = key => {try{return JSON.parse(localStorage.getItem(key) || 'null');}catch{return null;}};
const writeLocal = (key,value) => {try{value == null ? localStorage.removeItem(key) : localStorage.setItem(key,JSON.stringify(value));}catch{}};

// An issue is a task bound to the saved version. A preview never edits content;
// an uncertain save is queried by its existing identity, never dispatched again.
export class IssueDrawer {
  constructor({host,onApplied=()=>{},onLocate=()=>{},onViewOriginal=null,onOpenOriginal=null,onSummary=()=>{},onCapture=()=>null,onRestore=()=>{}}) {
    Object.assign(this,{host,onApplied,onLocate,onViewOriginal,onOpenOriginal,onSummary,onCapture,onRestore});
    this.epoch=0;this.context=null;this.abort=null;this.selected=null;this.busy=false;
    this.blockWheel=e=>{if(this.isOpen()&&!this.host.contains(e.target)&&!e.target.closest('dialog[open]'))e.preventDefault();};
  }
  isOpen(){return this.host.classList.contains('is-open');}
  async api(path,body,{write=false}={}) {
    if(body===undefined)return readJSON(path,{signal:this.abort?.signal});
    const response=await fetch(path,{...(!write?{signal:this.abort?.signal}:{}),...(body!==undefined?{method:'POST',headers:{'Content-Type':'application/json','X-SourceLoom':'1'},body:JSON.stringify(body)}:{})});
    let value;
    try{value=await response.json();}catch{const e=Error('没有收到可确认的操作回执');e.uncertain=write;throw e;}
    if(!response.ok){const e=Error(value.detail||value.message||value.error||'操作未完成，请重新查看当前版本');e.status=response.status;throw e;}
    return value;
  }
  base(context=this.context){return `/api/processor/projects/${encodeURIComponent(context.projectId)}/versions/${encodeURIComponent(context.versionId)}`;}
  pendingKey(){return `sourceloom-issue-operation:${this.context?.projectId}`;}
  laterKey(){return `sourceloom-issue-later:${this.context?.projectId}:${this.data?.source_digest}:${this.data?.draft_digest}`;}
  taskCategory(g){return g.category||({format:'format',table:'confirm',resource:'repair'}[g.kind]||'service');}
  groups(){return this.data?.issues||[];}
  group(){return this.groups().find(g=>g.id===this.selected);}
  actionLabel(g,a){
    if(Number(g.page)>0&&['add_page_reference','replace_page_reference'].includes(a))return a==='add_page_reference'?`添加第 ${g.page} 页原页参考`:`改用第 ${g.page} 页原页参考`;
    return g.action_labels?.[a]||labels[a]||a;
  }
  laterIds(){return readLocal(this.laterKey())||[];}
  async refresh(context) {
    const changed=!this.context||this.context.projectId!==context.projectId||this.context.versionId!==context.versionId;
    if(changed){this.cancelSource();this.manualReady=false;this.previewReceipt=null;this.selected=null;this.feedback=null;this.data=null;this.serviceError=null;if(context.projectId!==this.context?.projectId||context.versionId!==this.undoVersion){this.completion=null;this.undoVersion=null;}}
    this.context={...context};const epoch=++this.epoch;
    this.abort?.abort();this.abort=new AbortController();
    if(this.isOpen()&&changed)this.loading();
    try {
      const data=await this.api(this.base()+'/issues');if(epoch!==this.epoch)return;
      this.data=data;this.onSummary(data.summary);
      if(!this.completion&&data.last_issue_result?.version_id===context.versionId){this.completion=data.last_issue_result;this.undoVersion=data.last_issue_result.status==='undone'||data.last_issue_result.undo_available===false?null:context.versionId;}
      this.host.dispatchEvent(new CustomEvent('issues-summary',{bubbles:true,detail:data}));
      if(!this.groups().some(g=>g.id===this.selected)){
        const later=this.laterIds();this.selected=(this.groups().find(g=>!later.includes(g.id))||this.groups()[0])?.id;
      }
      if(this.isOpen())this.render();return data;
    }catch(e){if(e.name!=='AbortError'&&e.code!=='aborted'&&epoch===this.epoch){this.serviceError=e.message;if(this.isOpen())this.render();}}
  }
  loading(){
    const hadFocus=this.host.contains(document.activeElement);
    this.host.innerHTML='<div class="issue-drawer-head"><h2>成稿问题</h2><button type="button" class="issue-close" aria-label="关闭问题处理">×</button></div><p class="issue-feedback" role="status">正在检查当前保存版本…</p>';
    this.host.querySelector('.issue-close').onclick=()=>this.close();this.bindKeyboard();
    if(hadFocus)this.host.focus({preventScroll:true});
  }
  async open(context) {
    if(!this.isOpen()){
      this.busy=false;this.pending=null;
      this.focus=document.activeElement;this.readingSnapshot=this.onCapture();this.inerted=[];
      let node=this.host;
      while(node.parentElement){for(const sibling of node.parentElement.children)if(sibling!==node){this.inerted.push([sibling,sibling.inert]);sibling.inert=true;}node=node.parentElement;if(node===document.body)break;}
      this.oldOverflow=document.body.style.overflow;document.body.style.overflow='hidden';
      document.addEventListener('wheel',this.blockWheel,{capture:true,passive:false});
    }
    this.host.classList.add('is-open');this.host.setAttribute('role','dialog');this.host.setAttribute('aria-modal','true');this.host.setAttribute('aria-label','成稿问题处理');this.host.tabIndex=-1;
    this.loading();this.host.focus();await this.refresh(context);
    const pending=readLocal(this.pendingKey());
    if(this.isOpen()&&pending&&pending.source_digest===this.data?.source_digest){this.pending=pending;this.render();await this.queryPending();}
  }
  close() {
    const wasOpen=this.isOpen();this.host.classList.remove('is-open');this.manualReady=false;this.cancelSource();this.abort?.abort();this.epoch++;
    if(wasOpen){for(const [node,inert] of this.inerted||[])node.inert=inert;this.inerted=[];document.body.style.overflow=this.oldOverflow||'';document.removeEventListener('wheel',this.blockWheel,true);this.onRestore(this.readingSnapshot);if(this.focus?.isConnected)this.focus.focus({preventScroll:true});}
  }
  bindKeyboard() {
    this.host.onkeydown=e=>{
      if(e.key==='Escape'){e.preventDefault();e.stopPropagation();const menu=this.host.querySelector('details.issue-other[open]');if(menu)menu.open=false;else this.close();}
      if(e.key==='Tab'){
        const nodes=[...this.host.querySelectorAll('button,summary,input,select,a[href],[tabindex="0"]')].filter(n=>!n.disabled&&n.getClientRects().length&&!n.closest('[hidden]'));
        if(!nodes.length)return;const first=nodes[0],last=nodes.at(-1);
        if(e.shiftKey&&(document.activeElement===first||document.activeElement===this.host)){e.preventDefault();last.focus();}
        else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first.focus();}
      }
    };
  }
  error(message){this.feedback={message,error:true};this.updateFeedback();this.reveal(this.host.querySelector('.issue-feedback'));}
  updateFeedback(){const node=this.host.querySelector('.issue-feedback');if(node){node.textContent=this.feedback?.message||'';node.setAttribute('role',this.feedback?.error?'alert':'status');}}
  choose(id){if(this.busy||this.pending)return;this.cancelSource();this.selected=id;this.previewReceipt=null;this.manualReady=false;this.completion=null;this.differenceIndex=0;this.acknowledgeAction=null;this.feedback=null;this.render();}
  render() {
    if(!this.isOpen())return;
    const d=this.data||{issues:[],resolved:[]};const g=this.group();const hadFocus=this.host.contains(document.activeElement);const later=this.laterIds();
    const previousSource=this.sourceLoad?.key===this.sourceIdentity(g)&&!this.completion?this.host.querySelector('.issue-source-pane'):null;
    if(!previousSource)this.cancelSource();
    const counts=Object.keys(categories).map(c=>({c,list:this.groups().filter(x=>this.taskCategory(x)===c)})).filter(x=>x.list.length);
    this.host.innerHTML=`<header class="issue-drawer-head"><div><h2>${esc(g&&!this.completion?(g.kind==='table'&&this.taskCategory(g)==='confirm'?'确认'+(g.title.match(/表\s*\d+/)?.[0]||'表格')+'的呈现方式':g.title):'成稿问题')}</h2><p>${g&&!this.completion?`${g.page?'第 '+esc(g.page)+' 页 · ':''}${this.groups().indexOf(g)+1}/${this.groups().length}`:esc(d.summary||'检查范围：本版本文件、资源与格式')}</p></div><button type="button" class="issue-close" aria-label="关闭问题处理">×</button></header>
      <main class="issue-task-body">
      <div class="issue-feedback" aria-live="polite" role="status"></div>
      ${this.pending?`<section class="issue-service"><h3>${this.pending.operation==='undo'?'撤销':'保存'}结果尚待确认</h3><p>只查询刚才那次操作，不重新执行。成稿不会被重复修改。</p><button type="button" class="issue-query">查询原操作结果</button></section>`:''}
      ${this.serviceError?`<section class="issue-service"><h3>当前检查暂不可用</h3><p>${esc(this.serviceError)}</p><button type="button" class="issue-refresh">重新获取检查结果</button><p>这只读取状态，不会重新处理成稿。</p></section>`:''}
      ${!this.completion&&counts.length>1?`<nav class="issue-categories" aria-label="问题类型">${counts.map(({c,list})=>`<button type="button" data-category="${c}" aria-pressed="${String(g&&this.taskCategory(g)===c)}" ${this.busy||this.pending?'disabled':''}>${categories[c]} ${c==='format'?(d.counts?.format_occurrences||list.reduce((n,x)=>n+(x.occurrence_count||1),0))+' 处':list.length}</button>`).join('')}</nav>`:''}
      ${!this.completion&&g?`${this.groups().length>1?`<label class="issue-task-selector">当前任务<select id="issue-current-select" ${this.busy||this.pending?'disabled':''}>${this.groups().map(x=>`<option value="${esc(x.id)}" ${x.id===g.id?'selected':''}>${esc(categories[this.taskCategory(x)])} · ${esc(x.title)}${later.includes(x.id)?'（稍后）':''}</option>`).join('')}</select></label>`:''}<article class="issue-card" data-issue-id="${esc(g.id)}">${later.includes(g.id)?'<p class="issue-category-label">已暂缓，尚未处理</p>':''}${this.taskContent(g)}<div class="issue-local-preview"></div><details class="issue-technical"><summary>技术详情</summary><p>版本：${esc(d.version_id)}<br>原件对象：${esc((g.source_ids||[]).join(', ')||'无')}<br>${esc(g.evidence_scope||'当前局部范围，不是全文语义验证')}</p></details></article>`:!this.completion?`<p class="issue-all-clear">${esc(d.summary||'当前检查范围没有待处理任务')}。这不代表全文语义已经验证。</p>`:''}
      ${(d.resolved||[]).length?`<details class="issue-checked"><summary>已完成的局部检查 ${d.resolved.length} 项</summary>${d.resolved.map(x=>`<section><strong>${esc(x.title)}</strong><p>${esc(x.evidence_scope||'本版本局部来源核对')}</p><p>正文未改；无需再次确认，不创建新版本。</p></section>`).join('')}</details>`:''}
      ${this.completion?`<section class="issue-completion" role="status"><strong>${esc(this.completion.completion)}</strong><p>${esc(this.completion.evidence_scope||'只适用于本次局部处理；全文语义状态没有升级。')}</p>${this.undoVersion?`<button type="button" class="issue-undo">${esc(this.completion.undo_label||'撤销本次处理')}</button>`:''}<button type="button" class="issue-continue">返回待处理列表</button></section>`:''}</main>${!this.completion&&g?this.footer(g):''}`;
    if(previousSource)this.host.querySelector('.issue-source-pane')?.replaceWith(previousSource);
    this.host.querySelector('.issue-close').onclick=()=>this.close();this.bindKeyboard();this.updateFeedback();
    if(this.context?.readonly){for(const control of this.host.querySelectorAll('[data-action],[data-later],.issue-undo,.issue-restore-file,.issue-position-select'))control.disabled=true;const message=document.createElement('p');message.className='issue-scope';message.textContent='只读示例：可查看问题与原件；试用副本后再处理。';this.host.querySelector('.issue-drawer-head>div').append(message);}
    this.host.querySelector('#issue-current-select')?.addEventListener('change',e=>this.choose(e.target.value));
    this.host.querySelectorAll('[data-category]').forEach(b=>b.onclick=()=>this.choose(this.groups().find(x=>this.taskCategory(x)===b.dataset.category)?.id));
    this.host.querySelector('[data-later]')?.addEventListener('click',()=>this.later());
    this.host.querySelectorAll('[data-action]').forEach(b=>b.onclick=()=>this.beginAction(b.dataset.action));
    this.host.querySelectorAll('[data-locate]').forEach(b=>b.onclick=()=>this.onLocate(g));
    this.bindSourceZoom();
    const expand=this.host.querySelector('[data-source-expand]');if(expand)expand.onclick=e=>this.onViewOriginal?.(g,e.currentTarget);
    this.host.querySelector('.issue-restore-file')?.addEventListener('change',e=>this.restoreFile(e.target.files?.[0]));
    if(g&&!this.completion)this.loadSource(g);
    this.host.querySelector('.issue-refresh')?.addEventListener('click',()=>{this.serviceError=null;this.refresh(this.context);});
    this.host.querySelector('.issue-query')?.addEventListener('click',()=>this.queryPending());
    this.host.querySelector('.issue-undo')?.addEventListener('click',()=>this.undo());
    this.host.querySelector('.issue-continue')?.addEventListener('click',()=>{this.completion=null;this.render();});
    this.host.querySelector('#issue-difference-select')?.addEventListener('change',e=>{this.differenceIndex=Number(e.target.value);this.previewReceipt=null;this.render();});
    this.bindPanes();this.highlightDifference(g);
    if(this.previewReceipt)this.showPreview(this.previewReceipt);
    if(hadFocus)this.host.querySelector(this.completion?'.issue-completion button':'.issue-close')?.focus({preventScroll:true});
  }
  taskContent(g) {
    if(g.kind==='format')return `<p>${esc(g.explanation)}</p><p class="issue-scope">${esc(g.impact)}</p>${this.formatExamples(g)}<details class="issue-all-occurrences"><summary>查看全部 ${g.occurrence_count||0} 处 · ${(g.occurrences||[]).length} 个位置</summary>${(g.occurrences||[]).map(h=>`<section><strong>正文第 ${h.line} 行 · ${h.count} 处</strong><del>${esc(h.before)}</del><ins>${esc(h.after)}</ins></section>`).join('')}</details>`;
    const deltas=g.differences||[];this.differenceIndex=Math.min(this.differenceIndex||0,Math.max(0,deltas.length-1));const delta=deltas[this.differenceIndex];
    const difference=delta?`<section class="issue-cell-target"><strong>${esc(delta.row)} / ${esc(delta.column)}</strong><div><span>当前值 <b>${esc(delta.current)}</b></span><span>原件值 <b>${esc(delta.expected)}</b></span></div><p>只处理这处差异；其他单元格与正文保持。</p>${deltas.length>1?`<label>待修差异<select id="issue-difference-select">${deltas.map((x,i)=>`<option value="${i}" ${i===this.differenceIndex?'selected':''}>${esc(x.row)} / ${esc(x.column)}</option>`).join('')}</select></label>`:''}</section>`:'';
    const sourceURL=g.source_preview_html?null:g.source_preview_url||(g.kind==='table'&&g.page?this.base()+'/issue-image/'+encodeURIComponent(g.id):null);
    const status=`<div class="issue-source-status" role="status" aria-live="polite"><p>正在读取当前原件预览…</p><button type="button" data-source-retry hidden>重试原件预览</button>${this.onOpenOriginal?'<button type="button" data-source-original>查看原文件</button>':''}</div>`;
    const source=g.source_preview_html?`<div class="issue-original-html">${this.sourcePreviewHTML(g)}</div>${status}${this.onViewOriginal?`<div class="issue-source-controls"><button type="button" data-source-expand>${g.kind==='table'||g.source_kind==='table'?'查看原表':'查看原件'}</button></div>`:''}`:sourceURL?`<div class="issue-source-stage"><img class="issue-source-image" data-source-url="${esc(sourceURL)}" hidden alt="${g.kind==='table'?'原表所在完整原页':'原件资源'}${g.page?` · 第 ${g.page} 页`:''}"></div>${status}<div class="issue-source-controls"><button type="button" data-source-zoom="fit">适合宽度</button><button type="button" data-source-zoom="minus" aria-label="缩小原表">−</button><output class="issue-source-scale">适合宽度</output><button type="button" data-source-zoom="plus" aria-label="放大原表">＋</button>${this.onViewOriginal?`<button type="button" data-source-expand>${g.source_preview_precision==='table'?'查看原表':'查看原页'}</button>`:''}</div>`:g.category==='service'?'<p>资源文件暂不可用。再次插入资源标记不能恢复文件。</p>':'<p>当前没有可靠的原件局部预览，请参阅已保存原件；不会猜测内容。</p>';
    const comparison=`<div class="issue-pane-tabs" role="tablist" aria-label="对照内容"><button type="button" role="tab" data-pane="source" aria-selected="false">原件</button><button type="button" role="tab" data-pane="current" aria-selected="true">当前</button>${this.previewReceipt?.body_changed?'<button type="button" role="tab" data-pane="change" aria-selected="false">将修改</button>':''}</div><div class="issue-comparison" data-active-pane="current"><section class="issue-source-pane" data-pane-panel="source"><h4>原件${g.page?` · 第 ${g.page} 页`:''}</h4>${source}${g.page?'<button type="button" data-locate>参阅原页</button>':''}${g.kind==='table'&&!g.source_preview_html?'<p class="issue-source-note">完整原页，可放大和平移；没有裁掉表头。</p>':''}</section><section data-pane-panel="current"><h4>当前成稿</h4>${g.draft_preview?`<div class="issue-draft-preview">${this.resourcePreviewHTML(g.draft_preview,g)}</div>`:g.kind==='resource'?'<p>当前正文中的对应资源需要核查；请先查看推荐插入位置。</p>':'<p>对应正文位置尚不能可靠定位；不会猜测替换。</p>'}</section><section class="issue-change-pane" data-pane-panel="change"><h4>将修改</h4><p>选择处理方式后显示局部预览。</p></section></div>`;
    return `${difference}<p class="issue-scope">${g.kind==='table'&&this.taskCategory(g)==='confirm'?'对照原表与当前表格；确认只记录本版本关系，不改正文。':esc(g.explanation||g.evidence_scope||g.impact||'只处理当前局部；不代表全文语义审核')}</p>${comparison}${g.restore_requires_file?'<label class="issue-position">选择这份原件中缺失的原图文件<input class="issue-restore-file" type="file" accept="image/*"><span class="issue-scope">仅接受与已保存原图摘要完全一致的文件；不改写、不替换原件。</span></label>':''}${(g.actions||[]).includes('insert_resource')?`<label class="issue-position">插入位置<select class="issue-position-select"><option value="">${g.previous_position_available?'恢复本材料已保存的原位置':'请选择插入位置'}</option>${(this.data.blocks||[]).map(b=>`<option value="${esc(b.block_id)}">${esc(b.label||b.locator||'正文段落')}</option>`).join('')}</select></label>`:''}`;
  }
  formatExamples(g) {return `<div class="issue-format-examples">${(g.examples||g.occurrences?.slice(0,3)||[]).map(h=>`<section><strong>正文第 ${h.line} 行</strong><div class="issue-format-pair"><div><span>整理前</span><p>${esc(h.before)}</p></div><div><span>整理后</span><p>${esc(h.after)}</p></div></div></section>`).join('')}</div>`;}
  footer(g) {
    const main=g.recommended_action||(g.actions||[])[0];const others=(g.actions||[]).filter(a=>a!==main);
    const label=main==='confirm_manual'||main==='confirm_table'?'确认本版本表格表示':this.actionLabel(g,main);
    const blocked=a=>this.busy||this.pending||(this.requiresSource(a,g)&&!this.sourceReady());
    return `<footer class="issue-task-footer"><div class="issue-actions"><button type="button" data-later ${this.busy||this.pending?'disabled':''}>稍后处理</button>${others.length?`<details class="issue-other"><summary>其他处理方式</summary><div>${others.map(a=>`<button type="button" data-action="${esc(a)}" ${blocked(a)?'disabled':''}>${esc(this.actionLabel(g,a))}</button>`).join('')}</div></details>`:''}${main?`<button type="button" class="issue-primary" data-action="${esc(main)}" ${blocked(main)?'disabled':''}>${esc(label)}</button>`:''}</div></footer>`;
  }
  sourceIdentity(g=this.group()) {return g?JSON.stringify([this.context?.projectId,this.context?.versionId,this.data?.source_digest,this.data?.draft_digest,g.id,g.source_preview_url||'',g.source_preview_html||'']):null;}
  requiresSource(action,g=this.group()){return g?.kind==='table'&&['confirm_manual','confirm_table'].includes(action);}
  sourceReady(){return this.sourceLoad?.key===this.sourceIdentity()&&this.sourceLoad?.state==='ready'&&this.sourceLoad.pane?.isConnected===true;}
  cancelSource(){const load=this.sourceLoad;this.sourceLoad=null;if(!load)return;load.controller.abort();for(const url of load.urls)URL.revokeObjectURL(url);load.urls.clear();}
  sourcePreviewHTML(g){const template=document.createElement('template');template.innerHTML=this.resourcePreviewHTML(g.source_preview_html,g);for(const img of template.content.querySelectorAll('img[src]')){img.dataset.sourceUrl=img.getAttribute('src');img.removeAttribute('src');img.hidden=true;}return template.innerHTML;}
  decodeSource(img,signal){
    let timer,off;
    const cancelled=new Promise((_,reject)=>{off=()=>reject(Object.assign(Error('原件解码已取消'),{code:'aborted'}));if(signal.aborted)off();else signal.addEventListener('abort',off,{once:true});timer=setTimeout(()=>reject(Object.assign(Error('原件图像未能完成解码，请重试当前预览。'),{code:'timeout'})),15000);});
    return Promise.race([img.decode(),cancelled]).then(()=>{if(!img.naturalWidth||!img.naturalHeight)throw Error('原件图像没有可读内容，请重试预览或查看原文件。');}).finally(()=>{clearTimeout(timer);signal.removeEventListener('abort',off);});
  }
  updateSource(){
    const load=this.sourceLoad,pane=load?.pane;if(!pane?.isConnected)return;
    pane.dataset.sourceState=load.state;
    const status=pane.querySelector('.issue-source-status');if(status){status.hidden=load.state==='ready';status.setAttribute('role',load.state==='failed'?'alert':'status');status.querySelector('p').textContent=load.state==='failed'?`${load.error||'原件预览未能载入。'} 还不能确认本版本表格表示。`:'正在读取当前原件预览…';status.querySelector('[data-source-retry]').hidden=load.state!=='failed';}
    for(const button of pane.querySelectorAll('[data-source-zoom]'))button.disabled=load.state!=='ready';
    this.disableActions(this.busy||!!this.pending);
    const apply=this.host.querySelector('.issue-apply');if(apply&&this.requiresSource(this.previewReceipt?.action||this.previewReceipt?.request?.action))apply.disabled=this.busy||!!this.pending||!this.sourceReady();
  }
  async loadSource(g,{retry=false}={}){
    const key=this.sourceIdentity(g),pane=this.host.querySelector('.issue-source-pane');if(!pane)return;
    if(!retry&&this.sourceLoad?.key===key){this.updateSource();return;}
    this.cancelSource();
    const load=this.sourceLoad={key,pane,state:'loading',controller:new AbortController(),urls:new Set()};
    const current=()=>this.sourceLoad===load&&!load.controller.signal.aborted&&this.isOpen()&&this.sourceIdentity()===key&&pane.isConnected;
    const retryButton=pane.querySelector('[data-source-retry]');if(retryButton)retryButton.onclick=()=>this.loadSource(this.group(),{retry:true});
    const original=pane.querySelector('[data-source-original]');if(original)original.onclick=e=>this.onOpenOriginal?.(this.group(),e.currentTarget);
    for(const img of pane.querySelectorAll('img[data-source-url]')){img.hidden=true;img.removeAttribute('src');}
    this.updateSource();
    try{
      const images=[...pane.querySelectorAll('img[data-source-url]')],blobs=new Map();
      if(!images.length&&!pane.querySelector('.issue-original-html')?.textContent.trim())throw Error('当前没有可读的原件预览，请查看原文件。');
      await Promise.all(images.map(async img=>{
        const path=img.dataset.sourceUrl;
        if(!blobs.has(path))blobs.set(path,readBlob(path,{signal:load.controller.signal}));
        const blob=await blobs.get(path);if(!current())return;
        const url=URL.createObjectURL(blob);load.urls.add(url);img.src=url;
        await this.decodeSource(img,load.controller.signal);if(!current())return;img.hidden=false;
      }));
      if(current()){load.state='ready';this.updateSource();}
    }catch(error){
      if(!current()||error.code==='aborted')return;
      load.state='failed';load.error=error.message;load.controller.abort();
      for(const img of pane.querySelectorAll('img[data-source-url]')){img.hidden=true;img.removeAttribute('src');}
      for(const url of load.urls)URL.revokeObjectURL(url);load.urls.clear();this.updateSource();
    }
  }
  bindSourceZoom(){
    const stage=this.host.querySelector('.issue-source-stage'),img=this.host.querySelector('.issue-source-image');if(!stage||!img)return;
    let factor=Number(stage.dataset.scale)||1;
    const draw=()=>{stage.dataset.scale=factor;img.style.width=factor===1?'100%':`${stage.clientWidth*factor}px`;img.style.maxWidth='none';this.host.querySelector('.issue-source-scale').textContent=factor===1?'适合宽度':`${Math.round(factor*100)}%`;};
    this.host.querySelectorAll('[data-source-zoom]').forEach(button=>button.onclick=()=>{factor=button.dataset.sourceZoom==='fit'?1:Math.max(.5,Math.min(4,factor+(button.dataset.sourceZoom==='plus'?.25:-.25)));draw();});
    stage.onpointerdown=e=>{if(e.button!==0)return;const x=e.clientX,y=e.clientY,sl=stage.scrollLeft,st=stage.scrollTop;stage.setPointerCapture(e.pointerId);stage.onpointermove=m=>{stage.scrollLeft=sl+x-m.clientX;stage.scrollTop=st+y-m.clientY;};};stage.onpointerup=()=>stage.onpointermove=null;stage.onpointercancel=()=>stage.onpointermove=null;
    for(const box of this.host.querySelectorAll('.issue-draft-preview,.issue-original-html')){box.scrollLeft=0;box.scrollTop=0;const table=box.querySelector('table');if(table){table.style.minWidth=Math.max(720,Math.max(...[...table.rows].map(row=>row.cells.length))*100)+'px';if(!table.querySelector('[rowspan],[colspan]'))table.classList.add('issue-sticky-table');}}
  }
  bindPanes() {const tabs=[...this.host.querySelectorAll('[data-pane]')];tabs.forEach(b=>{b.onclick=()=>{this.host.querySelector('.issue-comparison').dataset.activePane=b.dataset.pane;tabs.forEach(t=>t.setAttribute('aria-selected',String(t===b)));};b.onkeydown=e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key))return;e.preventDefault();const active=tabs.filter(t=>!t.disabled),i=active.indexOf(b);const next=active[e.key==='Home'?0:e.key==='End'?active.length-1:(i+(e.key==='ArrowLeft'?-1:1)+active.length)%active.length];next?.click();next?.focus();};});}
  highlightDifference(g) {
    const delta=g?.differences?.[this.differenceIndex||0];if(!delta)return;
    const rows=[...this.host.querySelectorAll('.issue-draft-preview tr')];
    const matches=rows.filter(row=>row.querySelector('th,td')?.textContent.trim()===delta.row);
    const indexed=Number.isInteger(delta.current_row_index)?rows[delta.current_row_index]:null;
    const row=indexed&&indexed.querySelector('th,td')?.textContent.trim()===delta.row?indexed:matches.length===1?matches[0]:null;
    const cell=row?.querySelectorAll('th,td')[delta.column_index];
    if(cell){cell.classList.add('issue-target-cell');cell.setAttribute('aria-label',`${delta.row} / ${delta.column} 当前 ${delta.current} 原件 ${delta.expected}`);const box=cell.closest('.issue-draft-preview'),r=cell.getBoundingClientRect(),b=box.getBoundingClientRect();box.scrollTop=Math.max(0,box.scrollTop+r.top-b.top-64);box.scrollLeft=Math.max(0,box.scrollLeft+r.left-b.left-150);const footer=this.host.querySelector('.issue-task-footer');if(footer){const bottom=cell.getBoundingClientRect().bottom,limit=footer.getBoundingClientRect().top-12;if(bottom>limit)this.host.scrollTop+=bottom-limit;}}
  }
  later() {const g=this.group();if(!g)return;writeLocal(this.laterKey(),[...new Set([...this.laterIds(),g.id])]);this.feedback={message:'已暂缓，仍保留在待处理列表；没有记为已解决。'};this.render();}
  async beginAction(action) {
    if(this.context?.readonly||this.busy||this.pending)return;const g=this.group();if(!g)return;
    if(this.requiresSource(action,g)&&!this.sourceReady()){this.error('请先等待当前原件预览可读，或重试预览；尚未确认表格表示。');return;}
    this.host.querySelector('.issue-other')?.removeAttribute('open');
    if(action==='restore_resource'&&g.restore_requires_file){this.host.querySelector('.issue-restore-file')?.click();return;}
    if(action==='confirm_manual'&&!this.manualReady){
      this.manualReady=true;const panel=this.host.querySelector('.issue-local-preview');
      panel.innerHTML='<p class="issue-scope">人工对照，不声称程序逐格证明。</p><label class="issue-ack"><input type="checkbox">我已对照本版本的原表与成稿表格</label><button type="button" class="issue-manual-confirm" disabled>预览本次人工确认</button>';
      const box=panel.querySelector('input'),button=panel.querySelector('button');box.onchange=()=>button.disabled=!box.checked||!this.sourceReady();button.onclick=()=>this.preview(action,{acknowledged:true});
      this.host.querySelector('.issue-primary').disabled=true;this.host.querySelector('.issue-primary').textContent='确认本版本表格表示';box.focus();return;
    }
    if(['exclude_scope','retain_difference'].includes(action)){
      const panel=this.host.querySelector('.issue-local-preview');panel.innerHTML=`<h4>${esc(this.actionLabel(g,action))}</h4><p>${action==='exclude_scope'?'明确缩小本次正文范围，原件仍保留；这项内容不再承诺进入正文，不能因此声称完整忠实核对通过。':'保持当前差异并记录你的选择；这不等于修复，也不会标记来源一致。'}</p><label class="issue-ack"><input type="checkbox">我理解并明确选择上述影响</label><button type="button" class="issue-scope-confirm" disabled>预览这个决定</button>`;
      const box=panel.querySelector('input'),button=panel.querySelector('button');box.onchange=()=>button.disabled=!box.checked;button.onclick=()=>this.preview(action,{acknowledged:true});box.focus();return;
    }
    await this.preview(action);
  }
  async restoreFile(file) {
    if(!file||this.busy||this.pending)return;const g=this.group(),epoch=this.epoch;
    if(!g?.restore_requires_file)return;
    if(file.size>18*1024*1024){this.error('原图超过 18 MiB，请使用这份原件对应的原始资源文件。');return;}
    this.busy=true;this.disableActions(true);this.feedback={message:'正在核对所选文件是否为这份原件的同一张原图；尚未保存。'};this.updateFeedback();
    try{
      const bytes=await file.arrayBuffer(),hash=[...new Uint8Array(await crypto.subtle.digest('SHA-256',bytes))].map(b=>b.toString(16).padStart(2,'0')).join('');
      if(epoch!==this.epoch)return;
      if(hash!==g.expected_resource_sha){this.error('所选文件与原件中的原图不一致，没有保存或替换。请选对应原始文件。');return;}
      const array=new Uint8Array(bytes);let binary='';for(let start=0;start<array.length;start+=8192)binary+=String.fromCharCode(...array.subarray(start,start+8192));
      this.busy=false;await this.preview('restore_resource',{resource_bytes:btoa(binary)});
    }catch(e){if(epoch===this.epoch)this.error(e.message||'无法读取所选文件，没有修改成稿。');}
    finally{if(epoch===this.epoch){this.busy=false;this.disableActions(false);}}
  }
  async preview(action,extra={}) {
    if(this.busy||this.pending)return;const g=this.group(),epoch=this.epoch;if(!g)return;
    if(this.requiresSource(action,g)&&!this.sourceReady()){this.error('当前原件预览尚不可读，没有提交确认。');return;}
    const block=g.block_id||this.host.querySelector('.issue-position-select')?.value;
    const delta=g.differences?.[this.differenceIndex||0];
    const request={issue_id:g.id,action,...(block?{block_id:block}:{}),...extra,...(action==='repair_table'&&delta&&!delta.missing_row?{difference_index:this.differenceIndex||0}:{})};
    this.busy=true;this.feedback={message:'正在准备局部预览；尚未修改成稿。'};this.updateFeedback();this.disableActions(true);
    try{const receipt=await this.api(this.base()+'/issue-preview',request);if(epoch!==this.epoch)return;this.previewReceipt={...receipt,request};this.feedback=null;this.updateFeedback();this.showPreview(this.previewReceipt);}
    catch(e){if(e.name!=='AbortError'&&epoch===this.epoch)this.error(e.message);}
    finally{if(epoch===this.epoch){this.busy=false;this.disableActions(false);}}
  }
  disableActions(disabled){this.host.querySelectorAll('[data-action],[data-later],#issue-current-select,.issue-manual-confirm,.issue-scope-confirm').forEach(n=>n.disabled=disabled||this.context?.readonly===true||(this.requiresSource(n.dataset?.action)&&(!this.sourceReady()||(n.dataset.action==='confirm_manual'&&this.manualReady)))||(n.classList?.contains('issue-manual-confirm')&&(!this.sourceReady()||!this.host.querySelector('.issue-ack input')?.checked)));}
  reveal(node){if(!node)return;const r=node.getBoundingClientRect(),h=this.host.getBoundingClientRect(),head=this.host.querySelector('.issue-drawer-head');const body=node.closest('.issue-task-body');if(body)body.scrollTop+=r.top-body.getBoundingClientRect().top-16;else this.host.scrollTop+=r.top-h.top-(head?.getBoundingClientRect().height||0)-16;}
  showPreview(receipt) {
    const g=this.group();if(!g||receipt.issue_id!==g.id)return;
    const panel=this.host.querySelector('.issue-local-preview');const delta=g.differences?.[receipt.request?.difference_index??this.differenceIndex??0];
    let change;
    if(receipt.action==='repair_table'&&delta&&receipt.request?.difference_index!==undefined){
      change=`<div class="issue-cell-preview"><strong>${esc(delta.row)} / ${esc(delta.column)}</strong><div><span>修改前 <del>${esc(delta.current)}</del></span><span>修改后 <ins>${esc(delta.expected)}</ins></span></div><p>只修改 1 格；其他单元格和正文不变。</p></div>`;
    }else if(receipt.action==='format')change=this.formatExamples(g)+`<p>将处理全部 ${g.occurrence_count||0} 处列出的安全标点。代码、公式、表格、链接和引用保持。</p>`;
    else if(!receipt.body_changed)change='<p class="issue-no-body-change">正文保持原样，只保存这个版本的局部决定；不创建内容相同的新版本。</p>';
    else if(['add_page_reference','replace_page_reference'].includes(receipt.action)){
      const parsed=new DOMParser().parseFromString(receipt.after_html||'','text/html');
      const keys=new Set((receipt.derived_resources||[]).map(r=>'assets/'+r.sha256));
      const images=[...parsed.querySelectorAll('img')].filter(img=>keys.has(img.getAttribute('src'))).map(img=>img.outerHTML).join('');
      change=receipt.action==='add_page_reference'?`<p>保留当前可编辑表格及其单元格，新增下方完整原页参考。该原页也包含表格以外的内容。</p><div class="issue-rendered-patch">${images||receipt.after_html||''}</div>`:`<div class="issue-patch"><section><h5>将移除的正文表格</h5><div class="issue-rendered-patch">${g.draft_preview||receipt.before_html||''}</div></section><section><h5>将添加的完整原页</h5><div class="issue-rendered-patch">${images||receipt.after_html||''}</div><p>这是整页参考，含表格以外的内容；该表格将不再是可编辑正文表格。</p></section></div>`;
    }else change=`<div class="issue-patch"><section><h5>处理前</h5><div class="issue-rendered-patch">${receipt.before_html||'<p>所选插入位置</p>'}</div></section><section><h5>处理后</h5><div class="issue-rendered-patch">${receipt.after_html||'<p>正文不变</p>'}</div></section></div>`;
    panel.innerHTML=`<h4>处理预览</h4><p class="issue-impact">${esc(receipt.summary)}</p><p class="issue-scope">${esc(receipt.evidence_scope||'本次局部范围；全文语义状态不升级')}</p>${this.resourcePreviewHTML(change,g,receipt)}<div class="issue-preview-actions"><button type="button" class="issue-apply">${esc(receipt.action==='repair_table'&&receipt.request?.difference_index!==undefined?'修正 1 格并保存':receipt.apply_label||'应用并保存')}</button><button type="button" class="issue-cancel">取消预览</button></div>`;
    const changePane=this.host.querySelector('.issue-change-pane');if(changePane)changePane.innerHTML='<p>下方显示这次处理的唯一局部预览。</p>';
    this.host.querySelector('[data-pane="change"]')?.removeAttribute('disabled');
    this.host.querySelector('.issue-task-footer')?.classList.add('has-preview');
    panel.querySelector('.issue-cancel').onclick=()=>{this.previewReceipt=null;this.manualReady=false;this.render();};
    panel.querySelector('.issue-apply').onclick=()=>this.apply(receipt);
    this.reveal(panel);panel.querySelector('.issue-apply').focus({preventScroll:true});
  }
  resourcePreviewHTML(raw,g,receipt={}){const template=document.createElement('template');template.innerHTML=raw;this.resolveImages(template.content,receipt,g);return template.innerHTML;}
  resolveImages(panel,receipt,g){panel.querySelectorAll('img[src^="assets/"]').forEach(img=>{const key=img.getAttribute('src').slice(7);img.src=(receipt.derived_resources||[]).some(r=>r.sha256===key)?this.base()+'/issue-image/'+encodeURIComponent(g.id):`/api/processor/projects/${encodeURIComponent(this.context.projectId)}/files/${encodeURIComponent(key)}`;});}
  async apply(receipt) {
    if(this.busy||this.pending)return;
    if(this.requiresSource(receipt.action||receipt.request?.action)&&!this.sourceReady()){this.error('原件预览尚不可读，没有保存确认；请重新查看当前原件。');return;}
    if(this.host.contains(document.activeElement))this.host.focus({preventScroll:true});
    const epoch=this.epoch,context={...this.context};this.busy=true;
    this.pending={operation:'apply',project_id:context.projectId,version_id:context.versionId,source_digest:this.data.source_digest,draft_digest:this.data.draft_digest,preview_id:receipt.preview_id,completion:receipt.completion,undo_label:receipt.undo_label,undo_available:receipt.undo_available,evidence_scope:receipt.evidence_scope};
    writeLocal(this.pendingKey(),this.pending);this.disableActions(true);
    this.host.querySelector('.issue-apply').disabled=true;this.feedback={message:'正在保存这次局部处理；请等待原操作结果。'};this.updateFeedback();
    try {
      const result=await this.api(this.base(context)+'/issue-apply',{preview_id:receipt.preview_id,request:receipt.request},{write:true});
      if(epoch!==this.epoch)return;
      await this.acceptResult(result,result.processor?.last_issue_result||this.pending);
    }catch(e){
      if(epoch!==this.epoch)return;
      if(e.status){writeLocal(this.pendingKey(),null);this.pending=null;this.error(e.message);this.previewReceipt=null;this.render();}
      else{this.feedback={message:'没有收到可靠保存回执。请查询原操作结果，不会重新执行这次修改。',error:true};this.render();}
    }finally{if(epoch===this.epoch){this.busy=false;if(!this.pending)this.disableActions(false);}}
  }
  async acceptResult(result,completion) {
    const snapshot=this.readingSnapshot;
    const fallback=this.pending;writeLocal(this.pendingKey(),null);this.pending=null;this.previewReceipt=null;this.manualReady=false;this.busy=false;
    this.completion={...fallback,...completion};const version=completion.version_id||result.processor?.active_version||this.context.versionId;this.undoVersion=this.completion.undo_available===false?null:version;this.context={...this.context,versionId:version};
    await this.onApplied(result);if(!this.isOpen()||snapshot!==this.readingSnapshot)return;await this.refresh(this.context);if(!this.isOpen()||snapshot!==this.readingSnapshot)return;this.feedback=null;this.render();
    this.host.querySelector('.issue-completion button')?.focus({preventScroll:true});
  }
  async queryPending() {
    if(this.busy||!this.pending)return;const epoch=this.epoch;this.busy=true;const pending=this.pending;
    const button=this.host.querySelector('.issue-query');if(button)button.disabled=true;
    try{
      const status=await this.api(`/api/processor/projects/${encodeURIComponent(pending.project_id)}/issue-operations/${encodeURIComponent(pending.preview_id)}`);
      if(epoch!==this.epoch)return;
      if(status.status==='undone'){
        const result=await this.api(`/api/processor/projects/${encodeURIComponent(pending.project_id)}`);if(epoch!==this.epoch)return;
        await this.acceptResult(result,{...pending,...status,undo_available:false});
      }else if(status.status==='saved'&&pending.operation!=='undo'){
        const result=await this.api(`/api/processor/projects/${encodeURIComponent(pending.project_id)}`);if(epoch!==this.epoch)return;
        if(status.active)await this.acceptResult(result,{...pending,...status});
        else{writeLocal(this.pendingKey(),null);this.pending=null;this.feedback={message:'原操作已保存；当前已有后续版本，未切换或覆盖当前成稿。可从版本记录查看。'};this.render();}
      }else{this.feedback={message:pending.operation==='undo'?'原处理记录仍存在，尚不能确认撤销是否完成。保留原身份继续查询，不自动重新撤销。':status.completion||'尚未找到原操作的保存结果。保留该身份继续查询，不自动重发。'};this.updateFeedback();}
    }catch(e){if(e.name!=='AbortError'&&epoch===this.epoch)this.error(`暂时无法查询原操作：${e.message}`);}
    finally{if(epoch===this.epoch)this.busy=false;if(button?.isConnected)button.disabled=false;}
  }
  async undo() {
    if(this.busy||this.pending||!this.undoVersion)return;const epoch=this.epoch;this.busy=true;
    if(this.host.contains(document.activeElement))this.host.focus({preventScroll:true});
    this.host.querySelector('.issue-undo').disabled=true;
    const pending={operation:'undo',project_id:this.context.projectId,version_id:this.undoVersion,source_digest:this.data.source_digest,draft_digest:this.data.draft_digest,preview_id:this.completion?.preview_id};
    writeLocal(this.pendingKey(),pending);
    try{
      const result=await this.api(this.base({...this.context,versionId:this.undoVersion})+'/issue-undo',{preview_id:this.completion?.preview_id}, {write:true});if(epoch!==this.epoch)return;
      writeLocal(this.pendingKey(),null);
      const old=this.completion;this.undoVersion=null;this.completion={completion:result.processor?.last_issue_result?.completion||`${old?.undo_label||'撤销本次处理'}已完成`,evidence_scope:'只撤销本次局部操作；其他后续编辑不会被覆盖。'};
      const snapshot=this.readingSnapshot;this.busy=false;this.context={...this.context,versionId:result.processor.active_version};await this.onApplied(result);if(!this.isOpen()||snapshot!==this.readingSnapshot)return;await this.refresh(this.context);if(!this.isOpen()||snapshot!==this.readingSnapshot)return;this.render();this.host.querySelector('.issue-completion button')?.focus({preventScroll:true});
    }catch(e){if(epoch===this.epoch){if(e.status){writeLocal(this.pendingKey(),null);this.error(e.message);}else{this.pending=pending;this.feedback={message:'撤销结果未确认。请查询原操作，不会重复提交撤销。',error:true};this.completion=null;this.render();}}}
    finally{if(epoch===this.epoch)this.busy=false;const b=this.host.querySelector('.issue-undo');if(b)b.disabled=false;}
  }
}
