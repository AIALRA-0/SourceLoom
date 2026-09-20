const TABS=[
 {id:'interface',label:'接口'},
 {id:'retrieval',label:'检索'},
 {id:'generation',label:'生成'},
 {id:'cost',label:'成本'},
 {id:'sync',label:'ReadWeave 同步'}
];

const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const number=(value,fallback=0)=>Number.isFinite(Number(value))?Number(value):fallback;
const array=value=>Array.isArray(value)?value.filter(Boolean).map(item=>typeof item==='object'?(item.id??item.name??item.provider_id??''):String(item)).filter(Boolean):[];

export function maskSecret(value){
 if(value==null||value==='')return '';
 const text=String(value);
 if(/[•*]/.test(text))return '••••••••';
 if(text.length<=4)return '••••';
 return `${text.slice(0,2)}••••${text.slice(-2)}`;
}

function provider(raw={},index=0){
 const key=raw.api_key_masked??raw.apiKeyMasked??raw.secret_masked??raw.api_key??raw.apiKey??'';
 return {
  id:String(raw.id??raw.provider_id??raw.provider??`provider-${index+1}`),
  name:String(raw.name??raw.label??raw.provider_id??raw.provider??`接口 ${index+1}`),
  status:String(raw.status??raw.state??(raw.enabled===false?'disabled':raw.has_credentials?'ready':'unconfigured')),
  protocol:String(raw.protocol??raw.transport??'chat_completions'),
  model:String(raw.model??''),
  priority:number(raw.priority,index+1),
  keyMasked:maskSecret(key),
  secretDraft:''
 };
}

export function normalizeSettings(payload={}){
 const source=payload.settings??payload.data??payload;
 const interfaceData=source.interface??source.interfaces??{};
 const activeProvider=source.active??source.active_provider??null;
 const roleProviders=source.roles&&typeof source.roles==='object'?Object.values(source.roles):[];
 const providerList=interfaceData.providers??source.providers??source.routes??(activeProvider?[activeProvider,...roleProviders]:source.provider?[source.provider]:roleProviders);
 const retrieval=source.retrieval??source.search??{};
 const generation=source.generation??source.writing??{};
 const cost=source.cost??source.costs??source.pricing??activeProvider?.pricing??{};
 const sync=source.readweave??source.readWeave??source.sync??{};
 const lifecycle=String(payload.lifecycle??payload.state??source.lifecycle??source.state??(source.active?'active':'draft')).toLowerCase();
 return {
  lifecycle:['draft','probed','active'].includes(lifecycle)?lifecycle:'draft',
  available:source.available!==false,
  providers:providerList.map((item,index)=>provider(item,index)),
  primaryProvider:String(interfaceData.primary_provider??interfaceData.primaryProvider??source.primary_provider??source.primaryProvider??activeProvider?.provider_id??''),
  searchOrder:array(retrieval.search_order??retrieval.order??source.search_order??['OpenAlex','TinyFish','Octen','Parallel']),
  queryLimit:number(retrieval.query_limit??retrieval.max_queries,2),
  openLimit:number(retrieval.open_limit??retrieval.max_opens,2),
  generation:{
   contentPatchLimit:number(generation.content_patch_limit??generation.contentRepairLimit??generation.content_repair_limit,2),
   formatPatchLimit:number(generation.format_patch_limit??generation.formatRepairLimit??generation.format_repair_limit,2),
   headingNumbering:String(generation.heading_numbering??generation.title_numbering??'preserve'),
   mediaCollapsed:generation.media_collapsed??generation.collapse_media??true
  },
  cost:{
   cacheHit:number(cost.cache_hit_input??cost.cache_hit_input_rate??cost.cacheHit??cost.cache_hit_input_per_million??cost.cacheHitInputPerMillion,0.054),
   cacheMiss:number(cost.cache_miss_input??cost.cache_miss_input_rate??cost.cacheMiss??cost.cache_miss_input_per_million??cost.cacheMissInputPerMillion,1.62),
   output:number(cost.output??cost.output_rate??cost.output_per_million??cost.outputPerMillion,4.86),
   multiplier:number(cost.display_multiplier??cost.multiplier,0.15),
   fx:number(cost.fx_rate??cost.exchange_rate,7.2),
   currency:String(cost.currency??'CNY / 百万 Token')
  },
  sync:{
   profileId:String(sync.profile_id??sync.profileId??''),
   digest:String(sync.profile_digest??sync.profileDigest??''),
   status:String(sync.status??'未同步'),
   updatedAt:String(sync.updated_at??sync.updatedAt??'')
  }
 };
}

export function buildSettingsPayload(state){
 const payload={
  interface:{
   primary_provider:state.primaryProvider,
   providers:state.providers.map(item=>{
    const next={id:item.id,protocol:item.protocol,model:item.model,priority:item.priority};
    if(item.secretDraft)next.api_key=item.secretDraft;
    return next;
   })
  },
  retrieval:{search_order:state.searchOrder,query_limit:state.queryLimit,open_limit:state.openLimit},
  generation:{content_patch_limit:state.generation.contentPatchLimit,format_patch_limit:state.generation.formatPatchLimit,heading_numbering:state.generation.headingNumbering,media_collapsed:state.generation.mediaCollapsed},
  cost:{cache_hit_input:state.cost.cacheHit,cache_miss_input:state.cost.cacheMiss,output:state.cost.output,display_multiplier:state.cost.multiplier,fx_rate:state.cost.fx},
  readweave:{profile_id:state.sync.profileId,profile_digest:state.sync.digest}
 };
 return payload;
}

export function nextLifecycleState(action,current='draft'){
 if(action==='save')return 'draft';
 if(action==='probe')return 'probed';
 if(action==='activate')return 'active';
 return current;
}

function field(label,content,help=''){return `<label class="settings-field"><span>${label}</span>${content}${help?`<small>${help}</small>`:''}</label>`}
function textInput(path,value,attrs=''){return `<input data-setting="${path}" value="${esc(value)}" ${attrs}>`}
function numberInput(path,value,min=0,max=100){return textInput(path,value,`type="number" min="${min}" max="${max}" step="1"`)}

export function settingsPanel({api,notice=()=>{}}){
 const dialog=document.querySelector('#settings-dialog');
 const root=document.querySelector('#settings-panel');
 if(!dialog||!root)throw new Error('settings panel markup is missing');
 let state=normalizeSettings({available:false,state:'draft'}),tab='interface',loading=false;

 function lifecycleLabel(){return {draft:'草稿',probed:'已探测',active:'已启用'}[state.lifecycle]||'草稿'}
 function render(){
  const unavailable=!state.available;
  root.innerHTML=`<div class="settings-head"><div><p class="eyebrow">工作台设置</p><h2 id="settings-title">接口与生产策略</h2></div><div class="settings-head-actions"><span class="settings-state ${unavailable?'unavailable':state.lifecycle}">${unavailable?'暂不可用':lifecycleLabel()}</span><button type="button" data-settings-action="close" aria-label="关闭设置">关闭</button></div></div>
   <p class="settings-intro">配置只在当前设置窗口内编辑；密钥不会写入浏览器存储。服务端未提供设置接口时，仍可查看页面结构并安全退出。</p>
   <div class="settings-tabs" role="tablist" aria-label="设置分类">${TABS.map(item=>`<button type="button" role="tab" aria-selected="${tab===item.id}" aria-controls="settings-tab-${item.id}" data-settings-tab="${item.id}">${item.label}</button>`).join('')}</div>
   <form id="settings-form" class="settings-form" ${unavailable?'data-unavailable="true"':''}>
    <section id="settings-tab-interface" role="tabpanel" ${tab==='interface'?'':'hidden'}>${renderInterface()}</section>
    <section id="settings-tab-retrieval" role="tabpanel" ${tab==='retrieval'?'':'hidden'}>${renderRetrieval()}</section>
    <section id="settings-tab-generation" role="tabpanel" ${tab==='generation'?'':'hidden'}>${renderGeneration()}</section>
    <section id="settings-tab-cost" role="tabpanel" ${tab==='cost'?'':'hidden'}>${renderCost()}</section>
    <section id="settings-tab-sync" role="tabpanel" ${tab==='sync'?'':'hidden'}>${renderSync()}</section>
   </form>
   <div class="settings-actions"><button type="button" data-settings-action="import">从 ReadWeave 导入</button><span class="settings-action-spacer"></span><button type="button" data-settings-action="probe" ${unavailable||loading?'disabled':''}>检查配置</button><button type="button" data-settings-action="save" ${unavailable||loading?'disabled':''}>保存草稿</button><button type="button" class="primary" data-settings-action="activate" ${unavailable||loading?'disabled':''}>启用配置</button></div>
   <p class="settings-feedback" role="status" aria-live="polite">${unavailable?'设置服务暂不可用，稍后可重试。':''}</p>`;
 }
 function renderInterface(){
  const providers=state.providers.length?state.providers.map((item,index)=>`<article class="provider-card"><div class="provider-title"><strong>${esc(item.name)}</strong><span class="provider-status provider-${esc(item.status)}">${esc(item.status)}</span></div>${field('协议',`<select data-provider="${index}" data-provider-field="protocol"><option value="responses" ${item.protocol==='responses'?'selected':''}>Responses</option><option value="chat_completions" ${item.protocol==='chat_completions'?'selected':''}>Chat Completions</option></select>`)}${field('模型',textInput('',item.model,`data-provider="${index}" data-provider-field="model" autocomplete="off"`))}${field('优先级',numberInput('',item.priority,1,99).replace('data-setting=""',`data-provider="${index}" data-provider-field="priority"`))}${field('API 密钥',`<div class="secret-row"><input type="password" value="${esc(item.keyMasked||'未配置')}" data-secret="${index}" readonly autocomplete="off"><button type="button" data-secret-edit="${index}">${item.keyMasked?'更换':'填写'}</button></div>`,'仅显示脱敏值；新密钥只存在内存，保存时提交给服务端。')}</article>`).join(''):'<div class="settings-empty">服务端尚未返回 provider 配置。</div>';
  return `<div class="settings-section"><h3>模型接口</h3><p class="settings-muted">保留每条线路的状态、协议、模型和优先级，未配置的线路不会被前端猜测。</p>${providers}</div>`;
 }
 function renderRetrieval(){return `<div class="settings-section"><h3>检索边界</h3><p class="settings-muted">按顺序使用搜索适配器，只在明确证据缺口时发起检索。</p>${field('Search 顺序',`<textarea data-setting="searchOrder" rows="4" spellcheck="false">${esc(state.searchOrder.join('\n'))}</textarea>`,'每行一个适配器；顺序由服务端执行。')}${field('定向查询上限',numberInput('queryLimit',state.queryLimit,0,20))}${field('正文打开上限',numberInput('openLimit',state.openLimit,0,20))}</div>`}
 function renderGeneration(){return `<div class="settings-section"><h3>生成与阅读</h3>${field('内容修复上限',numberInput('generation.contentPatchLimit',state.generation.contentPatchLimit,0,10))}${field('格式修复上限',numberInput('generation.formatPatchLimit',state.generation.formatPatchLimit,0,10))}${field('标题编号',`<select data-setting="generation.headingNumbering"><option value="preserve" ${state.generation.headingNumbering==='preserve'?'selected':''}>沿用原稿</option><option value="numbered" ${state.generation.headingNumbering==='numbered'?'selected':''}>显示编号</option><option value="none" ${state.generation.headingNumbering==='none'?'selected':''}>不加编号</option></select>`)}<label class="settings-check"><input type="checkbox" data-setting="generation.mediaCollapsed" ${state.generation.mediaCollapsed?'checked':''}> 材料配图默认折叠</label></div>`}
 function renderCost(){return `<div class="settings-section"><h3>价格与计费口径</h3><p class="settings-muted">费用口径由服务端账本确认；倍率只作为商业说明，不参与再次折算。</p><div class="settings-rate-grid">${field('缓存读取 ¥ / 百万',numberInput('cost.cacheHit',state.cost.cacheHit,0,1000).replace('step="1"','step="0.0001"'))}${field('未命中缓存 ¥ / 百万',numberInput('cost.cacheMiss',state.cost.cacheMiss,0,1000).replace('step="1"','step="0.0001"'))}${field('输出 ¥ / 百万',numberInput('cost.output',state.cost.output,0,1000).replace('step="1"','step="0.0001"'))}${field('显示倍率',numberInput('cost.multiplier',state.cost.multiplier,0,1).replace('step="1"','step="0.01"'))}${field('汇率 CNY / USD',numberInput('cost.fx',state.cost.fx,0,100).replace('step="1"','step="0.01"'))}</div><p class="settings-hint">当前示例：¥0.054 / ¥1.62 / ¥4.86；0.15 不会再次乘入计费公式。</p></div>`}
 function renderSync(){return `<div class="settings-section"><h3>ReadWeave 同步</h3><p class="settings-muted">导入风格契约和规则摘要，保持 SourceLoom 原生实现。</p>${field('风格 Profile',textInput('sync.profileId',state.sync.profileId||'未同步',`readonly`))}${field('Profile 摘要',textInput('sync.digest',state.sync.digest||'未同步',`readonly`))}<div class="sync-meta"><span>状态：${esc(state.sync.status)}</span>${state.sync.updatedAt?`<span>更新时间：${esc(state.sync.updatedAt)}</span>`:''}</div><button type="button" data-settings-action="import">导入最新 ReadWeave 配置</button></div>`}

 function setPath(path,value){
  const parts=path.split('.');let target=state;
  for(let i=0;i<parts.length-1;i++)target=target[parts[i]];
  target[parts.at(-1)]=value;
 }
 function inputValue(target){return target.type==='checkbox'?target.checked:target.value}
 function markDraft(){if(state.lifecycle!=='draft'){state.lifecycle='draft';render()} }
 function updateFromInput(target){
  const path=target.dataset.setting;if(!path)return;
  let value=inputValue(target);
  if(path==='searchOrder')value=value.split(/\r?\n|,/).map(item=>item.trim()).filter(Boolean);
  if(/Limit$/.test(path)||path==='queryLimit'||path==='openLimit'||path.startsWith('cost.'))value=number(value,0);
  setPath(path,value);state.lifecycle='draft';
 }
 function updateProvider(target){
  const item=state.providers[Number(target.dataset.provider)];if(!item)return;
  const fieldName=target.dataset.providerField;item[fieldName]=fieldName==='priority'?number(target.value,1):target.value;state.lifecycle='draft';
 }
 function feedback(message){const node=root.querySelector('.settings-feedback');if(node)node.textContent=message}
 function mergeResponse(result,action){
  const incoming=result?.settings??result?.data?.settings??((result&&['interface','retrieval','generation','cost','readweave','providers'].some(key=>key in result))?result:null);
  if(incoming&&typeof incoming==='object'){
   const previous=state;state=normalizeSettings({...incoming,state:result?.state??result?.lifecycle??nextLifecycleState(action,previous.lifecycle)});
   state.providers.forEach((item,index)=>{item.secretDraft=previous.providers[index]?.secretDraft||''});
  }else state.lifecycle=nextLifecycleState(action,state.lifecycle);
  state.available=true;render();
 }
 async function request(action,path,method='POST'){
  loading=true;render();
  try{
   const options={method,headers:{'Content-Type':'application/json'}};
   if(method!=='GET')options.body=JSON.stringify(buildSettingsPayload(state));
   const result=await api(path,options);loading=false;mergeResponse(result,action);feedback(action==='probe'?'配置检查完成':action==='activate'?'配置已启用':'草稿已保存');notice('');
  }catch(error){loading=false;state.available=true;render();feedback(`操作未完成：${error.message||'设置服务不可用'}`);notice(error.message||'设置服务不可用')}
 }
 function bind(){
  if(root.dataset.bound)return;root.dataset.bound='true';
  root.addEventListener('click',event=>{
   const tabButton=event.target.closest('[data-settings-tab]');
   if(tabButton){tab=tabButton.dataset.settingsTab;render();return}
   const edit=event.target.closest('[data-secret-edit]');
   if(edit){const index=Number(edit.dataset.secretEdit),input=root.querySelector(`[data-secret="${index}"]`);if(input){input.readOnly=false;input.value='';state.providers[index].secretDraft='';input.focus()}return}
   const action=event.target.closest('[data-settings-action]')?.dataset.settingsAction;if(!action)return;
   if(action==='close'){dialog.close();return}
   if(action==='import'){request('import','/admin/settings/import-readweave');return}
   if(action==='save'){request('save','/admin/settings','PUT');return}
   if(action==='probe'){request('probe','/admin/settings/probe');return}
   if(action==='activate'){request('activate','/admin/settings/activate');return}
  });
  root.addEventListener('input',event=>{const target=event.target;if(target.dataset.secret!=null){const item=state.providers[Number(target.dataset.secret)];if(item)item.secretDraft=target.value;state.lifecycle='draft';return}updateFromInput(target);});
  root.addEventListener('change',event=>{const target=event.target;if(target.dataset.provider!=null){updateProvider(target);return}updateFromInput(target);});
  dialog.addEventListener('cancel',event=>{if(loading)event.preventDefault()});
 }
 async function open(){bind();dialog.showModal();state=normalizeSettings({available:false,state:'draft'});render();try{const result=await api('/admin/settings',{method:'GET'});state=normalizeSettings(result);render()}catch(error){state.available=false;render();feedback('设置服务暂不可用；请稍后重试或联系管理员。')}}
 bind();
 return {open,close:()=>dialog.close(),getState:()=>state};
}
