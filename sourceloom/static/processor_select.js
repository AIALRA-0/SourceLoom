/* Shared select-only control. Original <select> owns the form value; only the
   enhanced combobox is exposed for interaction. Action menus share its skin,
   not its semantics. No external library, browser popup or copied APG code. */
(function (g) {
  'use strict';
  const entries = new WeakMap();
  let current = null, serial = 0, hooks = {};
  const options = s => Array.from(s.options);
  const enabled = s => options(s).map((o,i)=>o.disabled ? -1 : i).filter(i=>i>=0);
  function sync(rec) {
    const {select:s, button:b} = rec;
    const option = s.selectedOptions[0];
    b.querySelector('.select-value').textContent = option?.textContent || '请选择';
    b.disabled = s.disabled;
    b.setAttribute('aria-required', String(s.required));
    if(s.getAttribute('aria-invalid')) b.setAttribute('aria-invalid',s.getAttribute('aria-invalid'));
    else b.removeAttribute('aria-invalid');
  }
  function close(commit=false, restore=false) {
    const c = current; if (!c) return false;
    current=null;
    c.rec.button.setAttribute('aria-expanded','false');
    c.rec.button.removeAttribute('aria-activedescendant');
    c.popup.remove();
    if(commit && c.active>=0 && !c.rec.select.options[c.active]?.disabled) {
      const old=c.rec.select.value;
      c.rec.select.selectedIndex=c.active; sync(c.rec);
      if(old!==c.rec.select.value) {
        c.rec.select.dispatchEvent(new Event('input',{bubbles:true}));
        c.rec.select.dispatchEvent(new Event('change',{bubbles:true}));
      }
    }
    if(restore && c.rec.button.isConnected)c.rec.button.focus({preventScroll:true});
    return true;
  }
  function position() {
    if(!current)return;
    const {rec,popup}=current;
    if(!rec.button.isConnected || !rec.button.getClientRects().length){close();return;}
    const r=rec.button.getBoundingClientRect(), vv=window.visualViewport;
    const width=vv?.width||innerWidth, height=vv?.height||innerHeight, ox=vv?.offsetLeft||0,oy=vv?.offsetTop||0,pad=8,gap=6;
    popup.style.width=Math.min(Math.max(r.width,220),width-2*pad)+'px';
    const below=height+oy-r.bottom-gap-pad, above=r.top-oy-gap-pad;
    const upward=below<Math.min(popup.scrollHeight,220)&&above>below;
    popup.style.maxHeight=Math.max(32,Math.min(320,upward?above:below))+'px';
    const h=popup.offsetHeight,w=popup.offsetWidth;
    popup.style.left=Math.max(ox+pad,Math.min(r.left,ox+width-w-pad))+'px';
    popup.style.top=Math.max(oy+pad,Math.min(upward?r.top-gap-h:r.bottom+gap,oy+height-h-pad))+'px';
    popup.dataset.side=upward?'top':'bottom';
  }
  function focusOption(index) {
    const c=current;if(!c)return;
    if(c.active===index&&c.rec.button.hasAttribute('aria-activedescendant'))return;
    c.active=index;
    for(const row of c.popup.querySelectorAll('[role=option]'))row.classList.toggle('option-active',+row.dataset.option===index);
    const el=c.popup.querySelector(`[data-option="${index}"]`);
    if(el) {c.rec.button.setAttribute('aria-activedescendant',el.id);el.scrollIntoView({block:'nearest'});}
  }
  function open(rec) {
    if(rec.select.disabled)return;
    if(current?.rec===rec){close(false,true);return;}
    close();hooks.beforeOpen?.();
    const popup=document.createElement('div');popup.className='popup-surface select-popup';
    popup.id=rec.popupId;popup.setAttribute('role','listbox');
    popup.setAttribute('aria-label',rec.button.getAttribute('aria-label')||'选择选项');
    for(const [index,o] of options(rec.select).entries()){
      const row=document.createElement('div');row.className='choice-row select-option';row.id=rec.popupId+'-'+index;
      row.setAttribute('role','option');row.setAttribute('aria-selected',String(o.selected));row.setAttribute('aria-disabled',String(o.disabled));row.dataset.option=index;
      const mark=document.createElement('span');mark.className='option-mark';mark.setAttribute('aria-hidden','true');if(o.selected)mark.innerHTML=g.WIcons('check');
      const label=document.createElement('span');label.className='label';label.textContent=o.textContent;
      row.append(mark,label);popup.append(row);
    }
    (hooks.host?.()||document.body).append(popup);
    current={rec,popup,active:rec.select.selectedIndex,typed:'',typedAt:0};
    rec.button.setAttribute('aria-expanded','true');position();focusOption(rec.select.selectedIndex);
    popup.addEventListener('pointerdown',e=>e.preventDefault());
    popup.addEventListener('pointermove',e=>{const row=e.target.closest('[data-option]');if(row&&row.getAttribute('aria-disabled')!=='true')focusOption(+row.dataset.option);});
    popup.addEventListener('click',e=>{const row=e.target.closest('[data-option]');if(!row||row.getAttribute('aria-disabled')==='true')return;focusOption(+row.dataset.option);close(true,true);});
  }
  function key(rec,e) {
    if(e.ctrlKey||e.metaKey||e.isComposing)return;
    const k=e.key;
    if(k==='Escape'&&current?.rec===rec){e.preventDefault();e.stopImmediatePropagation();close(false,true);return;}
    if(k==='Tab'&&current?.rec===rec){close(true);return;}
    const list=enabled(rec.select);if(!list.length)return;
    if(['ArrowDown','ArrowUp','Home','End','PageDown','PageUp','Enter',' '].includes(k)) {
      e.preventDefault();e.stopPropagation();
      const wasOpen=current?.rec===rec;
      if(!wasOpen)open(rec);
      else if(k==='Enter'||k===' '||e.altKey&&k==='ArrowUp'){close(true,true);return;}
      if(!current)return;
      let i=list.indexOf(current.active);
      if(k==='Home')i=0;else if(k==='End')i=list.length-1;
      else if(wasOpen&&k==='ArrowDown')i=Math.min(list.length-1,i+1);
      else if(wasOpen&&k==='ArrowUp')i=Math.max(0,i-1);
      else if(k==='PageDown')i=Math.min(list.length-1,i+10);
      else if(k==='PageUp')i=Math.max(0,i-10);
      focusOption(list[Math.max(0,i)]);return;
    }
    if(k.length===1&&!e.altKey){
      e.preventDefault();e.stopPropagation();if(current?.rec!==rec)open(rec);if(!current)return;
      const t=performance.now(),c=current;c.typed=t-c.typedAt<650?c.typed+k:k;c.typedAt=t;
      const needle=/^(.)\1+$/.test(c.typed)?k:c.typed;
      const start=list.indexOf(c.active)+1,order=list.slice(start).concat(list.slice(0,start));
      const found=order.find(i=>rec.select.options[i].textContent.trim().toLocaleLowerCase().startsWith(needle.toLocaleLowerCase()));
      if(found!==undefined)focusOption(found);
    }
  }
  function labelText(s) {
    const label=s.labels?.[0]?.cloneNode(true);if(!label)return '';
    label.querySelectorAll('select,button,input').forEach(n=>n.remove());return label.textContent.trim();
  }
  function enhance(root=document) {
    for(const s of root.querySelectorAll('select:not([data-native])')) {
      if(entries.has(s)){sync(entries.get(s));continue;}
      const b=document.createElement('button');b.type='button';b.className='select-trigger';b.setAttribute('role','combobox');
      b.setAttribute('aria-haspopup','listbox');b.setAttribute('aria-expanded','false');
      const popupId='choice-'+(++serial);b.id=s.id?s.id+'-control':popupId+'-control';
      b.setAttribute('aria-controls',popupId);b.setAttribute('aria-label',s.getAttribute('aria-label')||labelText(s)||s.name||'选项');
      if(s.getAttribute('aria-describedby'))b.setAttribute('aria-describedby',s.getAttribute('aria-describedby'));
      b.dataset.selectName=s.name||'';b.innerHTML='<span class="select-value"></span>'+g.WIcons('down','select-chevron');
      s.before(b);s.hidden=true;s.tabIndex=-1;s.setAttribute('aria-hidden','true');
      const rec={select:s,button:b,popupId};entries.set(s,rec);
      // Mirror application assignments without firing a business change event.
      for(const property of ['value','selectedIndex']) {
        const descriptor=Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype,property);
        if(descriptor?.get&&descriptor?.set)Object.defineProperty(s,property,{configurable:true,get(){return descriptor.get.call(this);},set(value){descriptor.set.call(this,value);sync(rec);}});
      }
      sync(rec);
      b.addEventListener('click',()=>open(rec));b.addEventListener('keydown',e=>key(rec,e));
      s.addEventListener('change',()=>sync(rec));
      new MutationObserver(()=>{sync(rec);if(current?.rec===rec)close(false);}).observe(s,{attributes:true,childList:true,subtree:true});
      s.addEventListener('invalid',e=>{e.preventDefault();b.focus();});
    }
  }
  // One global listener for the entire application, not one per render.
  document.addEventListener('pointerdown',e=>{if(current&&!current.popup.contains(e.target)&&!current.rec.button.contains(e.target))close(false);},true);
  document.addEventListener('focusin',e=>{if(current&&e.target!==current.rec.button&&!current.popup.contains(e.target))close(false);});
  window.addEventListener('resize',()=>close());
  document.addEventListener('keydown',e=>{if(e.key==='Escape'&&current){e.preventDefault();e.stopImmediatePropagation();close(false,true);}},true);
  window.visualViewport?.addEventListener('resize',position);
  document.addEventListener('scroll',e=>{if(current&&!current.popup.contains(e.target))position();},true);
  g.WBSelect=Object.freeze({enhance,close,position,configure:x=>hooks=x,isOpen:()=>!!current});
})(globalThis);
