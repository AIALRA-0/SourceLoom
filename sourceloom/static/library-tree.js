const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const saved=(key,fallback)=>{try{return JSON.parse(localStorage.getItem(key))??fallback}catch{return fallback}};
const icon=folder=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true">${folder?'<path d="M3 6h7l2 2h9v12H3z"/>':'<path d="M5 3h9l5 5v13H5zM14 3v6h5M8 13h8M8 17h6"/>'}</svg>`;
export function libraryTree(host, hooks){
 let data={folders:[],documents:[]},trash=false,current=null,focus=null,selected=new Set(),expanded=new Set(saved('loom-expanded',[])),anchor=null,menuTarget=null,search='',dragged=null,renameInput=null;
 const states={queued:'等待处理',running:'后台处理中',completed:'已核对',needs_attention:'需要修复',failed:'处理停止',uncertain:'等待原请求',edited:'修改待核对'};
 const byKey=key=>key?.startsWith('f:')?data.folders.find(f=>f.id===key.slice(2)):data.documents.find(d=>d.id===key?.slice(2));
 const isFolder=key=>key?.startsWith('f:');
 const keyOf=(x,folder)=>`${folder?'f':'d'}:${x.id}`;
 const node=key=>[...host.querySelectorAll('[data-node]')].find(x=>x.dataset.node===key);
 const row=key=>node(key)?.querySelector(':scope > .tree-row');
 const save=()=>localStorage.setItem('loom-expanded',JSON.stringify([...expanded]));
 const visible=()=>[...host.querySelectorAll('[data-node]')].map(x=>x.dataset.node);
 function path(parent){const names=[],seen=new Set();while(parent&&!seen.has(parent)){seen.add(parent);const f=data.folders.find(f=>f.id===parent);if(!f)break;names.unshift(f.name);parent=f.parent}return ['我的材料',...names].join(' / ')}
 function matchesFolder(f,seen=new Set()){
  if(seen.has(f.id))return false;seen=new Set([...seen,f.id]);
  return f.name.toLowerCase().includes(search)||data.documents.some(d=>d.folder===f.id&&d.title.toLowerCase().includes(search))||data.folders.some(c=>c.parent===f.id&&matchesFolder(c,seen));
 }
 function markup(x,folder,depth,children=''){
  const key=keyOf(x,folder),name=folder?x.name:x.title,open=folder&&(expanded.has(x.id)||Boolean(search));
  const full=path(folder?x.parent:x.folder)+' / '+name;
  return `<div class="tree-node" role="treeitem" aria-level="${depth}" aria-selected="${selected.has(key)}" ${folder?`aria-expanded="${open}"`:''} tabindex="${focus===key?0:-1}" data-node="${esc(key)}"><div class="tree-row ${key==='d:'+current?'current':''} ${selected.has(key)?'selected':''}" data-row="${esc(key)}" draggable="${!trash}" title="${esc(full)}"><button class="tree-arrow" tabindex="-1" data-toggle="${esc(key)}" aria-label="${open?'收起':'展开'} ${esc(name)}">${folder?(open?'⌄':'›'):''}</button>${icon(folder)}<span class="tree-name">${esc(name)}</span>${!folder&&states[x.state]?`<span class="tree-state" title="${esc(states[x.state])}" aria-label="${esc(states[x.state])}">${x.active_job?'◷':x.state==='completed'?'✓':'·'}</span>`:''}<button class="tree-more" tabindex="-1" data-more="${esc(key)}" aria-label="${esc(name)}的更多操作">···</button></div>${folder&&open?`<div role="group" class="tree-children">${children}</div>`:''}</div>`;
 }
 function draw(){
  if(renameInput?.isConnected)return;
  const oldScroll=host.scrollTop,hadFocus=host.contains(document.activeElement);
  function branch(parent,depth=1,seen=new Set()){
   if(parent&&seen.has(parent))return '';seen=new Set([...seen,parent]);
   const folders=data.folders.filter(f=>(f.parent===parent||parent===null&&f.parent&&!data.folders.some(p=>p.id===f.parent))&&(!search||matchesFolder(f)||path(f.parent).toLowerCase().includes(search)));
   const parentMatches=search&&parent&&path(parent).toLowerCase().includes(search);
   const docs=data.documents.filter(d=>(d.folder===parent||parent===null&&(!d.folder||!data.folders.some(p=>p.id===d.folder)))&&(!search||parentMatches||d.title.toLowerCase().includes(search)));
   return folders.map(f=>markup(f,true,depth,branch(f.id,depth+1,seen))).join('')+docs.map(d=>markup(d,false,depth)).join('');
  }
  host.innerHTML=branch(null)||`<p class="hint tree-empty">${search?'没有匹配的材料或文件夹':trash?'回收站为空':'导入第一份材料后，会显示在这里'}</p>`;
  const keys=visible();if(!keys.includes(focus))focus=keys.includes('d:'+current)?'d:'+current:keys[0];
  node(focus)?.setAttribute('tabindex','0');host.scrollTop=oldScroll;if(hadFocus)node(focus)?.focus({preventScroll:true});
  hooks.selection?.(selected.size);
 }
 function targetItems(keys){return keys.map(key=>{const x=byKey(key);if(!x)throw Error('材料已经变化，请刷新');return {kind:isFolder(key)?'folder':'document',id:x.id,revision:(isFolder(key)?x.revision:x.library_revision)||0}})}
 async function act(action,keys,destination,confirm=false){
  const result=await hooks.send('/library/actions',{action,items:targetItems(keys),destination,confirm});
  if(action!=='move'){selected.clear();await hooks.clearIfMissing?.(keys)}
  await hooks.refresh();hooks.notify(result.moved_to_root?.length?'已恢复到材料库根目录，原上级文件夹不在当前材料库':({trash:'已移到回收站，处理中任务已申请取消',restore:'已恢复材料',move:'已移动',purge:'已删除所选材料及历史版本'})[action]);
 }
 function closeMenu(){document.getElementById('tree-menu')?.remove();node(menuTarget)?.focus({preventScroll:true})}
 function openMenu(key,x,y){
  closeMenu();menuTarget=key;focus=key;if(!selected.has(key))selected=new Set([key]);draw();
  const keys=[...selected],folder=isFolder(key),multi=keys.length>1;
  const actions=trash?[['restore','恢复'],['purge','彻底删除…']]:multi?[['move','移动到…'],['trash','移到回收站']]:folder?[['toggle','展开 / 收起'],['import','在此导入'],['new-folder','新建子文件夹'],['rename','重命名'],['move','移动到…'],['trash','移到回收站']]:[['open','打开'],['rename','重命名'],['move','移动到…'],['duplicate','创建副本'],['download','下载正文'],['trash','移到回收站']];
  const menu=document.createElement('div');menu.id='tree-menu';menu.className='tree-menu';menu.setAttribute('role','menu');menu.setAttribute('aria-label',multi?`已选 ${keys.length} 项`:byKey(key).name||byKey(key).title);
  menu.innerHTML=(multi?`<div class="menu-count">已选 ${keys.length} 项</div>`:'')+actions.map(([a,t])=>`<button role="menuitem" data-command="${a}">${t}${a==='rename'?'<kbd>F2</kbd>':a==='trash'?'<kbd>Delete</kbd>':''}</button>`).join('');
  document.body.append(menu);menu.style.left=Math.max(8,Math.min(x,innerWidth-menu.offsetWidth-8))+'px';menu.style.top=Math.max(8,Math.min(y,innerHeight-menu.offsetHeight-8))+'px';menu.querySelector('button').focus();
  menu.onclick=async e=>{const command=e.target.closest('[data-command]')?.dataset.command;if(!command)return;closeMenu();try{await commandAction(command,key,keys)}catch(err){hooks.notify(err.message)}};
  menu.onkeydown=e=>{const buttons=[...menu.querySelectorAll('button')],i=buttons.indexOf(document.activeElement);if(e.key==='Escape'){e.preventDefault();closeMenu()}if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();buttons[e.key==='Home'?0:e.key==='End'?buttons.length-1:(i+(e.key==='ArrowDown'?1:buttons.length-1))%buttons.length].focus()}};
 }
 function promptDialog(title,label,field,submit,description=''){
  const dlg=document.createElement('dialog');dlg.className='tree-dialog';dlg.innerHTML=`<form><h2>${esc(title)}</h2>${description?`<p>${esc(description)}</p>`:''}<label>${esc(label)}</label><div class="dialog-actions"><button type="button">取消</button><button type="submit" class="primary">确定</button></div></form>`;
  if(field)dlg.querySelector('label').append(field);const close=()=>{dlg.close();dlg.remove();node(focus)?.focus()};dlg.querySelector('[type=button]').onclick=close;dlg.oncancel=()=>dlg.remove();
  dlg.querySelector('form').onsubmit=async e=>{e.preventDefault();const b=dlg.querySelector('[type=submit]');b.disabled=true;try{await submit(field?.value);close()}catch(err){hooks.notify(err.message);b.disabled=false}};
  document.body.append(dlg);dlg.showModal();field?.focus();return dlg;
 }
 function rename(key){
  const x=byKey(key),container=row(key),name=container.querySelector('.tree-name'),input=document.createElement('input');input.className='tree-rename';input.value=x.name||x.title;input.maxLength=180;name.replaceWith(input);renameInput=input;input.focus();input.select();let submitting=false;
  const cancel=()=>{if(renameInput!==input)return;renameInput=null;draw();node(key)?.focus()};input.onblur=()=>{if(!submitting)cancel()};input.onkeydown=async e=>{e.stopPropagation();if(e.key==='Escape'){e.preventDefault();cancel()}if(e.key==='Enter'&&!submitting){e.preventDefault();if(!input.value.trim())return;submitting=true;try{if(isFolder(key))await hooks.send('/library/folders/'+x.id,{name:input.value.trim(),parent:x.parent,revision:x.revision||0},'PATCH');else await hooks.send('/library/documents/'+x.id,{title:input.value.trim(),library_revision:x.library_revision||0},'PATCH');renameInput=null;await hooks.refresh();node(key)?.focus()}catch(err){hooks.notify(err.message);submitting=false;input.focus()}}};
 }
 async function commandAction(action,key,keys=[key]){
  if(action==='toggle'){const id=key.slice(2);expanded.has(id)?expanded.delete(id):expanded.add(id);save();draw()}
  if(action==='open')await hooks.open(key.slice(2));
  if(action==='rename')rename(key);
  if(action==='import')hooks.import(key.slice(2));
  if(action==='new-folder'){const input=document.createElement('input');input.required=true;input.maxLength=180;promptDialog('新建子文件夹','名称',input,async name=>{await hooks.send('/library/folders',{name,parent:key.slice(2)});expanded.add(key.slice(2));save();await hooks.refresh()})}
  if(action==='duplicate'){const n=await hooks.send('/projects/'+key.slice(2)+'/duplicate');await hooks.refresh();await hooks.open(n.id)}
  if(action==='download')await hooks.download('/api/projects/'+key.slice(2)+'/output?format=markdown','正文.md');
  if(action==='move'){const select=document.createElement('select');let banned=new Set(keys.filter(isFolder).map(k=>k.slice(2))),n=-1;while(n!==banned.size){n=banned.size;data.folders.filter(f=>banned.has(f.parent)).forEach(f=>banned.add(f.id))}select.innerHTML='<option value="">我的材料</option>'+data.folders.filter(f=>!banned.has(f.id)).map(f=>`<option value="${esc(f.id)}">${esc(path(f.parent)+' / '+f.name)}</option>`).join('');promptDialog('移动所选材料','目标文件夹',select,v=>act('move',keys,v||null))}
  if(['trash','restore'].includes(action))await act(action,keys);
  if(action==='purge')promptDialog('彻底删除所选材料','',null,()=>act('purge',keys,null,true),'材料及历史版本将无法通过本应用恢复，服务器文件与备份不会在此处安全擦除，已有导出和费用记录保留');
 }
 host.setAttribute('role','tree');host.setAttribute('aria-multiselectable','true');
 host.onclick=async e=>{if(e.target.closest('input'))return;const r=e.target.closest('[data-row]');if(!r)return;const key=r.dataset.row;focus=key;try{
  if(e.target.closest('[data-more]')){const box=r.getBoundingClientRect();openMenu(key,box.right-15,box.bottom);return}
  if(e.target.closest('[data-toggle]')&&isFolder(key)){await commandAction('toggle',key);return}
  if(e.shiftKey&&anchor){const keys=visible(),a=keys.indexOf(anchor),b=keys.indexOf(key);if(a>=0)selected=new Set(keys.slice(Math.min(a,b),Math.max(a,b)+1))}
  else if(e.ctrlKey||e.metaKey){selected.has(key)?selected.delete(key):selected.add(key);anchor=key}
  else{selected=new Set([key]);anchor=key}
  draw();node(key)?.focus();if(!isFolder(key)&&!e.ctrlKey&&!e.metaKey&&!e.shiftKey)await hooks.open(key.slice(2));
 }catch(err){hooks.notify(err.message)}};
 host.oncontextmenu=e=>{const r=e.target.closest('[data-row]');e.preventDefault();if(r){openMenu(r.dataset.row,e.clientX,e.clientY);return}if(trash)return;closeMenu();const menu=document.createElement('div');menu.id='tree-menu';menu.className='tree-menu';menu.setAttribute('role','menu');menu.setAttribute('aria-label','材料库操作');menu.innerHTML='<button role="menuitem" data-root="import">导入材料</button><button role="menuitem" data-root="folder">新建文件夹</button>';document.body.append(menu);menu.style.left=Math.max(8,Math.min(e.clientX,innerWidth-menu.offsetWidth-8))+'px';menu.style.top=Math.max(8,Math.min(e.clientY,innerHeight-menu.offsetHeight-8))+'px';menu.querySelector('button').focus();menu.onclick=e=>{const action=e.target.closest('[data-root]')?.dataset.root;if(!action)return;closeMenu();if(action==='import')hooks.import(null);else{const input=document.createElement('input');input.required=true;input.maxLength=180;promptDialog('新建文件夹','名称',input,async name=>{await hooks.send('/library/folders',{name,parent:null});await hooks.refresh()})}};menu.onkeydown=e=>{if(e.key==='Escape'){e.preventDefault();closeMenu()}if(['ArrowDown','ArrowUp'].includes(e.key)){e.preventDefault();const buttons=[...menu.querySelectorAll('button')];buttons[(buttons.indexOf(document.activeElement)+1)%buttons.length].focus()}}};
 host.onkeydown=async e=>{if(e.target.closest('input'))return;const key=e.target.closest('[data-node]')?.dataset.node;if(!key)return;const keys=visible(),i=keys.indexOf(key);try{
  if(['ArrowDown','ArrowUp','Home','End'].includes(e.key)){e.preventDefault();const next=keys[e.key==='Home'?0:e.key==='End'?keys.length-1:Math.max(0,Math.min(keys.length-1,i+(e.key==='ArrowDown'?1:-1)))];focus=next;if(e.shiftKey){anchor??=key;const a=keys.indexOf(anchor),b=keys.indexOf(next);selected=new Set(keys.slice(Math.min(a,b),Math.max(a,b)+1))}draw();node(next)?.focus()}
  if(e.key==='ArrowRight'&&isFolder(key)){e.preventDefault();if(!expanded.has(key.slice(2)))await commandAction('toggle',key);else{focus=keys[i+1]||key;draw();node(focus)?.focus()}}
  if(e.key==='ArrowLeft'){e.preventDefault();if(isFolder(key)&&expanded.has(key.slice(2)))await commandAction('toggle',key);else{const parent=isFolder(key)?byKey(key).parent:byKey(key).folder;if(parent){focus='f:'+parent;draw();node(focus)?.focus()}}}
  if(e.key==='Enter'){e.preventDefault();await commandAction(isFolder(key)?'toggle':'open',key)}
  if(e.key===' '){e.preventDefault();selected.has(key)?selected.delete(key):selected.add(key);draw();node(key)?.focus()}
  if(e.key==='F2'&&!trash){e.preventDefault();rename(key)}
  if(e.key==='Delete'&&!trash){e.preventDefault();await act('trash',selected.has(key)?[...selected]:[key])}
  if(e.key==='F10'&&e.shiftKey){e.preventDefault();const box=row(key).getBoundingClientRect();openMenu(key,box.left+15,box.bottom)}
  if((e.ctrlKey||e.metaKey)&&e.key==='a'){e.preventDefault();selected=new Set(keys);draw()}
 }catch(err){hooks.notify(err.message)}};
 host.ondragstart=e=>{const r=e.target.closest('[data-row]');if(trash||!r){e.preventDefault();return}const key=r.dataset.row;dragged=selected.has(key)?[...selected]:[key];e.dataTransfer.setData('application/x-sourceloom',JSON.stringify(dragged));e.dataTransfer.effectAllowed='move'};
 host.ondragover=e=>{if(!dragged)return;const r=e.target.closest('[data-row]');if(!r||isFolder(r.dataset.row)){e.preventDefault();host.querySelectorAll('.drop-target').forEach(x=>x.classList.remove('drop-target'));r?.classList.add('drop-target')}};
 host.ondrop=async e=>{if(!dragged)return;e.preventDefault();const r=e.target.closest('[data-row]'),keys=dragged;dragged=null;host.querySelectorAll('.drop-target').forEach(x=>x.classList.remove('drop-target'));if(r&&!isFolder(r.dataset.row))return;try{await act('move',keys,r?.dataset.row.slice(2)||null)}catch(err){hooks.notify(err.message)}};
 host.ondragend=()=>{dragged=null;host.querySelectorAll('.drop-target').forEach(x=>x.classList.remove('drop-target'))};
 document.addEventListener('pointerdown',e=>{if(!e.target.closest('#tree-menu,[data-more]'))document.getElementById('tree-menu')?.remove()});
 return {render(next,options={}){data=next;trash=Boolean(options.trash);current=options.current;search=String(options.query||'').toLowerCase().trim();selected=new Set([...selected].filter(k=>byKey(k)));if(!selected.size&&current)selected.add('d:'+current);draw()},reveal(id){let d=data.documents.find(d=>d.id===id),parent=d?.folder;const seen=new Set();while(parent&&!seen.has(parent)){seen.add(parent);expanded.add(parent);parent=data.folders.find(f=>f.id===parent)?.parent}save();focus='d:'+id;selected=new Set([focus]);draw()},collapse(){expanded.clear();save();draw()},menu(){const key=[...selected][0]||focus;if(key){const box=row(key)?.getBoundingClientRect();openMenu(key,box?.left||20,box?.bottom||150)}}};
}
