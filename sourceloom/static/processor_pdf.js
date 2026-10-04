import * as pdfjs from './vendor/pdfjs-6.3.289/legacy/build/pdf.mjs';

const vendor=new URL('./vendor/pdfjs-6.3.289/',import.meta.url);
pdfjs.GlobalWorkerOptions.workerSrc=new URL('legacy/build/pdf.worker.mjs',vendor).href;
for(const href of [new URL('./processor_pdf_vendor.css',import.meta.url).href,new URL('./processor_pdf.css',import.meta.url).href]){
  if(!document.querySelector(`link[href="${href}"]`)){const link=document.createElement('link');link.rel='stylesheet';link.href=href;document.head.append(link);}
}
const element=(tag,cls,text)=>{const node=document.createElement(tag);if(cls)node.className=cls;if(text)node.textContent=text;return node;};
const viewportRect=(viewport,rect)=>[...viewport.convertToViewportPoint(rect[0],rect[1]),...viewport.convertToViewportPoint(rect[2],rect[3])];
const viewportTransform=(from,to)=>{
  const [a,b,c,d,e,f]=from.transform,det=a*d-b*c;
  const [i,j,k,l,m,n]=[d/det,-b/det,-c/det,a/det,(c*f-d*e)/det,(b*e-a*f)/det];
  const [A,B,C,D,E,F]=to.transform;
  return [A*i+C*j,B*i+D*j,A*k+C*l,B*k+D*l,A*m+C*n+E,B*m+D*n+F];
};
const limit=n=>Math.max(.25,Math.min(4,Number(n)||1));
const safeURL=url=>{try{const value=new URL(url,location.href);return ['http:','https:','mailto:'].includes(value.protocol)?value.href:null;}catch{return null;}};

// The preparation and comparison panes display the same original. Retain one
// PDF.js document while either pane uses it, with at most two cached parsed
// documents (active leases are never evicted). Views release DOM and canvases.
const documents=new Map();
const documentLimit=2,documentIdleTTL=30_000;
function discardDocument(key,entry){
  if(entry.users||entry.destroyed)return;
  if(documents.get(key)===entry)documents.delete(key);
  clearTimeout(entry.timer);entry.timer=null;entry.destroyed=true;
  // onProgress belongs to a view; an idle parsed document must not retain it.
  entry.task.onProgress=null;
  try{entry.task.destroy()?.catch(()=>{});}catch{}
}
function trimDocuments(now=Date.now()){
  for(const [key,entry] of documents){
    if(!entry.users&&(entry.failed||now-entry.idleAt>=documentIdleTTL))discardDocument(key,entry);
  }
  for(const [key,entry] of documents){
    if(documents.size<=documentLimit)break;
    if(!entry.users)discardDocument(key,entry);
  }
}
function acquireDocument(key,options){
  trimDocuments();
  let entry=documents.get(key);
  if(!entry){
    entry={task:pdfjs.getDocument(options),users:0,idleAt:0,timer:null,failed:false,destroyed:false};
    documents.set(key,entry);
    entry.task.promise.catch(()=>{
      entry.failed=true;
      if(documents.get(key)===entry)documents.delete(key);
      if(!entry.users)discardDocument(key,entry);
    });
  }
  clearTimeout(entry.timer);entry.timer=null;
  documents.delete(key);documents.set(key,entry);
  entry.users++;
  trimDocuments();
  let released=false;
  return {task:entry.task,release(){
    if(released)return;released=true;
    if(--entry.users)return;
    entry.task.onProgress=null;
    if(entry.failed){discardDocument(key,entry);return;}
    entry.idleAt=Date.now();
    if(documents.get(key)===entry){documents.delete(key);documents.set(key,entry);}
    trimDocuments();
    if(!entry.destroyed){
      const timer=setTimeout(()=>{
        if(entry.timer===timer&&documents.get(key)===entry)discardDocument(key,entry);
      },documentIdleTTL);
      entry.timer=timer;
    }
  }};
}

// A display adapter, not a second reader: LinkedReader retains scroll, mapping,
// driver and bookmark ownership. PDF.js owns the PDF viewport and native text.
export class PDFDocumentView {
  constructor(container,{original,title,identity,current,beforeLayout,afterLayout,onNavigate,onState,saved={}}){
    Object.assign(this,{container,original,title,identity,current,beforeLayout,afterLayout,onNavigate,onState});
    this.mode=saved.pdfScaleMode||'width';this.scale=limit(saved.pdfScale||1);this.rotation=saved.pdfRotation||0;
    this.hand=false;this.rows=[];this.revision=0;this.destroyed=false;this.hits=[];this.hitIndex=-1;this.query='';this.indexed=0;
    this.url=`/api/processor/projects/${encodeURIComponent(identity.projectId)}/files/${original.sha256}`;
    this.scroll=()=>{clearTimeout(this.renderTimer);this.renderTimer=setTimeout(()=>this.renderVisible(),45);};
    container.addEventListener('scroll',this.scroll,{passive:true});
    this.resize=new ResizeObserver(()=>{
      if(this.destroyed||!this.doc||!container.clientWidth)return;
      if(Math.abs((this.lastWidth||0)-container.clientWidth)<1&&(this.mode!=='page'||Math.abs((this.lastHeight||0)-container.clientHeight)<1))return;
      this.lastWidth=container.clientWidth;
      if(!window.getSelection()?.isCollapsed && container.contains(window.getSelection()?.anchorNode))return;
      this.relayout();
    });this.resize.observe(container);
    this.dragStart=event=>{if(!this.hand||event.button!==0)return;event.preventDefault();this.onNavigate?.();this.drag={x:event.clientX,y:event.clientY,left:container.scrollLeft,top:container.scrollTop};container.setPointerCapture(event.pointerId);};
    this.dragMove=event=>{if(!this.drag)return;container.scrollLeft=this.drag.left+this.drag.x-event.clientX;container.scrollTop=this.drag.top+this.drag.y-event.clientY;};
    this.dragEnd=()=>{this.drag=null;};
    this.selectionBegan=()=>{this.selectionEpoch=(this.selectionEpoch||0)+1;};container.addEventListener('pointerdown',this.selectionBegan);
    container.addEventListener('pointerdown',this.dragStart);container.addEventListener('pointermove',this.dragMove);container.addEventListener('pointerup',this.dragEnd);container.addEventListener('pointercancel',this.dragEnd);
    this.ready=this.open();
  }
  valid(){return !this.destroyed&&this.current()&&this.container.isConnected;}
  emit(message){if(!this.valid())return;this.message=message||this.message;const state=this.state;this.onState?.(state);this.container.dispatchEvent(new CustomEvent('pdf-state',{bubbles:true,detail:state}));}
  get state(){return {loaded:!!this.doc,version:pdfjs.version,pages:this.rows.length,scale:this.scale,scaleMode:this.mode,rotation:this.rotation,hand:this.hand,
    query:this.query,hits:this.hits.length,hit:this.hitIndex+1,indexed:this.indexed,indexing:!!this.indexing,message:this.message,
    rendered:this.rows.filter(r=>r.canvas).map(r=>r.page),textPages:this.rows.filter(r=>r.textLayer).map(r=>r.page),identity:this.identity};}
  async open(){
    try{
      this.emit('正在读取原 PDF…');
      this.documentLease=acquireDocument(this.url+':'+this.identity.sourceDigest,{url:this.url,cMapUrl:new URL('cmaps/',vendor).href,cMapPacked:true,standardFontDataUrl:new URL('standard_fonts/',vendor).href,
        wasmUrl:new URL('wasm/',vendor).href,isEvalSupported:false,enableXfa:false,disableAutoFetch:true});
      this.task=this.documentLease.task;
      this.task.onProgress=progress=>{if(this.valid())this.emit(progress.total?`正在读取原 PDF · ${Math.round(progress.loaded/progress.total*100)}%`:'正在读取原 PDF…');};
      const doc=await this.task.promise;if(!this.valid())return;this.doc=doc;
      const pages=await Promise.all(Array.from({length:doc.numPages},(_,i)=>doc.getPage(i+1)));if(!this.valid())return;
      const stack=this.container.querySelector('.source-document')||element('div','source-document');
      if(!stack.isConnected)this.container.replaceChildren(stack);this.stack=stack;stack.classList.add('pdf-document');
      pages.forEach((pdfPage,index)=>{
        const page=index+1;let sheet=stack.querySelector(`.source-sheet[data-page="${page}"]`);
        if(!sheet){sheet=element('section','source-sheet');sheet.dataset.page=page;sheet.setAttribute('aria-label',`原件第 ${page} 页`);stack.append(sheet);}
        sheet.classList.add('pdf-sheet');const fallback=sheet.querySelector('img');if(fallback)fallback.classList.add('pdf-fallback');
        this.rows.push({page,pdfPage,sheet,fallback,text:null,textLayer:null,canvas:null,renderTask:null,renderRevision:-1});
      });
      this.outlineItems=await doc.getOutline();if(!this.valid())return;
      this.relayout();await this.renderVisible();this.emit('原 PDF 已载入 · 可选择文字');
      this.buildIndex();
    }catch(error){if(this.valid()){
      // Native PDF rendering is the primary path. Download raster previews only
      // when it actually fails, instead of decoding both representations first.
      for(const image of this.container.querySelectorAll('img[data-fallback-src]')){image.hidden=false;image.src=image.dataset.fallbackSrc;}
      this.emit(`PDF 文字视图未能载入：${error.message}。仍可使用原页预览或打开原文件。`);
    }}
  }
  setScale(value){this.mode=typeof value==='string'?value:'custom';if(typeof value!=='string')this.scale=limit(value);this.emit();if(this.doc)this.relayout();}
  rotate(){this.rotation=(this.rotation+90)%360;this.emit();if(this.doc)this.relayout();}
  setHand(value){this.hand=!!value;this.container.classList.toggle('pdf-hand',this.hand);this.emit();}
  settings(){return {pdfScaleMode:this.mode,pdfScale:this.scale,pdfRotation:this.rotation};}
  relayout(){
    if(!this.valid()||!this.doc)return;
    const width=Math.max(160,this.container.clientWidth-8),height=Math.max(120,this.container.clientHeight-12);
    const base=this.rows[0].pdfPage.getViewport({scale:1,rotation:this.rotation});
    let scale=this.scale*96/72;if(this.mode==='width')scale=width/base.width;else if(this.mode==='page')scale=Math.min(width/base.width,height/base.height);
    this.scale=scale/(96/72);this.lastWidth=this.container.clientWidth;this.lastHeight=this.container.clientHeight;
    const views=this.rows.map(row=>row.pdfPage.getViewport({scale,rotation:this.rotation}));
    if(this.rows.every((row,index)=>row.viewport&&row.viewport.transform.every((v,i)=>Math.abs(v-views[index].transform[i])<.01))){this.emit();return;}
    const selected=this.captureSelection(),selectionEpoch=this.selectionEpoch||0,saved=this.beforeLayout?.();this.revision++;
    this.stack.style.width=`${Math.max(...views.map(v=>v.width))}px`;this.stack.style.minWidth='0';
    this.rows.forEach((row,index)=>{
      row.renderTask?.cancel();row.renderTask=null;row.renderRevision=-1;row.viewport=views[index];
      row.sheet.style.width=row.viewport.width+'px';row.sheet.style.height=row.viewport.height+'px';row.sheet.style.aspectRatio='auto';
      row.sheet.style.setProperty('--total-scale-factor',row.viewport.scale);row.sheet.style.setProperty('--scale-factor',row.viewport.scale);row.sheet.style.setProperty('--user-unit','1');
      row.sheet.style.setProperty('--scale-round-x','1px');row.sheet.style.setProperty('--scale-round-y','1px');
      // Preserve the complete old frame until bitmap, text and links are ready.
      // The same viewport matrix keeps selection and annotations on the bitmap.
      if(row.frame&&row.committedViewport)row.frame.style.transform=`matrix(${viewportTransform(row.committedViewport,row.viewport).join(',')})`;
      row.pendingFrame?.textLayer?.cancel();
      if(row.fallback)row.fallback.hidden=this.rotation!==0;
    });
    this.afterLayout?.(saved);this.emit();const revision=this.revision;
    this.renderVisible().then(async()=>{
      if(!selected||!this.valid()||revision!==this.revision)return;
      for(const page of new Set([selected.anchor.page,selected.focus.page]))await this.ensureText(this.rows[page-1]);
      if(!this.valid()||revision!==this.revision||selectionEpoch!==(this.selectionEpoch||0))return;
      this.restoreSelection(selected);
    }).catch(()=>{});
  }
  captureSelection(){const selection=window.getSelection();if(!selection?.rangeCount||selection.isCollapsed)return null;
    const position=(node,offset)=>{const span=(node?.nodeType===Node.ELEMENT_NODE?node:node?.parentElement)?.closest('.textLayer span'),sheet=span?.closest('.source-sheet');if(!sheet||!this.container.contains(sheet))return null;
      const spans=[...sheet.querySelectorAll('.textLayer span')].filter(n=>n.firstChild?.nodeType===Node.TEXT_NODE);return {page:Number(sheet.dataset.page),index:spans.indexOf(span),offset:node.nodeType===Node.ELEMENT_NODE?(offset?span.textContent.length:0):offset};};
    const anchor=position(selection.anchorNode,selection.anchorOffset),focus=position(selection.focusNode,selection.focusOffset);return anchor&&focus?{anchor,focus}:null;}
  restoreSelection(selected){
    if(!selected)return;
    const nodeAt=point=>[...this.rows[point.page-1].sheet.querySelectorAll('.textLayer span')].filter(n=>n.firstChild?.nodeType===Node.TEXT_NODE)[point.index]?.firstChild;
    const anchor=nodeAt(selected.anchor),focus=nodeAt(selected.focus);
    if(anchor&&focus)window.getSelection().setBaseAndExtent(anchor,Math.min(anchor.length,selected.anchor.offset),focus,Math.min(focus.length,selected.focus.offset));
  }
  async renderVisible(){
    if(!this.valid()||!this.doc||!this.container.clientHeight)return;
    const origin=this.container.getBoundingClientRect(),height=this.container.clientHeight;
    const nearby=this.rows.filter(row=>{const box=row.sheet.getBoundingClientRect();return box.bottom>=origin.top-height&&box.top<=origin.bottom+height;});
    this.wanted=new Set(nearby);
    for(const row of this.rows){if(this.wanted.has(row))continue;
      row.renderTask?.cancel();row.renderTask=null;row.pendingFrame?.textLayer?.cancel();row.pendingFrame=null;row.pendingPromise=null;
      if(row.frame){
        const selection=window.getSelection?.(),selected=selection&&!selection.isCollapsed&&selection.containsNode(row.frame,true);
        // A long native selection may cross the prefetch window. Preserve its
        // text nodes, but release the distant bitmap allocation immediately.
        if(selected){if(row.canvas){row.canvas.remove();row.canvas.width=row.canvas.height=0;row.canvas=null;}row.frame.querySelector('.pdf-links')?.remove();row.renderRevision=-1;}
        else{row.frame.remove();row.frame=null;if(row.canvas)row.canvas.width=row.canvas.height=0;row.canvas=null;row.textLayer?.cancel();row.textLayer=null;row.renderRevision=-1;row.noText=false;}
        if(row.fallback)row.fallback.hidden=this.rotation!==0;
      }
    }
    // Current pages precede neighbours. Older scroll passes cannot resurrect
    // distant canvases after a new pass has established its viewport window.
    nearby.sort((a,b)=>{const priority=row=>{const r=row.sheet.getBoundingClientRect();return r.bottom>origin.top&&r.top<origin.bottom?0:1;};return priority(a)-priority(b);});
    for(const row of nearby){if(!this.valid())return;if(this.wanted.has(row))await this.renderPage(row);}
  }
  async renderPage(row){
    if(!this.valid()||!row.viewport)return;if(row.renderRevision===this.revision&&row.canvas)return;
    if(row.pendingRevision===this.revision&&row.pendingPromise)return row.pendingPromise;
    row.renderTask?.cancel();row.pendingFrame?.textLayer?.cancel();
    const revision=this.revision,viewport=row.viewport,frame=element('div','pdf-frame'),canvas=element('canvas','pdf-canvas');
    Object.assign(frame.style,{width:viewport.width+'px',height:viewport.height+'px'});
    frame.style.setProperty('--total-scale-factor',viewport.scale);frame.style.setProperty('--scale-factor',viewport.scale);
    const ratio=Math.min(window.devicePixelRatio||1,2,Math.sqrt(12_000_000/(viewport.width*viewport.height)));
    canvas.width=Math.ceil(viewport.width*ratio);canvas.height=Math.ceil(viewport.height*ratio);canvas.style.width=viewport.width+'px';canvas.style.height=viewport.height+'px';frame.append(canvas);
    const pending={frame,canvas,textLayer:null};row.pendingFrame=pending;row.pendingRevision=revision;
    const renderTask=row.pdfPage.render({canvasContext:canvas.getContext('2d'),viewport,transform:ratio===1?null:[ratio,0,0,ratio,0,0],annotationMode:pdfjs.AnnotationMode.ENABLE});row.renderTask=renderTask;
    const current=()=>this.valid()&&revision===this.revision&&row.pendingFrame===pending;
    const promise=(async()=>{
      try{
        await renderTask.promise;if(!current())return;
        if(!row.text)row.text=await row.pdfPage.getTextContent();if(!current())return;
        if(row.text.items.some(item=>item.str?.trim())){
          const container=element('div','textLayer');frame.append(container);
          pending.textLayer=new pdfjs.TextLayer({textContentSource:row.text,container,viewport});
          await pending.textLayer.render();container.dataset.page=String(row.page);
        }else frame.append(element('span','pdf-text-status','该页没有可用的原生文字层 · 可查看原页图像'));
        if(!current())return;
        frame.append(await this.renderLinks(row,viewport));if(!current())return;
        const selected=this.captureSelection(),oldFrame=row.frame,oldCanvas=row.canvas,oldText=row.textLayer;
        // Publish the correct bitmap and all interaction layers together.
        if(oldFrame)oldFrame.replaceWith(frame);else row.sheet.prepend(frame);
        Object.assign(row,{frame,canvas,textLayer:pending.textLayer,noText:!pending.textLayer,committedViewport:viewport,renderRevision:revision});
        this.restoreSelection(selected);oldText?.cancel();if(oldCanvas)oldCanvas.width=oldCanvas.height=0;
        if(row.fallback)row.fallback.hidden=true;row.sheet.querySelector('.page-load-state')?.remove();this.paintHits(row);this.emit();
      }catch(error){if(error.name!=='RenderingCancelledException'&&current()){
        if(!row.frame&&this.rotation===0&&row.fallback?.dataset.fallbackSrc){row.fallback.hidden=false;row.fallback.src=row.fallback.dataset.fallbackSrc;}
        this.emit(`第 ${row.page} 页重绘未完成，可重试缩放或打开原 PDF。`);
      }}
      finally{
        if(row.frame!==frame){pending.textLayer?.cancel();canvas.width=canvas.height=0;}
        if(row.renderTask===renderTask)row.renderTask=null;
        if(row.pendingFrame===pending){row.pendingFrame=null;row.pendingPromise=null;}
      }
    })();row.pendingPromise=promise;return promise;
  }
  async ensureText(row){if(row.textLayer||row.noText)return;return this.renderPage(row);}
  async renderLinks(row,viewport=row.viewport){
    const annotations=await row.pdfPage.getAnnotations({intent:'display'}),layer=element('div','pdf-links');
    for(const annotation of annotations){
      if(annotation.subtype!=='Link'&&!annotation.contentsObj?.str)continue;
      const rect=viewportRect(viewport,annotation.rect),left=Math.min(rect[0],rect[2]),top=Math.min(rect[1],rect[3]);
      const link=element(annotation.subtype==='Link'?'a':'span','pdf-annotation');
      Object.assign(link.style,{left:left+'px',top:top+'px',width:Math.abs(rect[2]-rect[0])+'px',height:Math.abs(rect[3]-rect[1])+'px'});
      const url=safeURL(annotation.url||annotation.unsafeUrl);
      if(url){link.href=url;link.target='_blank';link.rel='noopener noreferrer';link.title='打开原 PDF 链接';link.setAttribute('aria-label',`打开链接 ${url}`);}
      else if(annotation.dest){link.href='#';link.title='跳转到原文引用位置';link.setAttribute('aria-label','跳转到原文引用位置');link.onclick=event=>{event.preventDefault();this.jumpDestination(annotation.dest);};}
      else if(annotation.contentsObj?.str){link.title=annotation.contentsObj.str;link.tabIndex=0;link.setAttribute('aria-label',annotation.contentsObj.str);}
      else continue;layer.append(link);
    }return layer;
  }
  async buildIndex(){
    const identity=this.identity;this.indexing=true;this.emit('正在建立原件全文查找索引…');
    for(const row of this.rows){if(!this.valid()||identity!==this.identity)return;if(!row.text)row.text=await row.pdfPage.getTextContent();row.searchText=row.text.items.filter(i=>typeof i.str==='string').map(i=>i.str).join(' ');this.indexed++;this.emit();}
    if(this.valid()){this.indexing=false;this.emit('原件全文查找已就绪');}
  }
  async search(query,delta=1){
    const searchToken=this.searchToken=(this.searchToken||0)+1;
    await this.ready;if(!this.valid()||!this.doc||searchToken!==this.searchToken)return;query=String(query||'').trim();
    if(query!==this.query){this.query=query;this.hitIndex=-1;this.hits=[];
      for(const row of this.rows){if(!row.text)row.text=await row.pdfPage.getTextContent();if(!this.valid()||searchToken!==this.searchToken)return;
        let full='',segments=[];for(const [index,item] of row.text.items.entries()){if(!item.str)continue;segments.push({item:index,start:full.length,end:full.length+item.str.length});full+=item.str+' ';}
        const normalized=full.toLocaleLowerCase();let from=0,at;
        while(query&&(at=normalized.indexOf(query.toLocaleLowerCase(),from))>=0){const end=at+query.length;
          const pieces=segments.filter(s=>s.start<end&&s.end>at).map(s=>({item:s.item,start:Math.max(0,at-s.start),end:Math.min(s.end,end)-s.start}));
          this.hits.push({page:row.page,pieces});from=end;}
      }
    }
    if(!this.valid()||searchToken!==this.searchToken)return;if(!this.hits.length){this.rows.forEach(r=>this.paintHits(r));this.emit(query?'原件中没有找到该文字':'');return;}
    this.hitIndex=this.hitIndex<0?(delta<0?this.hits.length-1:0):(this.hitIndex+delta+this.hits.length)%this.hits.length;const hit=this.hits[this.hitIndex],row=this.rows[hit.page-1];
    this.onNavigate?.();await this.renderPage(row);if(!this.valid()||searchToken!==this.searchToken)return;this.rows.forEach(r=>this.paintHits(r));
    const target=row.sheet.querySelector('.pdf-search-hit-current');
    const point=target?target.getBoundingClientRect().top-this.container.getBoundingClientRect().top+this.container.scrollTop:row.sheet.offsetTop;
    this.container.scrollTop=Math.max(0,point-this.container.clientHeight*.25);this.onNavigate?.('after');this.emit(`${this.hitIndex+1} / ${this.hits.length} 处`);
  }
  paintHits(row){
    row.sheet.querySelector('.pdf-search-highlights')?.remove();if(!row.textLayer)return;
    const nodes=[...row.sheet.querySelectorAll('.textLayer span')].filter(n=>n.firstChild?.nodeType===Node.TEXT_NODE),overlay=element('div','pdf-search-highlights');
    const box=row.sheet.getBoundingClientRect();
    for(const [index,hit] of this.hits.entries()){if(hit.page!==row.page)continue;
      // TextLayer creates one span for each non-empty text item (EOL is a br).
      for(const piece of hit.pieces){const item=row.text.items[piece.item];const prior=row.text.items.slice(0,piece.item).filter(i=>i.str).length,node=nodes[prior];if(!node||node.textContent!==item.str)continue;
        const range=document.createRange();range.setStart(node.firstChild,piece.start);range.setEnd(node.firstChild,piece.end);
        for(const rect of range.getClientRects()){const mark=element('span','pdf-search-hit'+(index===this.hitIndex?' pdf-search-hit-current':''));Object.assign(mark.style,{left:rect.left-box.left+'px',top:rect.top-box.top+'px',width:rect.width+'px',height:rect.height+'px'});overlay.append(mark);}
      }
    }(row.frame||row.sheet).append(overlay);
  }
  async copy(withCitation=false){
    const selection=window.getSelection();if(!selection?.rangeCount||selection.isCollapsed||!this.container.contains(selection.anchorNode)||!this.container.contains(selection.focusNode))throw new Error('请先在原件中选择需要引用的文字');
    const text=selection.toString().trim();if(!text)throw new Error('选区没有可复制文字');
    const first=selection.anchorNode.parentElement.closest('.source-sheet'),last=selection.focusNode.parentElement.closest('.source-sheet');
    const pages=[Number(first?.dataset.page),Number(last?.dataset.page)].sort((a,b)=>a-b);
    const citation=pages[0]===pages[1]?`第 ${pages[0]} 页`:`第 ${pages[0]}–${pages[1]} 页`;
    const value=withCitation?`${text}\n\n—— ${this.title||this.original.name}，${citation}`:text;
    if(!navigator.clipboard?.writeText)throw new Error('浏览器不允许直接写入剪贴板，请使用 Ctrl+C 复制选区');
    await navigator.clipboard.writeText(value);this.emit(withCitation?'已复制引文与出处':'已复制原文');return value;
  }
  async outline(){await this.ready;return this.valid()?this.outlineItems||[]:[];}
  async jumpDestination(destination){
    await this.ready;if(!this.valid())return;let dest=typeof destination==='string'?await this.doc.getDestination(destination):destination;if(!dest)return;
    const number=typeof dest[0]==='number'?dest[0]+1:(await this.doc.getPageIndex(dest[0]))+1,row=this.rows[number-1];if(!row)return;
    const kind=dest[1]?.name;
    const x=['XYZ','FitV','FitBV','FitR'].includes(kind)?(dest[2]??0):0;
    const y=['FitH','FitBH'].includes(kind)?(dest[2]??row.pdfPage.view[3]):kind==='FitR'?(dest[5]??row.pdfPage.view[3]):kind==='XYZ'?(dest[3]??row.pdfPage.view[3]):row.pdfPage.view[3];
    const point=row.viewport.convertToViewportPoint(x,y);this.onNavigate?.();
    this.container.scrollTop=Math.max(0,row.sheet.offsetTop+point[1]-this.container.clientHeight*.25);this.onNavigate?.('after');this.renderVisible();
  }
  originalURL(download=false){return this.url+(download?'?download=true':'');}
  async renderRegion(page,bbox,scale=2){
    if(typeof page==='object'){const placement=page;
      const sourceDocument=placement.source_document??placement.document;
      const sourceDigest=placement.source_sha256??placement.document_sha256;
      if((sourceDocument&&sourceDocument!==this.original.name&&sourceDocument!==this.original.sha256)||(sourceDigest&&sourceDigest!==this.original.sha256))
        throw new Error('该区域属于另一份原件，请参阅对应原文件');
      scale=typeof bbox==='number'?bbox:2;bbox=placement.bbox;page=placement.page;}
    await this.ready;if(!this.valid())throw new Error('原件已切换');const row=this.rows[page-1];if(!row)throw new Error('原页不存在');
    const viewport=row.pdfPage.getViewport({scale,rotation:0}),canvas=document.createElement('canvas');canvas.width=Math.ceil(viewport.width);canvas.height=Math.ceil(viewport.height);
    await row.pdfPage.render({canvasContext:canvas.getContext('2d'),viewport}).promise;
    if(!bbox)return canvas;const cut=document.createElement('canvas');cut.width=Math.ceil((bbox[2]-bbox[0])*scale);cut.height=Math.ceil((bbox[3]-bbox[1])*scale);
    cut.getContext('2d').drawImage(canvas,bbox[0]*scale,bbox[1]*scale,cut.width,cut.height,0,0,cut.width,cut.height);canvas.width=canvas.height=0;return cut;
  }
  goToRegion(placement){const row=this.rows[placement.page-1];if(!row)return;const rect=viewportRect(row.viewport,[placement.bbox[0],row.pdfPage.view[3]-placement.bbox[3],placement.bbox[2],row.pdfPage.view[3]-placement.bbox[1]]);
    this.onNavigate?.();this.container.scrollTop=Math.max(0,row.sheet.offsetTop+Math.min(rect[1],rect[3])-this.container.clientHeight*.25);this.onNavigate?.('after');this.renderVisible();}
  destroy(){this.destroyed=true;clearTimeout(this.renderTimer);this.resize.disconnect();this.container.removeEventListener('scroll',this.scroll);
    this.container.removeEventListener('pointerdown',this.selectionBegan);
    this.container.removeEventListener('pointerdown',this.dragStart);this.container.removeEventListener('pointermove',this.dragMove);this.container.removeEventListener('pointerup',this.dragEnd);this.container.removeEventListener('pointercancel',this.dragEnd);
    this.rows.forEach(row=>{row.renderTask?.cancel();row.pendingFrame?.textLayer?.cancel();row.textLayer?.cancel();if(row.canvas)row.canvas.width=row.canvas.height=0;});this.documentLease?.release();this.documentLease=null;}
}
