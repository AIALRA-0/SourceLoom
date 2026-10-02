// Historical metadata/readback compatibility only; not a product UI or browser event handler.
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
