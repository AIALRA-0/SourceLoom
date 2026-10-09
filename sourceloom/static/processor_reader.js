import {clamp, locate, mapPoint} from './reader_geometry.js';
import {PDFDocumentView} from './processor_pdf.js?v=long-chinese-reader-20261001';

const $=id=>document.getElementById(id);
const pageOf=r=>Number(r?.page || String(r?.locator||'').match(/page\[(\d+)\]/i)?.[1] || 0);
const make=(tag,text,cls)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n;};
const editable=target=>target?.closest?.('input,textarea,select,[contenteditable=true]');

// The same controller owns page navigation, continuous scroll and local bookmarks.
// No writer/source records are changed; all reader metadata is browser-local.
export class LinkedReader {
  constructor(deps) {
    this.deps=deps;this.token=0;this.active=false;this.mode='unlocked';this.driver='right';
    this.pages=[];this.blocks=[];this.anchors=[];this.program={};this.cleanups=[];
    this.trace=[];this.saved=null;this.key='';this.layoutTimer=null;this.scrollFrame=null;
    this.zoom=1;this.mapVersion='local-content-anchors/1';this.pdfViews=new Map();
    for(const id of ['source-viewer','compare-source-viewer']) {
      const viewer=$(id);viewer.tabIndex=0;viewer.setAttribute('aria-label','连续原件阅读');
      this.bindInput(viewer,'left');
      viewer.addEventListener('scroll',()=>{
        if(id==='source-viewer')this.updatePage(viewer);
        else this.scrolled('left');
      },{passive:true});
    }
    this.bindInput($('result-markdown'),'left');
    $('result-markdown').addEventListener('scroll',()=>this.scrolled('left'),{passive:true});
    $('reader-unlock').addEventListener('click',()=>this.setMode('unlocked'));
    $('reader-link').addEventListener('click',()=>this.setMode('linked'));
    $('reader-independent').addEventListener('click',()=>{
      if(!this.independent)return;
      this.setMode('unlocked');this.restore(this.independent);this.persist();
    });
    $('reader-pair').addEventListener('click',()=>{
      if(!this.active)return;
      const pair={left:this.bookmark('left'),right:this.bookmark('right')};
      this.pairs.push(pair);this.refreshGeometry();this.persist();
      this.status('已配对当前两处 · 仅本机');
    });
    $('reader-toc').addEventListener('change',event=>{
      const node=this.doc?.querySelector(`[data-reader-heading="${event.target.value}"]`);
      if(node)this.jumpRight(node);
    });
    const search=()=>this.searchDraft($('reader-search').value);
    $('reader-search-next').addEventListener('click',search);
    $('reader-search').addEventListener('keydown',event=>{if(event.key==='Enter'){event.preventDefault();search();}});
    window.addEventListener('pagehide',()=>this.persist());
    document.addEventListener('visibilitychange',()=>{if(document.hidden)this.persist();});
    window.addEventListener('resize',()=>this.geometryChanged());
    window.__sourceLoomReader={state:()=>this.inspect(),pdfView:which=>this.pdfView(which)};
  }
  identity() {
    const c=this.deps.context();
    return [c.projectId,c.sourceDigest,c.versionId,c.draftDigest,'source','preview'].join(':');
  }
  detach({preserveSource=false}={}) {
    this.persist();this.active=false;++this.token;
    this.previewImages?.disconnect();this.previewImages=null;
    if(!preserveSource){for(const view of this.pdfViews.values())view.destroy();this.pdfViews.clear();}
    cancelAnimationFrame(this.scrollFrame);clearTimeout(this.layoutTimer);
    this.observer?.disconnect();for(const off of this.cleanups)off();this.cleanups=[];
    this.program={};this.saved=null;this.positions=null;this.layoutPending=false;this.layoutBookmark=null;this.attachedIdentity=null;
    // A stale iframe must never remain visible while a new material is loading.
    $('result-preview').hidden=true;
  }
  prepare() {
    this.key='sourceloom-reader:'+this.identity();this.pairs=[];this.independent=null;
    let saved=null;try{saved=JSON.parse(localStorage.getItem(this.key)||'null');}catch{}
    if($('reading-width'))$('reading-width').value=saved?.settings?.width||'full';
    this.previewScale=clamp(Number(saved?.settings?.previewScale)||1,.5,2);
    this.renderPreviewScale();
    this.saved=saved;this.mode=saved?.mode||'unlocked';this.driver=saved?.driver||'right';
    this.pdfSettings=saved?.settings?{pdfScaleMode:saved.settings.pdfScaleMode,pdfScale:saved.settings.pdfScale,pdfRotation:saved.settings.pdfRotation}:{};
    this.positions=saved?{left:saved.left,right:saved.right}:null;
    this.leftBookmarks=saved?.leftBookmarks||{};
    this.pairs=saved?.pairs||[];this.independent=saved?.independent||null;
    if(saved?.settings) {
      $('reading-size').value=saved.settings.size;$('reading-line-height').value=saved.settings.line;
      $('compare-left').value=saved.settings.leftKind||'source';
      $('compare-source').hidden=$('compare-left').value!=='source';
      document.querySelector('.editor-pane').hidden=$('compare-left').value!=='editor';
      const focus=saved.settings.focus||'both';document.body.dataset.readerFocus=focus;
      $('result-layout').classList.toggle('source-only',focus==='source');$('result-layout').classList.toggle('preview-only',focus==='draft');
      this.setShare(saved.settings.share||38);this.zoom=saved.settings.zoom||1;this.deps.sourceZoomChanged?.(this.zoom);
      for(const id of ['source-viewer','compare-source-viewer'])this.setSourceZoom($(id),this.zoom);
    }
    const context=this.deps.context();
    for(const id of ['source-viewer','compare-source-viewer'])this.renderSource($(id),context.resources,`${context.projectId}:${context.sourceDigest}`,this.zoom);
    this.renderMode();
  }
  renderSource(viewer, resources, identity, zoom) {
    // A hidden pane retains its adapter, but should not download and index the
    // original before the reader actually opens that pane.
    if(viewer.closest('[hidden]'))return;
    const context=this.deps.context(),original=context.originals?.find(r=>/\.pdf$/i.test(r.name)||r.mime==='application/pdf');
    const rows=resources.filter(r=>r.kind==='page'&&pageOf(r)).sort((a,b)=>pageOf(a)-pageOf(b));
    const key=identity+':'+rows.map(r=>r.sha256).join(',');
    if(viewer.dataset.sourceKey===key&&this.pdfViews.has(viewer.id)){return;}
    this.pdfViews.get(viewer.id)?.destroy();this.pdfViews.delete(viewer.id);
    viewer.dataset.sourceKey=key;viewer.replaceChildren();
    if(!rows.length) {
      viewer.append(make('pre',this.deps.context().sourceText||'此原件没有分页预览，可在原件与资源页打开原始文件。','viewer-placeholder'));
      return;
    }
    const stack=make('div',null,'source-document');viewer.append(stack);
    for(const r of rows) {
      const p=pageOf(r),sheet=make('section',null,'source-sheet');sheet.dataset.page=p;
      sheet.dataset.resourceId=r.id;sheet.dataset.sha256=r.sha256;
      sheet.setAttribute('aria-label',`原件第 ${p} 页`);
      sheet.style.aspectRatio=`${r.width||612} / ${r.height||792}`;
      const img=make('img');img.alt=`第 ${p} 页原图`;img.loading='lazy';img.decoding='async';
      if(r.width&&r.height){img.width=r.width;img.height=r.height;}
      const msg=make('div',`正在载入第 ${p} 页…`,'page-load-state');
      sheet.append(img,msg);stack.append(sheet);
      const url=r.url||`/api/processor/projects/${encodeURIComponent(this.deps.context().projectId)}/files/${r.sha256}`;
      const identityNow=key;
      const current=()=>viewer.dataset.sourceKey===identityNow && img.isConnected;
      img.onload=()=>{if(current()){msg.remove();sheet.dataset.loaded='true';}};
      img.onerror=()=>{if(!current())return;msg.replaceChildren(make('p',`第 ${p} 页载入失败，其他页仍可阅读。`));
        const retry=make('button','重试本页');retry.type='button';retry.onclick=()=>{if(current())img.src=url;};msg.append(retry);};
      if(r.available===false) {msg.textContent=`第 ${p} 页资源不可用，请回查原始文件。`;img.hidden=true;}
      else {try{const valid=new URL(url,location.origin);if(valid.origin===location.origin){if(original){img.dataset.fallbackSrc=valid.href;img.hidden=true;}else img.src=valid.href;}}catch{msg.textContent='原页地址无效';}}
    }
    this.setSourceZoom(viewer,zoom);this.updatePage(viewer);
    if(original){
      const view=new PDFDocumentView(viewer,{original,title:context.title,identity:{projectId:context.projectId,sourceDigest:context.sourceDigest},
        current:()=>viewer.dataset.sourceKey===key&&this.deps.context().projectId===context.projectId,
        saved:this.saved?.settings||this.pdfSettings||{},
        beforeLayout:()=>viewer.id==='compare-source-viewer'?(this.positions||this.capture()):null,
        afterLayout:saved=>{if(this.active&&viewer.id==='compare-source-viewer'){this.refreshGeometry();if(saved)this.restore(saved);this.positions=this.capture();this.persist();}this.updatePage(viewer);},
        onNavigate:phase=>{if(phase==='after'){if(this.mode==='linked')this.follow('left');this.updatePage(viewer);this.positions=this.capture();this.persist();}else this.interact('left');},
        onState:state=>{if(viewer.id==='compare-source-viewer'){const next={pdfScaleMode:state.scaleMode,pdfScale:state.scale,pdfRotation:state.rotation};const changed=JSON.stringify(next)!==JSON.stringify(this.pdfSettings);this.pdfSettings=next;this.zoom=state.scale;this.deps.sourceZoomChanged?.(state.scale);if(changed)this.persist();}}
      });this.pdfViews.set(viewer.id,view);
    }
  }
  pdfView(which='compare'){return this.pdfViews.get(which==='material'?'source-viewer':'compare-source-viewer');}
  setSourceZoom(viewer,zoom) {
    const pdf=this.pdfViews.get(viewer.id);if(pdf){pdf.setScale(zoom);return;}
    this.zoom=zoom;const stack=viewer.querySelector('.source-document');
    if(stack)stack.style.width=`${Math.round(zoom*100)}%`;
    $('source-zoom-label').textContent=`${Math.round(zoom*100)}%`;
  }
  attachPreview() {
    const doc=this.deps.preview(),c=this.deps.context();
    if(!doc||!c.projectId||!c.versionId)return;
    const expected=`/projects/${encodeURIComponent(c.projectId)}/preview`;
    if(!doc.location.pathname.includes(expected)||new URL(doc.location.href).searchParams.get('version')!==c.versionId)return;
    if(this.active&&this.doc===doc&&this.attachedIdentity===this.identity())return;
    this.observer?.disconnect();this.previewImages?.disconnect();for(const off of this.cleanups)off();this.cleanups=[];
    this.attachedIdentity=this.identity();
    this.doc=doc;$('result-preview').hidden=false;this.active=true;const token=++this.token;
    this.applyTypography();
    for(const table of doc.querySelectorAll('table')) {
      if(table.parentElement.classList.contains('table-scroll'))continue;
      const wrap=doc.createElement('div');wrap.className='table-scroll';wrap.tabIndex=0;
      wrap.setAttribute('role','region');wrap.setAttribute('aria-label','表格横向滚动');table.replaceWith(wrap);wrap.append(table);
    }
    let index=0;const toc=$('reader-toc');toc.replaceChildren(make('option','选择章节'));
    for(const h of doc.querySelectorAll('h1,h2,h3,h4')) {
      if(h.closest('details'))continue;h.dataset.readerHeading=String(++index);
      const option=make('option',h.textContent);option.value=index;toc.append(option);
    }
    const listen=(target,event,fn,options)=>{target.addEventListener(event,fn,options);this.cleanups.push(()=>target.removeEventListener(event,fn,options));};
    this.attachPreviewImages(doc,token,listen);
    this.bindInput(doc,'right',listen);
    listen(doc.defaultView,'scroll',()=>{if(token===this.token)this.scrolled('right');},{passive:true});
    listen(doc,'click',event=>{
      const a=event.target.closest?.('a[href^="#"]');
      if(a){const target=doc.getElementById(decodeURIComponent(a.getAttribute('href').slice(1)));if(target){event.preventDefault();this.jumpRight(target);}}
    });
    listen(doc,'toggle',()=>this.geometryChanged(),true);
    listen(doc,'load',()=>this.geometryChanged(),true);
    this.refreshGeometry();
    this.observer=new ResizeObserver(()=>{if(token===this.token)this.geometryChanged();});
    for(const target of [doc.body,$('compare-source-viewer'),$('result-preview')])this.observer.observe(target);
    if(this.saved){this.restore(this.saved);this.saved=null;}
    this.positions=this.capture();this.persist();
    $('result-preview').hidden=false;
  }
  attachPreviewImages(doc,token,listen) {
    const projectId=this.deps.context().projectId;
    const current=()=>this.active&&token===this.token&&this.doc===doc&&this.deps.preview()===doc&&this.attachedIdentity===this.identity();
    const folded=img=>img.closest('details:not([open])');
    const observer=new IntersectionObserver(entries=>{
      if(!current())return;
      for(const entry of entries){const img=entry.target;
        if(!entry.isIntersecting||folded(img))continue;
        const source=img.getAttribute('data-reader-src');if(!source){observer.unobserve(img);continue;}
        let url;try{url=new URL(source,doc.baseURI);}catch{continue;}
        if(url.origin!==location.origin||!url.pathname.startsWith(`/api/processor/projects/${encodeURIComponent(projectId)}/files/`))continue;
        // The sandboxed preview cannot execute scripts: native lazy loading is
        // deliberately not its loading authority. The existing trusted reader
        // hydrates nearby images without adding scripts to the source document.
        img.src=url.href;img.removeAttribute('data-reader-src');observer.unobserve(img);
      }
    },{root:doc,rootMargin:'50% 0px'});
    this.previewImages=observer;
    for(const img of doc.querySelectorAll('img[data-reader-src]'))if(!folded(img))observer.observe(img);
    listen(doc,'toggle',event=>{
      if(!current()||!event.target.open)return;
      for(const img of event.target.querySelectorAll('img[data-reader-src]'))if(!folded(img))observer.observe(img);
    },true);
  }
  bindInput(target,side,listen=(t,e,f,o)=>t.addEventListener(e,f,o)) {
    const claim=event=>{
      if(event.type==='wheel' && (Math.abs(event.deltaX)>Math.abs(event.deltaY)||event.ctrlKey))return;
      if(event.type==='keydown' && (!['PageDown','PageUp','ArrowDown','ArrowUp','Home','End',' '].includes(event.key)&&!((event.ctrlKey||event.metaKey)&&['f','g'].includes(event.key.toLowerCase()))||editable(event.target)))return;
      if(editable(event.target)&&event.type!=='wheel')return;
      this.interact(side);
    };
    for(const event of ['wheel','pointerdown','touchstart','keydown'])listen(target,event,claim,{passive:true});
  }
  interact(side) {
    if(!this.active)return;
    // A real input takes over the current viewport. A delayed image/layout
    // bookmark must not pull the user back to where that resize began.
    if(this.layoutPending){clearTimeout(this.layoutTimer);this.layoutPending=false;this.layoutBookmark=null;this.refreshGeometry();this.positions=this.capture();}
    cancelAnimationFrame(this.scrollFrame);this.program={};this.driver=side;
    this.trace.push({event:'user_control',side,at:performance.now()});if(this.trace.length>500)this.trace.shift();
  }
  scroller(side) {return side==='right'?this.doc?.scrollingElement:$('compare-left').value==='editor'?$('result-markdown'):$('compare-source-viewer');}
  point(side) {const sc=this.scroller(side);if(!this.visible(side))return this.bookmarkPoint(side,this.positions?.[side])||0;return sc?sc.scrollTop+sc.clientHeight*.25:0;}
  setPoint(side,point) {
    const sc=this.scroller(side);if(!sc)return;
    if(!this.visible(side)) {
      const row=locate(side==='left'?this.pages:this.blocks,point);
      if(row){this.positions ||= {};this.positions[side]={kind:side==='left'?'page':'block',page:row.page,id:row.id,offset:clamp((point-row.top)/row.height),edge:null};}
      return;
    }
    const value=clamp(point-sc.clientHeight*.25,0,Math.max(0,sc.scrollHeight-sc.clientHeight));
    this.program[side]=value;sc.scrollTop=value;
  }
  updatePage(viewer=$('compare-source-viewer')) {
    if(!viewer.clientHeight)return;
    if(viewer===$('compare-source-viewer') && this.active && this.pages.length && this.geometrySize===this.sizeSignature()) {
      const row=viewer.scrollTop>=viewer.scrollHeight-viewer.clientHeight-2 ? this.pages.at(-1) :
        locate(this.pages,viewer.scrollTop+viewer.clientHeight*.25+.5);
      this.deps.pageChanged(row.page,this.pages.length);return;
    }
    const sheets=[...viewer.querySelectorAll('.source-sheet')];if(!sheets.length)return;
    const point=viewer.scrollTop+viewer.clientHeight*.25;
    const origin=viewer.getBoundingClientRect().top;
    const row=viewer.scrollTop>=viewer.scrollHeight-viewer.clientHeight-2 ? sheets.at(-1) :
      sheets.findLast(n=>n.getBoundingClientRect().top-origin+viewer.scrollTop<=point+.5)||sheets[0];
    this.deps.pageChanged(Number(row.dataset.page),sheets.length);
  }
  jumpPage(page,which='compare',user=true) {
    const viewer=$(which==='material'?'source-viewer':'compare-source-viewer');
    const rows=[...viewer.querySelectorAll('.source-sheet')];if(!rows.length)return;
    const target=rows.find(n=>Number(n.dataset.page)===clamp(Number(page)||1,1,rows.length));
    if(!target)return;
    if(user)this.interact('left');
    const top=target.getBoundingClientRect().top-viewer.getBoundingClientRect().top+viewer.scrollTop;
    viewer.scrollTop=Math.max(0,top-viewer.clientHeight*.25+.5);
    this.updatePage(viewer);
    if(which==='compare'&&this.mode==='linked')this.follow('left');
    this.positions=this.capture();this.persist();
  }
  jumpRight(node) {
    this.interact('right');this.setPoint('right',node.getBoundingClientRect().top+this.doc.scrollingElement.scrollTop);
    if(this.mode==='linked')this.follow('right');this.positions=this.capture();this.persist();
  }
  searchDraft(text) {
    if(!this.doc||!text.trim())return;
    const query=text.trim().toLocaleLowerCase(),walker=this.doc.createTreeWalker(this.doc.body,NodeFilter.SHOW_TEXT),hits=[];
    let node;while((node=walker.nextNode())) {
      if(node.parentElement.closest('details,script,style')||!node.parentElement.getBoundingClientRect().height)continue;
      let start=0,index;while((index=node.textContent.toLocaleLowerCase().indexOf(query,start))>=0){hits.push({node,index});start=index+query.length;}
    }
    if(!hits.length){this.status('成稿中没有找到该文字');return;}
    const old=this.searchPosition;
    const at=old?.query===query?(old.index+1)%hits.length:0,hit=hits[at];this.searchPosition={query,index:at};
    const range=this.doc.createRange();range.setStart(hit.node,hit.index);range.setEnd(hit.node,hit.index+query.length);
    const selection=this.doc.defaultView.getSelection();selection.removeAllRanges();selection.addRange(range);
    this.interact('right');this.setPoint('right',range.getBoundingClientRect().top+this.doc.scrollingElement.scrollTop);
    if(this.mode==='linked')this.follow('right');this.positions=this.capture();this.persist();
  }
  scrolled(side) {
    if(!this.active)return;
    // Layout itself can emit scroll events. Only actual input (interact) may
    // replace the bookmark captured before reflow.
    if(this.layoutPending)return;
    if(this.geometrySize!==this.sizeSignature()){this.geometryChanged();return;}
    const sc=this.scroller(side);
    if(side==='left')this.updatePage();
    if(this.program[side]!=null && Math.abs(sc.scrollTop-this.program[side])<=2) {
      this.positions=this.capture();return;
    }
    if(this.driver!==side)return;
    cancelAnimationFrame(this.scrollFrame);const token=this.token;
    this.scrollFrame=requestAnimationFrame(()=>{
      if(token!==this.token||!this.active)return;
      if(this.mode==='linked')this.follow(side);
      this.positions=this.capture();this.persist();
    });
  }
  follow(side) {
    if(!this.active)return;
    const other=side==='left'?'right':'left';
    if($('compare-left').value==='editor') {
      const editor=$('result-markdown'),height=parseFloat(getComputedStyle(editor).lineHeight)||24;
      const nodes=this.blocks.filter(r=>Number(r.node.dataset.sourceStartLine)>0);
      const anchors=nodes.map(r=>({left:(Number(r.node.dataset.sourceStartLine)-1)*height,right:r.top,page:0,precision:'line'}));
      const mapped=mapPoint(anchors,this.point(side),side,this.point(other));
      if(mapped)this.setPoint(other,mapped.position);this.status('Markdown 源行对应');return;
    }
    // An isolated known object does not authorize alignment for the rest of an
    // unmapped article. Adjacent reliable anchors can bound a local gap.
    const point=this.point(side),row=locate(side==='left'?this.pages:this.blocks,point);
    const covered=side==='left'?this.anchors.some(a=>a.page===row?.page):this.anchors.some(a=>a.blockId===row?.id);
    if(!covered) {
      const sorted=this.orderedAnchors[side],before=sorted.findLast(a=>a[side]<=point),after=sorted.find(a=>a[side]>=point);
      if(!before||!after||Math.abs(before.page-after.page)>1||after[other]<before[other]) {
        this.status('待对齐 · 可在阅读定位中配对当前两处');return;
      }
    }
    const mapped=mapPoint(this.orderedAnchors[side],this.point(side),side,this.point(other),true);
    if(!mapped){this.status('待对齐 · 可在阅读定位中配对当前两处');return;}
    this.setPoint(other,mapped.position);this.updatePage();
    this.status(mapped.precision==='region'?'原件区域对应':mapped.precision==='manual'?'本机配对区间':'按页近似 · 非逐句对应');
    this.trace.push({event:'follow',side,precision:mapped.precision,blockId:mapped.blockId,at:performance.now()});
  }
  setMode(mode) {
    cancelAnimationFrame(this.scrollFrame);this.program={};
    if(mode==='linked'&&this.mode!=='linked')this.independent=this.capture();
    this.mode=mode;this.renderMode();
    if(mode==='linked')this.follow(this.driver);
    this.positions=this.capture();this.persist();
  }
  renderMode() {
    for(const [id,mode] of [['reader-unlock','unlocked'],['reader-link','linked']])$(id).setAttribute('aria-pressed',String(this.mode===mode));
    $('reader-independent').disabled=!this.independent;
    if(this.mode==='unlocked')this.status('自由阅读 · 左右独立');
  }
  status(text) {$('mapping-status').textContent=text;}
  refreshGeometry() {
    if(!this.doc)return;
    const sc=$('compare-source-viewer'),top=sc.getBoundingClientRect().top;
    if(sc.clientWidth&&sc.clientHeight)this.pages=[...sc.querySelectorAll('.source-sheet')].map(node=>({node,page:Number(node.dataset.page),top:node.getBoundingClientRect().top-top+sc.scrollTop,height:node.getBoundingClientRect().height}));
    const scroll=this.doc.scrollingElement.scrollTop;
    if(this.visible('right'))this.blocks=[...this.doc.querySelectorAll('[data-block-id]')].filter(node=>!node.closest('details') && node.getBoundingClientRect().height>0)
      .map(node=>({node,id:node.dataset.blockId,top:node.getBoundingClientRect().top+scroll,height:node.getBoundingClientRect().height}));
    const context=this.deps.context(),resources=new Map(context.resources.map(r=>[r.id,r]));
    const mappings=new Map(context.mappings.map(m=>[m.block_id,m]));this.previousAnchors=this.anchors;this.anchors=[];
    // Page images kept inside folded reference details are not article anchors.
    const main=this.blocks.filter(b=>!b.node.querySelector('details') || b.node.querySelector('p,table,h1,h2,h3'));
    const grouped=new Map();
    for(const b of main) {
      const m=mappings.get(b.id),page=pageOf(m)||m?.source_ids?.map(id=>pageOf(resources.get(id))).find(Boolean);
      if(!page)continue;
      if(!grouped.has(page))grouped.set(page,[]);grouped.get(page).push(b);
    }
    for(const [page,blocks] of grouped) {
      const left=this.pages.find(p=>p.page===page);if(!left)continue;
      const start=blocks[0].top,end=blocks.at(-1).top+blocks.at(-1).height;
      for(const b of blocks)for(const right of [b.top,b.top+b.height])this.anchors.push({left:left.top+clamp((right-start)/(end-start))*left.height,right,page,range:'page-'+page,blockId:b.id,precision:'page'});
    }
    // A specific resource occurrence maps to its saved PDF region, never its ID digits.
    const occurrences=new Map();
    for(const b of main) {
      const m=mappings.get(b.id);if(m?.mapping!=='explicit_resource')continue;
      for(const id of m.source_ids||[]) {
        const r=resources.get(id);if(r?.kind!=='image')continue;
        const ordinal=occurrences.get(id)||0;occurrences.set(id,ordinal+1);
        const placement=r.placements?.[ordinal];
        if(placement)this.addRegion(b,b.node.querySelector('img'),placement);
      }
    }
    // Rebuilt tables represent the aggregate original glyph region, not one reused checkmark.
    for(const rep of context.representations) {
      const b=this.blocks.find(b=>b.id===rep.block_id),places=rep.source_ids.flatMap(id=>resources.get(id)?.placements||[]);
      const page=places[0]?.page,same=places.filter(p=>p.page===page);
      if(!b||!same.length)continue;
      const bbox=[Math.min(...same.map(p=>p.bbox[0])),Math.min(...same.map(p=>p.bbox[1])),Math.max(...same.map(p=>p.bbox[2])),Math.max(...same.map(p=>p.bbox[3]))];
      this.addRegion(b,b.node.querySelector(rep.method==='table'?'table':'math'),{...same[0],bbox});
    }
    for(const pair of this.pairs) {
      const left=this.bookmarkPoint('left',pair.left),right=this.bookmarkPoint('right',pair.right);
      if(left!=null&&right!=null)this.anchors.push({left,right,page:pair.left.page,blockId:pair.right.id,precision:'manual'});
    }
    this.orderedAnchors={};
    for(const side of ['left','right'])this.orderedAnchors[side]=[...this.anchors].sort((a,b)=>a[side]-b[side]||a[side==='left'?'right':'left']-b[side==='left'?'right':'left']);
    this.geometrySize=this.sizeSignature();
  }
  sizeSignature() {
    const left=$('compare-source-viewer'),frame=$('result-preview');
    return [left.clientWidth,left.clientHeight,frame.clientWidth,frame.clientHeight,this.doc?.scrollingElement.scrollHeight].join(':');
  }
  addRegion(b,node,placement) {
    const p=this.pages.find(p=>p.page===placement.page);if(!p||!node)return;
    const previous=this.previousAnchors.filter(a=>a.blockId===b.id&&a.precision==='region');
    const top=this.visible('right')?node.getBoundingClientRect().top+this.doc.scrollingElement.scrollTop:previous[0]?.right;
    const height=this.visible('right')?node.getBoundingClientRect().height:previous.at(-1)?.right-top;
    if(!Number.isFinite(top)||!height)return;
    // Direct region takes precedence over page approximations inside this object.
    this.anchors=this.anchors.filter(a=>!(a.right>top&&a.right<top+height));
    const pdfRow=this.pdfView()?.rows.find(row=>row.page===placement.page);
    const region=pdfRow?.viewport?[...pdfRow.viewport.convertToViewportPoint(placement.bbox[0],placement.page_height-placement.bbox[3]),...pdfRow.viewport.convertToViewportPoint(placement.bbox[2],placement.page_height-placement.bbox[1])]:null;
    for(const fraction of [0,1])this.anchors.push({left:p.top+(region?(fraction?Math.max(region[1],region[3]):Math.min(region[1],region[3])):(placement.bbox[fraction?3:1]/placement.page_height)*p.height),
      right:top+fraction*height,page:p.page,blockId:b.id,precision:'region'});
  }
  bookmark(side) {
    const sc=this.scroller(side);if(!sc)return null;
    if(!this.visible(side))return this.positions?.[side]||null;
    const edge=sc.scrollTop<=1?'start':sc.scrollTop>=sc.scrollHeight-sc.clientHeight-1?'end':null;
    if(side==='left'&&$('compare-left').value==='editor')return {kind:'editor',line:this.point(side)/(parseFloat(getComputedStyle(sc).lineHeight)||24),edge};
    const rows=side==='left'?this.pages:this.blocks;
    const point=this.point(side),row=edge==='end'?rows.at(-1):locate(rows,point);
    if(!row)return null;
    const result={kind:side==='left'?'page':'block',page:row.page,id:row.id,offset:clamp((point-row.top)/row.height),edge};
    const pdfRow=side==='left'?this.pdfView()?.rows.find(p=>p.page===row.page):null;
    if(pdfRow?.viewport){const sheetLeft=pdfRow.sheet.getBoundingClientRect().left-sc.getBoundingClientRect().left;
      const [pdfX,pdfY]=pdfRow.viewport.convertToPdfPoint(sc.clientWidth*.5-sheetLeft,point-row.top);Object.assign(result,{pdfX,pdfY});}
    return result;
  }
  bookmarkPoint(side,b) {
    if(!b)return null;
    if(b.kind==='editor')return b.line*(parseFloat(getComputedStyle($('result-markdown')).lineHeight)||24);
    const row=(side==='left'?this.pages:this.blocks).find(r=>side==='left'?r.page===b.page:r.id===b.id);
    if(!row)return null;
    const pdfRow=side==='left'?this.pdfView()?.rows.find(p=>p.page===row.page):null;
    if(pdfRow?.viewport&&Number.isFinite(b.pdfY)&&Number.isFinite(b.pdfX))return row.top+pdfRow.viewport.convertToViewportPoint(b.pdfX,b.pdfY)[1];
    return row.top+row.height*b.offset;
  }
  capture() {return {left:this.bookmark('left'),right:this.bookmark('right')};}
  restorePosition(side,b) {
    const sc=this.scroller(side);if(!sc||!b)return;
    if(!this.visible(side))return;
    const point=this.bookmarkPoint(side,b);if(point==null)return;
    const pdfRow=side==='left'?this.pdfView()?.rows.find(p=>p.page===b.page):null;
    if(pdfRow?.viewport&&Number.isFinite(b.pdfX)&&Number.isFinite(b.pdfY)){const left=pdfRow.sheet.getBoundingClientRect().left-sc.getBoundingClientRect().left+sc.scrollLeft;
      sc.scrollLeft=Math.max(0,left+pdfRow.viewport.convertToViewportPoint(b.pdfX,b.pdfY)[0]-sc.clientWidth*.5);}
    if(b.edge){this.program[side]=b.edge==='start'?0:Math.max(0,sc.scrollHeight-sc.clientHeight);sc.scrollTop=this.program[side];}
    else this.setPoint(side,point);
  }
  restore(saved) {
    this.positions={left:saved.left,right:saved.right};
    this.restorePosition('left',saved.left);this.restorePosition('right',saved.right);
    if(this.mode==='linked')this.follow(this.driver);this.updatePage();
  }
  persist() {
    if(!this.active||!this.key)return;
    const value={...this.capture(),leftBookmarks:this.leftBookmarks,mode:this.mode,driver:this.driver,independent:this.independent,pairs:this.pairs,
      mappingVersion:this.mapVersion,identity:this.identity(),settings:{size:$('reading-size').value,line:$('reading-line-height').value,
        share:$('compare-divider').getAttribute('aria-valuenow'),zoom:this.zoom,leftKind:$('compare-left').value,...(this.pdfView()?.settings()||this.pdfSettings||{}),
        focus:document.body.dataset.readerFocus||'both',width:$('reading-width')?.value||'full',previewScale:this.previewScale||1}};
    try{localStorage.setItem(this.key,JSON.stringify(value));}catch{this.status('浏览器未允许保存位置；本次阅读仍可继续');}
  }
  applyTypography() {if(this.doc){this.doc.documentElement.style.fontSize=$('reading-size').value+'px';this.doc.documentElement.style.lineHeight=$('reading-line-height').value;this.doc.documentElement.style.zoom=String(this.previewScale||1);this.doc.body.style.maxWidth=$('reading-width')?.value==='measure'?'42em':'';this.doc.body.style.margin='0 auto';}}
  renderPreviewScale() {
    const control=$('draft-zoom');if(!control)return;
    const value=String(this.previewScale||1);if(![...control.options].some(o=>o.value===value))control.add(new Option(Math.round(Number(value)*100)+'%',value));control.value=value;
    if($('draft-zoom-minus'))$('draft-zoom-minus').disabled=Number(value)<=.5;
    if($('draft-zoom-plus'))$('draft-zoom-plus').disabled=Number(value)>=2;
    globalThis.WBSelect?.enhance(control.parentElement);
  }
  setPreviewScale(value) {this.layoutChange(()=>{this.previewScale=clamp(Number(value)||1,.5,2);this.applyTypography();this.renderPreviewScale();});}
  setShare(value) {const share=clamp(Math.round(Number(value)),25,75);$('result-layout').style.setProperty('--left-share',share+'%');$('reading-controls')?.style.setProperty('--left-share',share+'%');$('compare-divider').setAttribute('aria-valuenow',share);}
  layoutChange(change) {
    const saved={...(this.positions||this.capture())},kind=$('compare-left').value;
    change();document.body.dataset.readerFocus=$('result-layout').classList.contains('source-only')?'source':$('result-layout').classList.contains('preview-only')?'draft':'both';if(!this.active)return;
    const nextKind=$('compare-left').value;
    if(kind!==nextKind){this.leftBookmarks[kind]=saved.left;saved.left=this.leftBookmarks[nextKind]||this.bookmark('left');}
    this.refreshGeometry();this.restore(saved);this.positions=this.capture();this.persist();
    // Showing an equal-width pane may not change PDF viewport geometry. It
    // still needs its visible pages drawn after a previous hidden render pass.
    const pdf=this.pdfView();if(this.visible('left')&&pdf){pdf.relayout();void pdf.renderVisible();}
  }
  visible(side) {const node=side==='right'?$('result-preview'):this.scroller('left');return Boolean(node?.clientWidth&&node?.clientHeight);}
  geometryChanged() {
    if(!this.active)return;
    // A dimension-preserving image load must not restore an older bookmark
    // while a native keyboard or wheel scroll is still moving the viewport.
    if(!this.layoutPending&&this.geometrySize===this.sizeSignature())return;
    if(!this.layoutPending)this.layoutBookmark=this.positions||this.capture();
    this.layoutPending=true;
    clearTimeout(this.layoutTimer);cancelAnimationFrame(this.scrollFrame);const token=this.token;
    this.layoutTimer=setTimeout(()=>{if(token===this.token&&this.active)this.finishGeometry();},60);
  }
  finishGeometry() {
    clearTimeout(this.layoutTimer);
    const saved=this.layoutBookmark;this.layoutPending=false;this.layoutBookmark=null;
    this.refreshGeometry();this.restore(saved||this.capture());this.positions=this.capture();this.persist();
  }
  inspect() {return {active:this.active,identity:this.identity(),key:this.key,mode:this.mode,driver:this.driver,...this.capture(),
    pdf:this.pdfView()?.state,
    independent:this.independent,pairs:this.pairs,mappingVersion:this.mapVersion,
    pages:this.pages.map(p=>({page:p.page,top:p.top,height:p.height})),
    anchors:this.anchors,blocks:this.blocks.map(b=>({id:b.id,top:b.top,height:b.height})),trace:this.trace};}
}
