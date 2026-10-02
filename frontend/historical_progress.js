// Historical metadata/readback compatibility only; not a product UI or browser event handler.
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function progressMarkup(run){
 const p=run?.progress;if(!p)return '';
 const stages=['保存原件','清点信息与材料','整理结构','改写正文','核对修正并交付'];
 const current=Math.min(stages.length-1,Math.max(0,Number(p.current)||0));
 const steps=stages.map((label,index)=>{
  const done=Boolean(p.completed)||index<current;
  const active=!p.completed&&index===current;
  return `<li class="${done?'done':active?'current':''}" ${active?'aria-current="step"':''}><span>${index+1}</span>${label}</li>`;
 }).join('');
 const error=run.error?'<p class="progress-error">'+escape(/validation error|pydantic|Traceback/i.test(run.error)?'本次核对返回的格式不完整，已保存原件和已有正文':run.error)+'</p>':'';
 const originals=(run.received_files||[]).map(f=>`<a href="${escape(f.url)}" download>下载原件 ${escape(f.name)}</a>`).join('');
 return `<section class="production-progress" aria-label="处理记录"><h2>处理记录</h2><ol aria-label="五个处理阶段">${steps}</ol><p role="status">${escape(p.label)}${p.active?'，可以关闭页面后再回来':''}</p>${p.active?`<span class="elapsed" data-started="${Number(p.started)||0}"></span>`:''}${error}${originals}</section>`;
}
