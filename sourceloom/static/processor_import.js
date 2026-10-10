// Upload transport and source preparation are separate, observable stages.
export function preparationLabel(progress={}) {
  const names={not_started:'等待上传',queued:'原件已接收，等待准备',saving_original:'保存原件',parsing_file:'解析文件',pdf_pages:'准备原件页面',extracting_resources:'提取图文资源',building_index:'保存来源索引',complete:'材料准备完成',failed:'材料准备未完成'};
  const count=Number.isFinite(progress.completed)&&Number.isFinite(progress.total)&&progress.total>0?` · ${progress.completed}/${progress.total}${['pdf_pages','extracting_resources'].includes(progress.phase)?' 页':' 项'}`:'';
  return (names[progress.phase]||'准备材料')+count;
}
export async function directoryOptions(api) {
  const rows=[];let more=true;
  while(more){const page=await api('/api/processor/tree?space=mine&folders_only=true&limit=500&offset='+rows.length);rows.push(...page.nodes);more=page.more;if(more&&!page.nodes.length)throw Error('目录列表未完整返回，请重新打开导入窗口');}
  return rows.filter(row=>row.kind==='folder');
}
export function uploadFiles(path,body,onProgress) {
  return new Promise((resolve,reject)=>{
    const xhr=new XMLHttpRequest();xhr.open('POST',path);xhr.setRequestHeader('X-SourceLoom','1');xhr.timeout=180000;
    xhr.upload.onprogress=event=>onProgress({phase:'upload',sent:event.loaded,total:event.lengthComputable?event.total:0});
    xhr.upload.onload=()=>onProgress({phase:'accepting'});
    xhr.onerror=xhr.ontimeout=()=>reject(Error('上传回执未取得，先查询这份材料的进度，不要重复建立项目'));
    xhr.onload=()=>{let value;try{value=JSON.parse(xhr.responseText);}catch{reject(Error('上传未取得有效回执，请查询原材料进度'));return;}
      if(xhr.status>=200&&xhr.status<300)resolve(value);else reject(Error(value?.error||value?.detail||`上传未完成（${xhr.status}）`));};
    xhr.send(body);
  });
}
export class MaterialImport {
  constructor(deps){
    this.deps=deps;this.epoch=0;this.active=null;this.$=s=>document.querySelector(s);this.form=this.$('#import-form');
    this.form.addEventListener('submit',event=>{event.preventDefault();void this.submit();});
    this.$('#import-resume').onclick=()=>this.continue();
    this.$('#import-check').onclick=()=>this.continue(false);
    this.$('#import-open').onclick=()=>{if(this.active?.id){this.$('#import-dialog').close();void this.deps.open(this.active.id);}};
  }
  show(){
    const dialog=this.$('#import-dialog');if(this.active?.busy){if(!dialog.open)dialog.showModal();return;}
    this.active=null;this.form.reset();this.$('#import-status').hidden=true;this.$('#import-recovery').hidden=true;
    const select=this.form.elements.parent_id,parent=this.deps.parent();select.replaceChildren(new Option('我的材料（根目录）',''));select.disabled=true;this.form.querySelector('[type=submit]').disabled=true;
    if(!dialog.open)dialog.showModal();const epoch=++this.epoch;
    directoryOptions(this.deps.api).then(rows=>{
      if(epoch!==this.epoch)return;for(const row of rows)select.add(new Option([row.path,row.title].filter(Boolean).join('/'),row.id));select.value=rows.some(row=>row.id===parent)?parent:'';select.disabled=false;this.form.querySelector('[type=submit]').disabled=false;
    }).catch(error=>{if(epoch===this.epoch)this.progress({label:'目录尚未读取',detail:error.message,error:true});});
  }
  progress(value){
    const host=this.$('#import-status');host.hidden=false;host.dataset.state=value.error?'error':'running';
    this.$('#import-progress-label').textContent=value.label||preparationLabel(value);
    this.$('#import-progress-detail').textContent=value.detail||'';
    const meter=this.$('#import-progress');
    if(value.phase==='upload'&&value.total>0){meter.max=value.total;meter.value=value.sent;this.$('#import-progress-detail').textContent=`${(value.sent/1048576).toFixed(1)} / ${(value.total/1048576).toFixed(1)} MB`;}else if(Number.isFinite(value.completed)&&value.total>0){meter.max=value.total;meter.value=value.completed;}else meter.removeAttribute('value');
    meter.hidden=!!value.error||value.phase==='complete';
    if(this.active&&this.deps.owns(this.active.message))this.deps.feedback(this.$('#import-progress-label').textContent+(value.detail?' · '+value.detail:''));
  }
  busy(value){for(const control of this.form.querySelectorAll('input,textarea,select,button[type=submit]'))control.disabled=value;this.$('#import-dismiss').textContent=value?'在后台准备':'取消';}
  async wait(session){
    const deadline=performance.now()+600000;
    while(this.active===session&&performance.now()<deadline){
      const progress=await this.deps.api(`/api/processor/projects/${session.id}/progress`);
      if(this.active!==session)return false;
      this.progress({...progress,detail:(progress.detail||'')+(progress.updated&&Date.now()/1000-progress.updated>15?' · 此阶段暂未更新，可在后台继续等待':'')});
      if(progress.phase==='complete')return true;
      if(progress.phase==='failed'){session.resumable=progress.resumable;throw Error(progress.detail||'准备未完成，原件保留');}
      await new Promise(resolve=>setTimeout(resolve,800));
    }
    throw Error('材料准备尚未结束，原记录已保留；可查询进度，不必重新上传');
  }
  async finished(session){
    session.busy=false;this.busy(false);this.$('#import-recovery').hidden=true;
    if(this.deps.owns(session.message))this.deps.done(`“${session.title}”已准备完成，可以交给模型`);
    void this.deps.refresh(session.parent).catch(error=>this.deps.error(error.message));
    if(this.$('#import-dialog').open&&this.deps.viewEpoch()===session.viewEpoch){this.$('#import-dialog').close();await this.deps.open(session.id);}
  }
  failed(session,error){
    if(this.active!==session)return;session.busy=false;this.busy(false);this.progress({label:'导入尚未完成',detail:error.message,error:true});
    this.$('#import-recovery').hidden=!session.id;this.$('#import-resume').hidden=!session.resumable;
    if(this.deps.owns(session.message))this.deps.error(error.message);
    if(session.id)void this.deps.refresh(session.parent).catch(()=>{});
  }
  async submit(){
    if(this.active?.busy)return;
    // A retained record is queried, not blindly replaced after a lost receipt.
    if(this.active?.id){await this.continue(false);return;}
    const values=new FormData(this.form),files=[...this.form.elements.files.files],url=String(values.get('url')||'').trim();
    if(!files.length&&!url){this.progress({label:'请选择来源文件或网页地址',error:true});return;}
    if(files.length&&url){this.progress({label:'一次请选择文件或网页地址其中一种',error:true});return;}
    const title=String(values.get('title')),session={title,parent:values.get('parent_id')||null,busy:true,viewEpoch:this.deps.viewEpoch(),message:`正在导入“${title}”`};this.active=session;this.busy(true);this.deps.start(session.message);
    try{
      this.progress({label:'建立材料记录'});const created=await this.deps.post('/api/processor/projects',{title,preferences:values.get('preferences'),parent_id:session.parent});session.id=created.id;
      if(files.length){const form=new FormData();for(const file of files)form.append('files',file);
        await uploadFiles(`/api/processor/projects/${created.id}/upload?background=true`,form,p=>this.progress({...p,label:p.phase==='upload'?'上传原始文件':'传输完成，正在确认接收'}));
      }else{this.progress({label:'读取与解析网页'});await this.deps.post(`/api/processor/projects/${created.id}/url`,{url});}
      if(await this.wait(session))await this.finished(session);
    }catch(error){this.failed(session,error);}
  }
  async continue(resume=true){
    const session=this.active;if(!session?.id||session.busy)return;session.busy=true;this.busy(true);this.$('#import-recovery').hidden=true;this.deps.start(session.message);
    try{if(resume)await this.deps.post(`/api/processor/projects/${session.id}/upload/resume`);if(await this.wait(session))await this.finished(session);}catch(error){this.failed(session,error);}
  }
}
