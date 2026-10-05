// Bound read-only waits through response-body consumption. Never retry writes.
const failure=(code,message,status)=>Object.assign(new Error(message),{code,status});
function responseError(response,type) {
  if(response.status===401||response.status===403||(/text\/html/i.test(type)&&/\/(?:_aialra_auth|sign-in|login)(?:\/|\?|$)/i.test(response.url||'')))
    return failure('authentication','登录状态已失效，请重新登录后重试；当前正文与位置已保留。',response.status);
  if(response.status===404)return failure('not_found','原件或材料不存在，请查看原文件或重新选择材料。',404);
  if(!response.ok)return failure('network',`读取未完成（${response.status}），请重试当前内容。`,response.status);
  return null;
}
async function read(path,options,kind) {
  const {signal,timeoutMs=30000,expectedType='image/',onResponse,...fetchOptions}=options;
  if(fetchOptions.method && fetchOptions.method.toUpperCase()!=='GET')throw failure('protocol','读取接口不能提交修改。');
  const controller=new AbortController();let timer,off;
  const cancelled=new Promise((_,reject)=>{
    off=()=>{controller.abort();reject(failure('aborted','读取已取消。'));};
    if(signal?.aborted)off();else signal?.addEventListener('abort',off,{once:true});
    timer=setTimeout(()=>{controller.abort();reject(failure('timeout','读取尚未完成，请重试当前内容；已显示的正文保持不变。'));},timeoutMs);
  });
  const request=(async()=>{
    const response=await fetch(path,{...fetchOptions,signal:controller.signal});
    onResponse?.(response);
    const type=response.headers.get('content-type')||'';
    const error=responseError(response,type);if(error)throw error;
    if(kind==='json') {
      if(!/(?:application|text)\/(?:[\w.-]+\+)?json\b/i.test(type))throw failure('protocol','读取返回了非材料数据，请重新登录或重试当前材料。',response.status);
      try{return await response.json();}catch(error){if(controller.signal.aborted)throw error;throw failure('protocol','材料数据未能完整读取，请重试当前材料。',response.status);}
    }
    if(!type.toLowerCase().startsWith(expectedType.toLowerCase()))throw failure('protocol','原件返回的文件类型不正确，请重新登录或重试原件。',response.status);
    const blob=await response.blob();
    if(!blob.size)throw failure('protocol','原件文件为空，请查看原文件或重试。',response.status);
    return blob;
  })();
  try{return await Promise.race([request,cancelled]);}
  catch(error){if(error.code)throw error;if(signal?.aborted)throw failure('aborted','读取已取消。');throw failure('network','连接中断，当前正文已保留，请重试当前内容。');}
  finally{clearTimeout(timer);signal?.removeEventListener('abort',off);}
}
export const readJSON=(path,options={})=>read(path,options,'json');
export const readBlob=(path,options={})=>read(path,options,'blob');
