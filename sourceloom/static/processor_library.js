import {MaterialTree} from './processor_tree.js?v=return-ui-20261009';
const q=s=>document.querySelector(s);
const node=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;};
export class MaterialLibrary {
  constructor(deps){this.deps=deps;this.mode='mine';this.epoch=0;this.build();}
  build(){
    const nav=q('.library-nav')||node('nav',undefined,'library-nav');nav.setAttribute('aria-label','材料空间');
    for(const [mode,label] of [['mine','我的材料'],['examples','示例库'],['archives','已归档'],['trash','回收站']]){const b=nav.querySelector(`[data-library-mode="${mode}"]`)||node('button',label);b.type='button';b.dataset.libraryMode=mode;b.disabled=false;b.onclick=()=>this.show(mode);if(!b.parentElement)nav.append(b);}
    if(!nav.isConnected)q('#material-search').closest('label').before(nav);
    this.view=node('section',undefined,'library-view');this.view.id='library-view';this.view.hidden=true;q('#main-content').append(this.view);
    this.trial=node('button','试用副本');this.trial.id='sample-trial';this.trial.type='button';this.trial.hidden=true;q('#reading-document-bar').insertBefore(this.trial,q('#document-menu'));
    this.trial.onclick=()=>this.trySample(this.deps.current()?.id);

    this.tree=new MaterialTree({...this.deps,open:id=>this.open(id),restoreArchive:id=>this.restore(id),archiveMaterial:id=>this.archive(id),tryExample:id=>this.trySample(id),metadataChanged:r=>this.metadataChanged(r)});
    this.setNav('mine');
  }
  setNav(mode){this.mode=mode;for(const b of q('.library-nav').children)b.setAttribute('aria-current',String(b.dataset.libraryMode===mode));}
  async show(mode){
    if(this.deps.isDirty()){this.deps.notify('先保存当前编辑，再切换材料空间',true);return;}
    this.deps.persist();this.setNav(mode);const epoch=++this.epoch;
    if(mode==='mine'){await this.tree.showMode('mine');if(epoch!==this.epoch)return;this.view.hidden=true;q('#workspace').hidden=!this.deps.current();q('#empty').hidden=!!this.deps.current();this.deps.tab();return;}
    q('#workspace').hidden=true;q('#empty').hidden=true;if(this.deps.leaveReader)this.deps.leaveReader();else document.body.classList.remove('reading-active');this.view.hidden=false;
    if(mode==='trash'){await this.tree.showMode('trash');if(epoch!==this.epoch)return;this.view.replaceChildren(node('h1','回收站'),node('p','选择左侧条目，可只读查看、恢复原位置或恢复到指定目录。永久删除需要再次确认。'));return;}
    await this.tree.showMode(mode==='archives'?'archive':'examples');if(epoch!==this.epoch)return;
    this.view.replaceChildren(node('p','正在读取材料…'));
    try{const value=await this.deps.api('/api/processor/'+mode);if(epoch!==this.epoch)return;this.rows=Array.isArray(value)?value:value.examples||value.archives||value.projects||[];this.draw();}catch(e){if(epoch===this.epoch)this.view.replaceChildren(node('p',e.message));}
  }
  draw(){
    const header=node('header',undefined,'library-head');header.append(node('h1',this.mode==='examples'?'示例库':'已归档'));
    const back=node('button','返回阅读');back.type='button';back.onclick=()=>this.show('mine');header.append(back);
    const intro=node('p',this.mode==='examples'?'默认展示已有中文阅读稿；原文与处置材料可单独查看。示例只读，试用副本可独立编辑。':'原件、成稿版本和导入关系保留，随时可以恢复。','muted');
    const filters=node('div',undefined,'library-filters'),search=node('input');search.type='search';search.placeholder='搜索标题、格式或用途';search.setAttribute('aria-label','搜索此材料空间');
    const format=node('select');format.setAttribute('aria-label','筛选材料格式');for(const f of ['全部','PDF','网页','代码','DOCX'])format.add(new Option(f,f));filters.append(search,format);
    const kind=node('select');kind.setAttribute('aria-label','示例内容');if(this.mode==='examples'){kind.add(new Option('中文阅读稿','chinese_reading'));kind.add(new Option('原文与处置材料','source_baseline'));filters.prepend(kind);}
    const grid=node('div',undefined,'sample-grid');this.view.replaceChildren(header,intro,filters,grid);
    const drawCards=()=>{grid.replaceChildren();for(const r of this.rows){if(this.mode==='examples'){const chinese=r.content_kind==='chinese_reading'&&r.reading_language==='zh';if(chinese!==(kind.value==='chinese_reading'))continue;}const hay=JSON.stringify(r).toLowerCase();if(search.value&&!hay.includes(search.value.toLowerCase()))continue;const f=format.value;if(f!=='全部'&&!hay.includes(f==='网页'?'html':f==='代码'?'readme':f.toLowerCase()))continue;
      const c=node('article',undefined,'sample-card');c.dataset.sampleId=r.id;c.tabIndex=0;c.setAttribute('aria-label',r.title);if(this.mode==='examples'){const menu=e=>{e.preventDefault();c.focus();const rect=c.getBoundingClientRect();this.tree.menu({...r,kind:'example'},e.clientX||rect.left,e.clientY||rect.bottom);};c.oncontextmenu=menu;c.onkeydown=e=>{if(e.key==='F10'&&e.shiftKey)menu(e);};}const imURL=r.thumbnail_url;if(imURL){const im=node('img');im.src=imURL;im.alt=r.title+' 原件预览';im.loading='lazy';c.append(im);}c.append(node('h3',r.title));c.append(node('p',r.range||r.description||'原件与成稿版本均保留'));
      const details=node('details',undefined,'sample-details');details.append(node('summary','来源与范围'));const info=r.details;details.append(node('p',typeof info==='string'?info:info?Object.values(info).filter(x=>typeof x==='string').join(' · '):r.source_url||'已保存原件快照'));
      const supplement=r.supplement_url||r.library?.supplement_url;if(supplement){const a=node('a','打开独立表格补充页');a.href=supplement;a.onclick=e=>{e.preventDefault();this.open(new URL(a.href).searchParams.get('material'));};details.append(a);}c.append(details);
      const footer=node('footer');footer.append(node('span',r.label||'归档材料','sample-label'));const open=node('button','打开');open.type='button';open.onclick=()=>this.open(r.id);footer.append(open);
      if(this.mode==='archives'){const restore=node('button','恢复');restore.type='button';restore.onclick=()=>this.restore(r.id);footer.append(restore);}c.append(footer);grid.append(c);
    }if(!grid.children.length)grid.append(node('p','没有匹配的材料','muted'));};search.oninput=drawCards;format.onchange=drawCards;kind.onchange=drawCards;drawCards();
  }
  async open(id){const epoch=++this.epoch;this.view.hidden=true;await this.deps.open(id);if(epoch!==this.epoch)return;const current=this.deps.current();q('#workspace').hidden=!current;q('#empty').hidden=!!current;if(current)this.deps.tab();this.selected();}
  selected(){
    const p=this.deps.current(),trashed=p?.trashed===true||p?.library?.trashed===true||this.tree?.getNode(p?.id)?.trashed===true,example=p?.library?.readonly===true&&!trashed;const readonly=example||trashed;this.tree?.syncCurrent();this.trial.hidden=!example;const space=trashed?'trash':example?'examples':p?.library?.archived?'archive':'mine';if(this.view.hidden){this.setNav(space==='archive'?'archives':space);if(this.tree.mode!==space)void this.tree.showMode(space);}
    const baseline=example&&p.library.content_kind==='source_baseline';
    const paneTitle=q('.draft-toolbar .pane-title');if(paneTitle)paneTitle.textContent=baseline?'原文与处置基线':'成稿';
    q('#result-preview').title=baseline?'原文与处置基线预览':'图文成稿预览';
    q('#mobile-compare-right').textContent=baseline?'原文基线':'成稿';
    q('#toggle-editor').disabled=readonly;const selector=q('#compare-left');selector.querySelector('[value=editor]').disabled=readonly;q('#result-markdown').readOnly=readonly;if(readonly&&selector.value==='editor'){selector.value='source';selector.dispatchEvent(new Event('change',{bubbles:true}));}q('#import-markdown').disabled=readonly;q('#issue-trigger').disabled=false;q('#send-readweave').disabled=readonly||q('#send-readweave').disabled;
    let banner=q('#sample-banner');if(!banner){banner=node('div',undefined,'sample-banner');banner.id='sample-banner';q('#reading-document-bar').after(banner);}banner.hidden=!readonly;banner.textContent=trashed?'回收站材料 · 只读查看；恢复后继续编辑。':example?`${p.library.label||'只读示例'} · ${p.library.range||'完整原件与资源'} · 需要编辑时创建试用副本。`:'';
    document.body.classList.toggle('example-reading',example);
  }
  refreshTree(){return this.tree.refresh();}
  get insertionParent(){return this.tree.importParent||null;}
  async placeImported(project){const parent=this.tree.importParent;this.tree.importParent=null;if(!parent)return;await this.tree.refresh();const n=this.tree.getNode(project.id);if(!n)throw Error('新材料尚未出现在目录，请从根目录使用“移动到”');this.tree.selected=new Set([n.id]);await this.tree.perform('move',{parent_id:parent});}
  metadataChanged(response){const p=this.deps.current();if(!p)return;const nodes=response.nodes||response.items||response.changed||[];const n=Array.isArray(nodes)?nodes.find(n=>n.id===p.id):null;const fresh=n||this.tree.getNode(p.id);const affected=response.affected_ids?.includes(p.id);if(affected&&['trash','restore'].includes(response.action)){p.library=p.library||{};p.trashed=response.action==='trash';p.library.trashed=p.trashed;p.library.readonly=p.library.trashed;}const titleValue=affected&&response.action==='rename'?response.title:fresh?.title;if(titleValue){p.library=p.library||{};p.library.display_name=titleValue;const title=q('#material-title');if(title)title.textContent=titleValue;}if(fresh&&'trashed' in fresh){p.library=p.library||{};p.library.trashed=fresh.trashed;p.library.readonly=fresh.trashed||p.library.space==='examples';}this.deps.metadataChanged?.(response);this.selected();}
  async trySample(id){const b=this.trial;b.disabled=true;try{const p=await this.deps.post(`/api/processor/examples/${encodeURIComponent(id)}/trial`);await this.deps.refresh();await this.open(p.id||p.project?.id);}catch(e){this.deps.notify(e.message,true);}finally{b.disabled=false;}}
  async archive(id){if(this.deps.isDirty()){this.deps.notify('先保存编辑再归档',true);return;}const current=this.deps.current(),n=this.tree.getNode(id);if(!n||n.kind!=='material'||this.tree.mode!=='mine'||(current?.id===id&&current.library?.readonly))return;
    if(!confirm('移入可恢复归档？原件、正文和历史版本都会保留。'))return;
    try{await this.deps.post(`/api/processor/projects/${encodeURIComponent(id)}/archive`);if(current?.id===id){await this.deps.refresh();await this.show('archives');}else await this.tree.refresh();this.deps.notify('材料已移入可恢复归档');}catch(e){this.deps.notify(e.message,true);}}

  async restore(id){try{await this.deps.post(`/api/processor/projects/${encodeURIComponent(id)}/restore`);await this.deps.refresh();await this.show('archives');this.deps.notify('材料已恢复到我的材料');}catch(e){this.deps.notify(e.message,true);}}
}
