const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
export function progressMarkup(run){
 const p=run?.progress;if(!p)return '';
 const steps=p.steps.map((label,index)=>`<li class="${index<p.current||p.completed?'done':index===p.current?'current':''}" ${index===p.current?'aria-current="step"':''}><span>${index+1}</span>${escape(label)}</li>`).join('');
 const error=run.error?'<p class="progress-error">'+escape(/validation error|pydantic|Traceback/i.test(run.error)?'本次核对返回的格式不完整，已保存原件和已有正文':run.error)+'</p>':'';
 const originals=(run.received_files||[]).map(f=>`<a href="${escape(f.url)}" download>下载原件 ${escape(f.name)}</a>`).join('');
 return `<div class="production-progress"><ol aria-label="处理阶段">${steps}</ol><p role="status">${escape(p.label)}${p.active?'，可以关闭页面后再回来':''}</p>${p.active?`<span class="elapsed" data-started="${Number(p.started)||0}"></span>`:''}${error}${originals}</div>`;
}
setInterval(()=>{for(const el of document.querySelectorAll('.elapsed[data-started]')){const seconds=Math.max(0,Math.floor(Date.now()/1000-Number(el.dataset.started)));el.textContent=`距开始 ${Math.floor(seconds/60)} 分 ${seconds%60} 秒，含等待与暂停`}},1000);
