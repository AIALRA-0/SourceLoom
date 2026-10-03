// Compact reading UI; all document state remains in the existing reader/version system.
const q = s => document.querySelector(s);
const icons = {
  menu:'M4 6h16M4 12h16M4 18h16', search:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14m5 12 6 6',
  link:'M9 15l6-6M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0m2 2 1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0',
  free:'M3 3l18 18M8 16l-1 1a4 4 0 0 1-6-6l3-3m9-1 1-1a4 4 0 0 1 6 6l-3 3',
  focus:'M8 3H3v5m13-5h5v5M3 16v5h5m13-5v5h-5',
  rotate:'M4 10a8 8 0 1 1 2 9M4 3v7h7', edit:'M4 16l12-12 4 4-12 12H4v-4',
  copy:'M9 9h11v12H9zM15 9V3H3v12h6', thumbs:'M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z',
  download:'M12 3v12m-5-5 5 5 5-5M4 16v5h16v-5', hand:'M7 12V7a1.5 1.5 0 0 1 3 0v4-7a1.5 1.5 0 0 1 3 0v7-6a1.5 1.5 0 0 1 3 0v7-4a1.5 1.5 0 0 1 3 0v7c0 5-2 7-6 7H9l-5-6a2 2 0 0 1 3-2l2 2',
};
function el(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text)n.textContent=text;return n;}
function icon(node,type,label,short=''){node.replaceChildren();const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');svg.setAttribute('viewBox','0 0 24 24');svg.setAttribute('aria-hidden','true');svg.innerHTML=`<path d="${icons[type]||icons.menu}"/>`;node.append(svg);if(short)node.append(el('span','',short));node.classList.add('tool-icon');node.title=label;node.setAttribute('aria-label',label);node.dataset.tooltip=label;return node;}
function button(id,type,label,short=''){const n=el('button');n.type='button';n.id=id;return icon(n,type,label,short);}
function menu(label){const d=el('details','tool-menu'),s=el('summary','',label);s.setAttribute('aria-label',label);const p=el('div','menu-panel');d.append(s,p);return {d,p};}
function move(s,p){const n=q(s);if(n)p.append(n);return n;}

export class ReadingWorkbench {
  constructor(deps){this.deps=deps;this.summaryToken=0;this.build();this.bind();this.loupe=new DocumentLoupe({...deps,tableReferences:()=>{const c=deps.context();return this.referenceContext?.projectId===c.projectId&&this.referenceContext?.versionId===c.versionId?this.referenceGroups||[]:[];}});}
  build(){
    this.capture();
    document.body.classList.add('reader-product');
    const doc=el('div','document-bar');doc.id='reading-document-bar';
    doc.append(button('reading-sidebar','menu','显示或收起材料列表'));
    move('#material-title',doc);move('#edit-state',doc);
    const versions=menu('版本历史');versions.d.id='reading-version-menu';move('#version-picker',versions.p);
    doc.append(el('button','issue-trigger','检查问题'));doc.lastChild.id='issue-trigger';
    icon(move('#toggle-editor',doc),'edit','编辑成稿','编辑');move('#send-readweave',doc);move('#open-readweave',doc);
    const more=menu('更多');more.d.id='document-menu';more.p.append(versions.d);move('.steps',more.p);move('#import-markdown',more.p);
    for(const s of ['#download-markdown','#export-package','#query-readweave','#copy-readweave-url'])move(s,more.p);
    more.p.append(el('hr'));move('#delivery-status',more.p);move('#semantic-status',more.p);move('#readweave-target',more.p);
    const facts=menu('材料详情');move('#material-summary',facts.p);move('#document-status',facts.p);more.p.append(facts.d);
    move('#refresh',more.p);
    q('.document-head')?.remove();
    const history=menu('交接记录');move('#request-history',history.p);more.p.append(history.d);
    const restore=el('button','restore-comparison','恢复对照');restore.id='restore-comparison';restore.type='button';restore.hidden=true;doc.append(restore,more.d);q('#workspace').prepend(doc);
    const toolbar=q('.compare-toolbar');toolbar.replaceChildren();toolbar.classList.add('reading-controls');toolbar.id='reading-controls';
    // Kept native controls retain their existing listeners and selectors.
    const old=q('.editor-toolbar');if(old)old.hidden=true;
    // Controls detached with the old toolbar are recovered from its captured nodes below.
    const left=q('#compare-source .section-heading');left.replaceChildren(...this.controls[0]);left.classList.add('pane-toolbar','source-toolbar');
    const right=el('div','pane-toolbar draft-toolbar');right.append(...this.controls[1]);
    const shared=el('div','reading-toolbar');shared.id='shared-reading-tools';
    const views=move('.mobile-compare-switch',shared);views.querySelector('#mobile-compare-left').textContent='原件';views.querySelector('#mobile-compare-right').textContent='成稿';
    shared.append(this.controls[2]);toolbar.append(left,shared,right);
    q('#compare-source').append(this.controls[3]);
  }
  // Capture existing reader controls before their toolbar is rebuilt.
  get controls(){return this._controls||[];}
  capture(){
    const nodes={};for(const id of ['compare-left','reader-unlock','reader-link','reading-size','reading-line-height','mapping-status','reader-toc','reader-search','reader-search-next','reader-pair','reader-independent'])nodes[id]=q('#'+id);
    const group=el('div','tool-group');nodes['compare-left'].setAttribute('aria-label','对照左侧视图');group.append(nodes['compare-left']);
    const sync=el('div','tool-group reader-modes');sync.setAttribute('role','group');sync.setAttribute('aria-label','对照模式');sync.append(icon(nodes['reader-unlock'],'free','解锁：两侧自由阅读','自由'),icon(nodes['reader-link'],'link','连锁：按最近操作侧立即对齐','连锁'));
    const find=menu('查找');find.d.id='reading-find';icon(find.d.querySelector('summary'),'search','查找原件或成稿');
    find.p.innerHTML='<label>在原件中查找<input id="pdf-search" type="search" placeholder="输入原文文字"></label><div class="inline-actions"><button id="pdf-search-prev" type="button">上一处</button><button id="pdf-search-next" type="button">下一处</button><output id="pdf-search-count" role="status">等待原件载入</output></div>';
    const draftFind=menu('查找');icon(draftFind.d.querySelector('summary'),'search','查找成稿');const label=el('label','','在成稿中查找');label.append(nodes['reader-search']);draftFind.p.append(label,nodes['reader-search-next']);
    const zoom=el('div','tool-group pdf-zoom-group');zoom.innerHTML='<button id="pdf-zoom-minus" type="button" aria-label="缩小原件" title="缩小原件">−</button><select id="pdf-zoom-mode" aria-label="原件缩放"><option value="width">适合宽度</option><option value="page">整页适合</option><option value="0.25">25%</option><option value="0.5">50%</option><option value="1">100%</option><option value="2">200%</option><option value="4">400%</option></select><button id="pdf-zoom-plus" type="button" aria-label="放大原件" title="放大原件">＋</button>';
    const aa=menu('Aa');aa.d.querySelector('summary').setAttribute('aria-label','成稿排版设置');for(const [id,text] of [['reading-size','字号'],['reading-line-height','行距']]){const l=el('label','',text);l.append(nodes[id]);aa.p.append(l);}aa.p.innerHTML+='<label>正文行宽<select id="reading-width"><option value="full">填满面板</option><option value="measure">适中</option></select></label>';
    // innerHTML above would destroy moved control listeners: restore the original nodes.
    for(const id of ['reading-size','reading-line-height']){const clone=aa.p.querySelector('#'+id);clone.replaceWith(nodes[id]);}
    const nav=el('details','reader-options tool-menu');nav.append(el('summary','','定位'));const np=el('div','menu-panel');nav.append(np);np.append(nodes['reader-toc'],nodes['reader-pair'],nodes['reader-independent'],el('p','muted','配对只保存本机阅读位置，不改正文'));
    const focus=menu('专注');focus.p.append(button('focus-source','focus','专注原件','原件'),icon(q('#focus-reading'),'focus','专注成稿','成稿'),button('reader-fullscreen','focus','全屏阅读','全屏'));
    const pages=q('.compare-page-tools');
    const title=el('span','pane-title','成稿');
    this._controls=[[group,find.d,pages,zoom],[title,draftFind.d,aa.d,nav,focus.d],sync,nodes['mapping-status']];
  }
  bind(){
    const layout=q('#result-layout'),restore=q('#restore-comparison');
    const refreshFocus=()=>restore.hidden=!layout.classList.contains('source-only')&&!layout.classList.contains('preview-only');
    new MutationObserver(refreshFocus).observe(layout,{attributes:true,attributeFilter:['class']});refreshFocus();
    restore.onclick=()=>this.deps.reader.layoutChange(()=>{layout.classList.remove('source-only','preview-only');q('#focus-source').setAttribute('aria-pressed','false');q('#focus-reading').setAttribute('aria-pressed','false');document.body.dataset.readerFocus='both';refreshFocus();});
    q('#reading-sidebar').onclick=()=>this.toggleSidebar();
    for(const id of ['reading-sidebar','sidebar-toggle']){q('#'+id).setAttribute('aria-controls','sidebar');q('#'+id).setAttribute('aria-expanded',String(!matchMedia('(max-width:700px)').matches));}
    q('#sidebar').addEventListener('click',e=>{if(matchMedia('(max-width:700px)').matches&&e.target.closest('button,a'))this.closeSidebar();});
    q('#issue-trigger').onclick=async()=>{if(this.deps.canProcessIssues?.()===false){this.deps.notify('请先保存当前修改，再处理本版本的问题',true);return;}if(document.fullscreenElement)await document.exitFullscreen();this.deps.issues.open(this.deps.context());};
    const left=q('#reading-controls>.source-toolbar');
    q('#compare-source').append(q('#mapping-status'));
    const tools=el('div','source-quick-tools');tools.append(button('pdf-thumbnails','thumbs','原件目录与页缩略图'),button('pdf-hand','hand','切换手形平移'),button('pdf-rotate','rotate','顺时针旋转原件'),button('pdf-copy','copy','复制所选原文'),button('pdf-copy-cite','copy','复制所选原文并附出处','引用'));
    left.append(tools.querySelector('#pdf-thumbnails'));const sourceMore=menu('原文件');sourceMore.p.innerHTML='<a id="pdf-original-open" target="_blank" rel="noopener noreferrer">独立打开原件</a><a id="pdf-original-download" download>下载原件</a>';tools.append(sourceMore.d);
    const originalTools=menu('原件工具');originalTools.d.classList.add('source-more');originalTools.p.append(tools);left.append(originalTools.d);
    const thumb=el('dialog','thumbnail-dialog');thumb.id='pdf-navigation';thumb.innerHTML='<div class="dialog-heading"><h2>原件目录与页面</h2><button type="button" aria-label="关闭原件导航">×</button></div><nav id="pdf-outline" aria-label="原生文档目录"></nav><div id="pdf-thumbnail-list" class="pdf-thumbnail-list"></div>';document.body.append(thumb);thumb.querySelector('button').onclick=()=>thumb.close();
    const view=()=>this.deps.reader.pdfView?.('compare');
    originalTools.d.addEventListener('mousedown',e=>{if(e.target.closest('button,summary')&&view()?.captureSelection())e.preventDefault();});
    const attempt=async fn=>{try{await fn();}catch(e){this.deps.notify(e.message||'操作未完成',true);}};
    q('#pdf-zoom-mode').onchange=e=>attempt(()=>view()?.setScale(isNaN(Number(e.target.value))?e.target.value:Number(e.target.value)));
    for(const [id,delta] of [['pdf-zoom-minus',-.25],['pdf-zoom-plus',.25]])q('#'+id).onclick=()=>attempt(()=>view()?.setScale(Math.max(.25,Math.min(4,(Number(view()?.state?.scale)||1)+delta))));
    q('#pdf-rotate').onclick=()=>attempt(()=>view()?.rotate());
    q('#pdf-hand').onclick=()=>{const b=q('#pdf-hand');b.setAttribute('aria-pressed',String(b.getAttribute('aria-pressed')!=='true'));view()?.setHand(b.getAttribute('aria-pressed')==='true');};
    for(const [id,cite] of [['pdf-copy',false],['pdf-copy-cite',true]])q('#'+id).onclick=()=>attempt(async()=>{const pdf=view();if(!pdf)throw Error('当前原件没有可复制的 PDF 文字层');await pdf.copy(cite);this.deps.notify(cite?'原文与出处已复制':'原文已复制');});
    const search=delta=>attempt(async()=>{this.deps.reader.interact('left');await view()?.search(q('#pdf-search').value,delta);});
    q('#pdf-search-next').onclick=()=>search(1);q('#pdf-search-prev').onclick=()=>search(-1);q('#pdf-search').onkeydown=e=>{if(e.key==='Enter'){e.preventDefault();search(e.shiftKey?-1:1);}};
    document.addEventListener('pdf-state',e=>{if(e.target.id!=='compare-source-viewer')return;const state=e.detail||view()?.state||{};q('#pdf-search-count').textContent=state.indexing?`正在检索 ${state.indexed||0}/${state.pages||'?'} 页`:state.query?`${Number(state.hit)||0} / ${Number(state.hits)||0} 处`:state.message||'';const v=view();if(v){q('#pdf-original-open').href=v.originalURL(false);q('#pdf-original-download').href=v.originalURL(true);}if(Number(state.scale)){q('#pdf-zoom-mode').value=['width','page'].includes(state.scaleMode)?state.scaleMode:String(state.scale);if(!q('#pdf-zoom-mode').value){const opt=new Option(`${Math.round(state.scale*100)}%`,String(state.scale));q('#pdf-zoom-mode').add(opt);q('#pdf-zoom-mode').value=String(state.scale);}}});
    q('#pdf-thumbnails').onclick=()=>attempt(async()=>{const pdf=view();if(!pdf)return;const outline=await pdf.outline();const out=q('#pdf-outline');out.replaceChildren();const add=(rows,depth=0)=>{for(const row of rows||[]){const b=el('button','',row.title);b.type='button';b.style.marginLeft=depth*12+'px';b.onclick=()=>{pdf.jumpDestination(row.dest);thumb.close();};out.append(b);add(row.items,depth+1);}};add(outline);if(!out.children.length)out.append(el('p','muted','原件没有原生大纲，使用页缩略图定位'));const list=q('#pdf-thumbnail-list');list.replaceChildren();for(const r of this.deps.resources().filter(r=>r.kind==='page')){const p=Number(r.page||r.locator?.match(/page\[(\d+)\]/)?.[1]);const b=el('button','thumbnail-page');b.type='button';const im=el('img');im.src=r.url;im.alt=`第 ${p} 页缩略图`;im.loading='lazy';b.append(im,el('span','',`第 ${p} 页`));b.onclick=()=>{this.deps.reader.jumpPage(p,'compare',true);thumb.close();};list.append(b);}thumb.showModal();});
    q('#focus-source').onclick=()=>this.deps.reader.layoutChange(()=>{const layout=q('#result-layout');layout.classList.toggle('source-only');layout.classList.remove('preview-only');q('#focus-reading').setAttribute('aria-pressed','false');q('#focus-source').setAttribute('aria-pressed',String(layout.classList.contains('source-only')));document.body.dataset.readerFocus=layout.classList.contains('source-only')?'source':'both';});
    q('#focus-reading').addEventListener('click',()=>{q('#result-layout').classList.remove('source-only');q('#focus-source').setAttribute('aria-pressed','false');document.body.dataset.readerFocus=q('#result-layout').classList.contains('preview-only')?'draft':'both';icon(q('#focus-reading'),'focus','专注成稿','成稿');this.deps.reader.persist();});
    q('#reader-fullscreen').onclick=()=>attempt(async()=>{this.deps.reader.persist();try{if(document.fullscreenElement)await document.exitFullscreen();else await q('#workspace').requestFullscreen();}catch{q('#focus-reading').click();this.deps.notify('浏览器未允许全屏，已使用专注阅读');}this.deps.reader.geometryChanged();});
    q('#reading-width').onchange=e=>this.deps.reader.layoutChange(()=>{const doc=this.deps.preview();if(doc){doc.body.style.maxWidth=e.target.value==='measure'?'42em':'';doc.body.style.margin='0 auto';}});
    document.addEventListener('keydown',e=>{if(e.key==='Escape'){for(const d of document.querySelectorAll('.tool-menu[open]'))d.open=false;if(q('#sidebar').classList.contains('open')){this.closeSidebar();const trigger=q('#reading-sidebar').offsetParent?q('#reading-sidebar'):q('#sidebar-toggle');trigger.focus();}}});
    document.addEventListener('click',e=>{for(const d of document.querySelectorAll('.tool-menu[open]'))if(!d.contains(e.target))d.open=false;if(matchMedia('(max-width:700px)').matches&&q('#sidebar').classList.contains('open')&&!q('#sidebar').contains(e.target)&&!e.target.closest('#reading-sidebar,#sidebar-toggle'))this.closeSidebar();});
    for(const toolbar of document.querySelectorAll('.pane-toolbar,.reading-toolbar')){toolbar.setAttribute('role','toolbar');toolbar.setAttribute('aria-label','阅读工具');toolbar.addEventListener('keydown',e=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(e.key)||['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName))return;const buttons=[...toolbar.querySelectorAll('button,summary,select')].filter(n=>n.getClientRects().length);const i=buttons.indexOf(e.target);if(i<0)return;e.preventDefault();buttons[e.key==='Home'?0:e.key==='End'?buttons.length-1:(i+(e.key==='ArrowLeft'?-1:1)+buttons.length)%buttons.length].focus();});}
  }
  async refresh(){const c=this.deps.context();if(!c.projectId||!c.versionId)return;const token=++this.summaryToken;try{const r=await this.deps.api(`/api/processor/projects/${encodeURIComponent(c.projectId)}/versions/${encodeURIComponent(c.versionId)}/issues`);if(token!==this.summaryToken||c.projectId!==this.deps.context().projectId||c.versionId!==this.deps.context().versionId)return;this.referenceGroups=[...(r.issues||[]),...(r.resolved||[])];this.referenceContext=c;this.refreshTableActions();const issues=r.issues||[];q('#issue-trigger').textContent=issues.length===1&&issues[0].kind==='table'?`表格待确认 1 处`:r.summary||`${issues.length} 处待确认`;q('#issue-trigger').dataset.count=r.issues?.length||0;}catch{if(token===this.summaryToken)q('#issue-trigger').textContent='查看检查建议';}}
  tab(name){document.body.classList.toggle('reading-active',name==='result');if(name!=='result')q('#result-layout').classList.remove('source-only');}
  toggleSidebar(){this.deps.reader.layoutChange(()=>{const narrow=matchMedia('(max-width:700px)').matches;if(narrow){document.body.classList.remove('sidebar-collapsed');q('#sidebar').classList.toggle('open');}else{q('#sidebar').classList.remove('open');document.body.classList.toggle('sidebar-collapsed');}const expanded=narrow?q('#sidebar').classList.contains('open'):!document.body.classList.contains('sidebar-collapsed');for(const id of ['reading-sidebar','sidebar-toggle'])q('#'+id).setAttribute('aria-expanded',String(expanded));});}
  closeSidebar(){q('#sidebar').classList.remove('open');for(const id of ['reading-sidebar','sidebar-toggle'])q('#'+id).setAttribute('aria-expanded','false');}
  refreshTableActions(){const doc=this.deps.preview();for(const table of doc?.querySelectorAll('table')||[]){const b=table.previousElementSibling?.querySelector('.table-view-action');if(!b)continue;const info=this.loupe.tableReference(table);b.textContent=info.precision==='table'?'查看原表':'查看原页';b.title=info.page?`第 ${info.page} 页${info.precision==='table'?'的原表区域':'完整原页'}`:'查看来源位置';const location=b.parentElement.querySelector('.table-source-location');location.textContent=info.page?`第 ${info.page} 页`: '来源位置待核对';}}
  attach(){const doc=this.deps.preview();if(!doc||this.attachedDocument===doc)return;this.attachedDocument=doc;const current=()=>doc===this.deps.preview();for(const im of doc.querySelectorAll('img:not(details img)')){im.style.cursor='zoom-in';im.tabIndex=0;im.setAttribute('role','button');im.setAttribute('aria-label',`放大查看 ${im.alt||'图片'}`);const open=()=>{if(current())this.loupe.open(im);};im.addEventListener('click',open);im.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();open();}});}for(const table of doc.querySelectorAll('table')){const tools=doc.createElement('div');tools.className='original-view-tools';tools.setAttribute('role','group');tools.setAttribute('aria-label','表格来源');const location=doc.createElement('span');location.className='table-source-location';const b=doc.createElement('button');b.type='button';b.className='table-view-action';b.onclick=()=>{if(current())this.loupe.open(table,b);};tools.append(location,b);table.parentElement.insertBefore(tools,table);}this.refreshTableActions();}
}

export class DocumentLoupe {
  constructor(deps){
    this.deps=deps;this.token=0;this.zoom=1;this.dialog=q('#image-dialog');this.image=q('#image-dialog-image');
    this.controls=el('div','loupe-tools');this.controls.setAttribute('role','toolbar');this.controls.setAttribute('aria-label','原件放大工具');
    this.controls.innerHTML='<button type="button" data-z="fit">适合窗口</button><button type="button" data-z="1">100%</button><button type="button" data-z="minus" aria-label="缩小资源">−</button><output aria-live="polite">100%</output><button type="button" data-z="plus" aria-label="放大资源">＋</button><button type="button" id="loupe-original-page">返回原页</button>';
    this.image.before(this.controls);this.stage=el('div','loupe-stage');this.image.replaceWith(this.stage);this.stage.append(this.image);
    this.htmlView=el('div','loupe-html');this.htmlView.hidden=true;this.stage.append(this.htmlView);this.status=el('p','loupe-status');this.status.setAttribute('role','status');this.stage.append(this.status);
    this.retry=el('button','loupe-retry','重新读取原件');this.retry.type='button';this.retry.hidden=true;this.controls.append(this.retry);
    this.dialog.classList.add('document-loupe');this.dialog.setAttribute('aria-labelledby','image-dialog-title');this.dialog.setAttribute('aria-describedby','image-dialog-caption');
    this.controls.onclick=e=>{const z=e.target.dataset.z;if(z){if(this.image.hidden&&this.htmlView.hidden)return;this.zoom=z==='fit'?this.fit():z==='minus'?Math.max(.1,this.zoom-.25):z==='plus'?Math.min(8,this.zoom+.25):Number(z);this.draw();}else if(e.target.id==='loupe-original-page'&&this.page){this.dialog.close();this.deps.reader.jumpPage(this.page,'compare',true);}};
    this.retry.onclick=()=>{if(this.reference)this.openReference(this.reference,this.trigger);};
    this.stage.onpointerdown=e=>{if(e.button!==0||e.target.closest('button'))return;const x=e.clientX,y=e.clientY,sl=this.stage.scrollLeft,st=this.stage.scrollTop;this.stage.setPointerCapture(e.pointerId);this.stage.onpointermove=m=>{this.stage.scrollLeft=sl-(m.clientX-x);this.stage.scrollTop=st-(m.clientY-y);};};this.stage.onpointerup=()=>this.stage.onpointermove=null;this.stage.onpointercancel=()=>this.stage.onpointermove=null;
    this.stage.onwheel=e=>{if(e.ctrlKey&&(!this.image.hidden||!this.htmlView.hidden)){e.preventDefault();this.zoom=Math.max(.1,Math.min(8,this.zoom+(e.deltaY<0?.1:-.1)));this.draw();}e.stopPropagation();};
    this.dialog.addEventListener('close',()=>{++this.token;this.abort?.abort();this.reference=null;this.htmlView.replaceChildren();this.dialog.inert=this.previousInert||false;this.image.onload=null;this.image.onerror=null;this.image.removeAttribute('src');if(this.saved&&this.ownerIdentity===this.deps.reader.identity()){const unchanged=this.background?.length&&this.background.every(({node,width,height,extent,top,left})=>node.clientWidth===width&&node.clientHeight===height&&node.scrollHeight===extent&&node.scrollTop===top&&node.scrollLeft===left);if(!unchanged)this.deps.reader.restore(this.saved);this.deps.reader.positions=unchanged?this.deps.reader.capture():this.saved;this.trigger?.focus({preventScroll:true});}});
    this.dialog.addEventListener('keydown',e=>{if((e.key==='+'||e.key==='-')&&(!this.image.hidden||!this.htmlView.hidden)){e.preventDefault();this.zoom=Math.max(.1,Math.min(8,this.zoom+(e.key==='+'?.25:-.25)));this.draw();}});
  }
  fit(){const width=this.htmlView.hidden?this.image.naturalWidth:this.htmlView.offsetWidth,height=this.htmlView.hidden?this.image.naturalHeight:this.htmlView.offsetHeight;return Math.min(1,this.stage.clientWidth/(width||1),this.stage.clientHeight/(height||1));}
  draw(){if(!this.htmlView.hidden)this.htmlView.style.zoom=String(this.zoom);else{this.image.style.width=(this.image.naturalWidth||800)*this.zoom+'px';this.image.style.maxWidth='none';this.image.style.maxHeight='none';}this.controls.querySelector('output').textContent=Math.round(this.zoom*100)+'%';}
  tableReference(target){
    const block=target.closest('[data-block-id]'),blockId=block?.dataset.blockId;
    const index=block?[...block.querySelectorAll('table')].indexOf(target):0;
    const map=this.deps.mappings().find(m=>m.block_id===blockId);
    const rep=[...(this.deps.tableReferences?.()||[]),...this.deps.representations()].find(r=>r.block_id===blockId&&(r.table_index??0)===index);
    const resources=this.deps.resources(),sourceIds=rep?.source_ids||map?.source_ids||[];
    const places=sourceIds.flatMap(id=>{const r=resources.find(r=>r.id===id);return(r?.placements||[]).map(p=>({...p,source_document:p.source_document||r?.locator?.split('/page[')[0]}));});
    const page=Number(rep?.page||places[0]?.page||map?.locator?.match(/page\[(\d+)\]/)?.[1]||map?.page||0);
    const sourceDocument=rep?.source_document||places[0]?.source_document||rep?.source_locator?.split('/page[')[0]||map?.locator?.split('/page[')[0];
    const pageImage=resources.find(r=>r.kind==='page'&&Number(r.page||r.locator?.match(/page\[(\d+)\]/)?.[1])===page&&(!sourceDocument||r.locator?.split('/page[')[0]===sourceDocument));
    const c=this.deps.context(),url=rep?.source_preview_url||(rep?.id?'/api/processor/projects/'+encodeURIComponent(c.projectId)+'/versions/'+encodeURIComponent(c.versionId)+'/issue-image/'+encodeURIComponent(rep.id):null);
    return {page,sourceDocument,url,pageURL:pageImage?.url,html:rep?.source_preview_html,precision:rep?.source_preview_html||rep?.source_preview_precision==='table'?'table':'page',label:target.caption?.textContent||rep?.title?.match(/表\s*\d+/)?.[0]||'表格',valid:()=>target.ownerDocument===this.deps.preview()};
  }
  open(target,trigger=target){
    if(target.tagName==='TABLE')return this.openReference(this.tableReference(target),trigger);
    const block=target.closest('[data-block-id]'),map=this.deps.mappings().find(m=>m.block_id===block?.dataset.blockId);
    const resource=this.deps.resources().find(r=>map?.source_ids?.includes(r.id)&&r.kind==='image');
    const composed=map?.mapping==='original_page_composition',page=Number(composed?map.page||map.locator?.match(/page\[(\d+)\]/)?.[1]:resource?.placements?.[0]?.page||map?.locator?.match(/page\[(\d+)\]/)?.[1]||map?.page||0);
    return this.openReference({page,url:composed?target.src:resource?.url||target.src,label:target.alt||resource?.label||'研究图',precision:'image',direct:true,valid:()=>target.ownerDocument===this.deps.preview()},trigger);
  }
  openResource(resource,trigger){
    return this.openReference({page:Number(resource.page||resource.locator?.match(/page\[(\d+)\]/)?.[1]||0),url:resource.url,label:resource.label||'原始资源',precision:resource.kind==='page'?'page':'image',direct:true},trigger);
  }
  openIssue(info,trigger){
    const c=this.deps.context(),page=Number(info.page||0),docName=info.source_document||info.source_locator?.split('/page[')[0];
    const pageImage=this.deps.resources().find(r=>r.kind==='page'&&Number(r.page||r.locator?.match(/page\[(\d+)\]/)?.[1])===page&&(!docName||r.locator?.split('/page[')[0]===docName));
    const url=info.source_preview_url||(info.kind==='table'?'/api/processor/projects/'+encodeURIComponent(c.projectId)+'/versions/'+encodeURIComponent(c.versionId)+'/issue-image/'+encodeURIComponent(info.id):null);
    const nativeTable=info.kind==='table'||info.source_kind==='table',native=!!info.source_preview_html;
    const label=native&&!nativeTable?{code:'原始代码',formula:'原始公式'}[info.source_kind]||'原件内容':info.title?.match(/表\s*\d+/)?.[0]||'原件资源';
    return this.openReference({page,url,pageURL:pageImage?.url,html:info.source_preview_html,label,precision:native?(nativeTable?'table':'native'):info.source_preview_precision==='table'?'table':'page',direct:info.kind!=='table',valid:()=>trigger?.isConnected},trigger);
  }
  heading(info,precision){
    const subject=precision==='table'?info.label||'原表':precision==='page'?'原页':info.label||'研究图';
    return [subject,this.deps.materialTitle?.()||q('#material-title')?.textContent,info.page?'第 '+info.page+' 页':null].filter(Boolean).join(' · ');
  }
  unavailable(message){
    this.image.hidden=true;this.status.hidden=false;this.status.textContent=message;this.status.setAttribute('role','alert');this.retry.hidden=!this.reference?.url&&!this.reference?.pageURL;
  }
  async openReference(info,trigger){
    const alreadyOpen=this.dialog.open;
    if(!alreadyOpen){this.trigger=trigger;this.saved=this.deps.reader.capture();this.background=['left','right'].map(side=>this.deps.reader.scroller?.(side)).filter(Boolean).map(node=>({node,width:node.clientWidth,height:node.clientHeight,extent:node.scrollHeight,top:node.scrollTop,left:node.scrollLeft}));this.deps.reader.positions=this.saved;if(this.deps.reader.layoutPending)this.deps.reader.layoutBookmark=this.saved;}
    this.ownerIdentity=this.deps.reader.identity();this.reference=info;this.page=Number(info.page||0);this.abort?.abort();this.abort=new AbortController();
    const token=++this.token,current=()=>token===this.token&&this.ownerIdentity===this.deps.reader.identity()&&(!info.valid||info.valid());
    this.stage.scrollTop=0;this.stage.scrollLeft=0;this.image.hidden=true;this.image.removeAttribute('src');this.htmlView.hidden=true;this.htmlView.replaceChildren();this.status.hidden=false;this.status.textContent='正在读取已保存原件…';this.status.setAttribute('role','status');this.retry.hidden=true;
    q('#loupe-original-page').disabled=!this.page;q('#image-dialog-title').textContent=this.heading(info,info.precision);q('#image-dialog-caption').textContent=info.precision==='image'?'原始资源；位图放大不会增加细节':'已保存原件；仅使用可信表格区域，否则展示完整原页';
    if(!alreadyOpen){this.previousInert=this.dialog.inert;this.dialog.inert=false;this.dialog.showModal();this.dialog.querySelector('[data-close]')?.focus();}
    if(info.html){
      // source_preview_html is the existing server-sanitized native source table,
      // not model-authored HTML. Resolve its project-local resources only.
      this.htmlView.innerHTML=info.html;const c=this.deps.context();for(const img of this.htmlView.querySelectorAll('img[src^="assets/"]'))img.src='/api/processor/projects/'+encodeURIComponent(c.projectId)+'/files/'+encodeURIComponent(img.getAttribute('src').slice(7));
      this.htmlView.style.zoom='1';this.htmlView.hidden=false;this.status.hidden=true;this.zoom=this.fit();this.draw();q('#image-dialog-title').textContent=this.heading(info,info.precision);q('#image-dialog-caption').textContent=info.precision==='table'?'已保存原生表格；保留行列与合并关系，未替代正文':'已保存原件内容；保留原有结构，未替代正文';return;
    }
    const show=(src,precision)=>{
      if(!current())return;q('#image-dialog-title').textContent=this.heading(info,precision);
      q('#image-dialog-caption').textContent=precision==='table'?'完整原表区域；未替代正文':precision==='page'?'完整原页；保留表头、列与表注，可缩放和平移':'原始资源；位图放大不会增加细节';
      if(trigger?.classList?.contains('table-view-action')){trigger.textContent=precision==='table'?'查看原表':'查看原页';}
      this.image.alt=this.heading(info,precision);
      this.image.onload=()=>{if(!current())return;this.image.hidden=false;this.status.hidden=true;this.zoom=this.fit();this.draw();};
      this.image.onerror=()=>{if(current())this.unavailable('原件图像未能读取。可返回原页查看，或重新读取同一来源文件。');};
      this.image.src=src;
    };
    if(info.direct){if(info.url)show(info.url,info.precision);else this.unavailable('当前资源没有可读取的原件地址。请在材料准备页检查原件。');return;}
    if(info.url){
      try{
        const response=await fetch(info.url,{signal:this.abort.signal});if(!response.ok)throw Error('原件预览不可用');
        const data=await response.blob();const src=await new Promise((resolve,reject)=>{const file=new FileReader();file.onload=()=>resolve(file.result);file.onerror=()=>reject(file.error);file.readAsDataURL(data);});
        if(!current())return;show(src,response.headers.get('X-SourceLoom-Region-Precision')==='table'?'table':'page');return;
      }catch(e){if(e.name==='AbortError'||!current())return;}
    }
    if(current()){if(info.pageURL)show(info.pageURL,'page');else this.unavailable('没有可读取的原表或原页预览。请返回材料准备页检查已保存原件；正文没有被修改。');}
  }
}
