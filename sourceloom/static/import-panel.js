export function bindImport({api,send,notice,refresh,open}) {
 const form=document.querySelector('#import-form'),dialog=document.querySelector('#import-dialog');
 const state=document.createElement('div');state.className='import-progress';state.hidden=true;
 state.innerHTML='<progress max="100" aria-label="文件上传进度"></progress><p role="status"></p>';
 form.querySelector('.dialog-actions').before(state);
 let uploading=false;
 const update=(text,percent=null)=>{state.hidden=false;state.querySelector('p').textContent=text;const bar=state.querySelector('progress');if(percent===null)bar.removeAttribute('value');else bar.value=percent};
 function upload(path,body){return new Promise((resolve,reject)=>{
  const request=new XMLHttpRequest();request.open('POST','/api'+path);request.setRequestHeader('X-SourceLoom','1');
  request.upload.onprogress=event=>{if(event.lengthComputable)update(`正在上传文件 ${Math.round(event.loaded/event.total*100)}%，上传完成前请保持页面打开`,event.loaded/event.total*100)};
  request.upload.onload=()=>update('文件已发送，正在确认后台保存');
  request.onload=()=>{let result;try{result=JSON.parse(request.responseText)}catch{return reject(Error('服务器未返回有效确认，请刷新材料列表查看保存状态'))}if(request.status>=200&&request.status<300)resolve(result);else reject(Error(result.error||'文件保存失败'))};
  request.onerror=()=>reject(Error('连接中断，请刷新材料列表确认原件是否已保存，不要重复提交'));
  request.send(body);
 })}
 dialog.addEventListener('cancel',event=>{if(uploading)event.preventDefault()});
 form.addEventListener('reset',()=>{if(!uploading){delete form.dataset.project;delete form.dataset.accepted;delete form.dataset.requestId;state.hidden=true}});
 form.onsubmit=async event=>{
  event.preventDefault();if(uploading)return;
  const files=[...form.elements.files.files],url=form.elements.url.value.trim();
  if(!files.length&&!url){notice('请选择文件或填写网页地址');return}
  if(files.length&&url){notice('本次请选择文件或网页中的一种');return}
  uploading=true;const controls=[...form.querySelectorAll('input,textarea,select,button')];controls.forEach(x=>x.disabled=true);
  try{
   update('正在建立材料记录');
   let pid=form.dataset.project;
   if(!pid){const created=await send('/projects',{title:form.elements.title.value,goal:form.elements.goal.value,budget_cny:Number(form.elements.budget.value)});pid=created.id;form.dataset.project=pid;
    if(form.elements.folder.value)await send('/library/documents/'+pid,{library_revision:0,folder:form.elements.folder.value},'PATCH')}
   const generate=form.elements.generate.checked;
   if(!form.dataset.accepted){
    form.dataset.requestId ||= crypto.randomUUID();
    if(files.length){const body=new FormData();files.forEach(file=>body.append('files',file));await upload(`/projects/${pid}/upload?background=true&generate=${generate}&request_id=${encodeURIComponent(form.dataset.requestId)}`,body)}
    else await send('/projects/'+pid+'/url',{url,background:true,generate,request_id:form.dataset.requestId});
    form.dataset.accepted='true';
   }
   update('原件已进入后台，正在打开处理进度');await refresh(false);await open(pid);dialog.close();delete form.dataset.project;delete form.dataset.accepted;
   notice(generate?'原件已交给后台，接入后会自动改写，可以关闭页面，稍后从左侧重新打开':'原件已交给后台，接入进度会在材料页显示');
   uploading=false;form.reset();
  }catch(error){update(error.message);notice(error.message);await refresh(false).catch(()=>{})}
  finally{uploading=false;controls.forEach(x=>x.disabled=false)}
 };
}
