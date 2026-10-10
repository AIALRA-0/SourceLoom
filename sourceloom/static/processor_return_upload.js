export function uploadReturn(path, file, fields, onProgress) {
  return new Promise((resolve,reject)=>{
    const form=new FormData();form.append('file',file);
    for(const [key,value] of Object.entries(fields))form.append(key,value??'');
    const xhr=new XMLHttpRequest();xhr.open('POST',path);xhr.timeout=90000;
    xhr.setRequestHeader('X-SourceLoom','1');
    xhr.upload.onprogress=e=>onProgress(e.loaded,e.lengthComputable?e.total:0);
    const uncertain=()=>{const error=new Error('尚未取得保存回执，重试会查询并沿用同一次上传，不会重复保存');error.uncertain=true;reject(error);};
    xhr.onerror=xhr.ontimeout=uncertain;
    xhr.onload=()=>{
      let body;try{body=JSON.parse(xhr.responseText);}catch{uncertain();return;}
      if(xhr.status>=200&&xhr.status<300)resolve(body);
      else reject(new Error(body.error||body.detail||'上传未完成，请检查文件或登录状态'));
    };
    xhr.send(form);
  });
}

export class ReturnUpload {
  constructor(deps) {
    this.deps=deps;this.states=new Map();this.root=document.createElement('section');
    this.root.className='return-upload';this.root.setAttribute('aria-label','上传 GPT 成稿');
    this.root.innerHTML='<div class="return-upload-heading"><strong>上传 GPT 成稿</strong><span class="muted">自动保存为新版本</span></div><div class="return-dropzone"><span class="return-upload-icon" aria-hidden="true">↑</span><div><strong>将成稿文件拖到这里</strong><p class="muted">完整 .md 或 .txt 文件 · 上传后自动保存并打开预览</p></div><button type="button" class="return-file-button">选择文件</button><input type="file" accept=".md,.markdown,.txt,text/markdown,text/plain" hidden></div><div class="return-upload-state" role="status" aria-live="polite" hidden><span></span><progress max="100" hidden></progress><button type="button" hidden>重试同一次上传</button></div>';
    this.input=this.root.querySelector('input');this.button=this.root.querySelector('.return-file-button');
    this.zone=this.root.querySelector('.return-dropzone');this.status=this.root.querySelector('.return-upload-state');
    this.label=this.status.querySelector('span');this.progress=this.status.querySelector('progress');this.retry=this.status.querySelector('button');
    this.button.onclick=()=>this.input.click();this.input.onchange=()=>{const file=this.input.files[0];this.input.value='';if(file)void this.submit(file);};
    this.retry.onclick=()=>{const s=this.states.get(this.deps.context()?.id);if(s)void this.submit(s.file,s);};
    this.zone.ondragover=e=>{e.preventDefault();this.zone.classList.add('dragover');};
    this.zone.ondragleave=()=>this.zone.classList.remove('dragover');
    this.zone.ondrop=e=>{e.preventDefault();this.zone.classList.remove('dragover');if(e.dataTransfer.files.length!==1){this.showError('请一次上传一份完整成稿');return;}void this.submit(e.dataTransfer.files[0]);};
  }
  showError(message){this.status.hidden=false;this.label.textContent=message;this.progress.hidden=true;this.retry.hidden=true;}
  render() {
    const c=this.deps.context(),s=this.states.get(c?.id),busy=s?.phase==='uploading'||s?.phase==='saving';
    this.button.disabled=this.input.disabled=!c?.editable||busy;this.zone.setAttribute('aria-disabled',String(!c?.editable||busy));
    this.root.setAttribute('aria-busy',String(!!busy));this.status.hidden=!s;
    this.label.textContent=s?.message||'';this.progress.hidden=!busy;this.retry.hidden=!(s?.phase==='error'&&s.uncertain&&c?.editable);
    if(s?.phase==='uploading'&&s.total){this.progress.value=s.loaded/s.total*100;}else this.progress.removeAttribute('value');
  }
  async submit(file, previous=null) {
    const c=this.deps.context(),active=this.states.get(c?.id);
    if(!c?.editable||active&&['uploading','saving'].includes(active.phase))return;
    if(!/\.(md|markdown|txt)$/i.test(file.name)||!file.size||file.size>4*1024*1024){this.showError('请选择非空的 .md 或 .txt 成稿，大小不超过 4 MB');return;}
    if(!previous&&!this.deps.allowUpload())return;
    const s=previous||{id:c.id,version:c.version,sourceDigest:c.sourceDigest,requestId:crypto.randomUUID(),file};
    s.phase='uploading';s.message='正在上传：'+file.name;s.uncertain=false;this.states.set(c.id,s);this.render();
    try {
      const result=await this.deps.upload(s,(loaded,total)=>{s.loaded=loaded;s.total=total;s.phase=total&&loaded>=total?'saving':'uploading';s.message=s.phase==='saving'?'文件已接收，正在保存并检查图文资源':`正在上传：${file.name}${total?' · '+Math.floor(loaded/total*100)+'%':''}`;if(this.deps.context()?.id===s.id)this.render();});
      s.phase='complete';s.message=`已保存 ${file.name} · 新版本已保留原始文件`;
      if(this.deps.context()?.id===s.id){
        this.render();
        try {await this.deps.saved(result,s);}
        catch(error){s.message=`成稿已保存，预览暂未完成：${error.message}；重新打开该材料即可读取已保存版本`;if(this.deps.context()?.id===s.id)this.render();}
      }
    } catch(error){s.phase='error';s.message=error.message;s.uncertain=!!error.uncertain;if(this.deps.context()?.id===s.id)this.render();}
  }
}
