export class ReadWeaveImport {
  constructor({read,context,submit}) {
    Object.assign(this,{read,context,submit});this.token=0;this.path=[];
    this.dialog=document.createElement('dialog');this.dialog.className='readweave-import-dialog';
    this.dialog.innerHTML='<form><div class="dialog-heading"><h2>导入 ReadWeave</h2><button type="button" class="icon" aria-label="关闭">×</button></div><p class="readweave-import-material"></p><p class="muted">正文、图文资源与原件一并交付，保存后独立读回核对</p><div class="readweave-target-presets"><button type="button" data-default>统一管理（默认）</button><button type="button" data-root>选择其他位置</button></div><nav class="readweave-path" aria-label="目标路径"></nav><section class="readweave-folders" aria-label="可选位置"></section><p class="readweave-import-target"></p><p class="readweave-import-state" role="status" aria-live="polite"></p><div class="dialog-actions"><button type="button" data-cancel>取消</button><button class="primary" type="submit">导入到此位置</button></div></form>';
    document.body.append(this.dialog);this.form=this.dialog.querySelector('form');this.state=this.dialog.querySelector('.readweave-import-state');this.target=this.dialog.querySelector('.readweave-import-target');this.confirm=this.form.querySelector('[type=submit]');
    this.dialog.querySelector('[aria-label="关闭"]').onclick=this.dialog.querySelector('[data-cancel]').onclick=()=>this.dialog.close();
    this.dialog.addEventListener('close',()=>{++this.token;this.trigger?.focus({preventScroll:true});});
    this.dialog.querySelector('[data-default]').onclick=()=>this.browse(this.snapshot.defaultId,[]);
    this.dialog.querySelector('[data-root]').onclick=()=>this.browse('root',[]);
    this.form.onsubmit=async event=>{event.preventDefault();if(this.confirm.disabled)return;
      const c=this.context();if(c.id!==this.snapshot.id||c.version!==this.snapshot.version||c.sourceDigest!==this.snapshot.sourceDigest){this.state.textContent='材料或版本已改变，请关闭后重新打开';this.confirm.disabled=true;return;}
      const token=this.token;this.setBusy(true);this.state.textContent='正在导入并核对原件与资源，请勿重复提交';
      try{const r=await this.submit(this.selected,this.snapshot);if(token!==this.token)return;
        if(r?.status==='readback_passed'){this.dialog.close();return;}
        this.state.textContent=r?.message||'导入已记录，尚未完成读回；请查询原操作，不要再次建立副本';
      }catch(error){if(token===this.token)this.state.textContent=error.message;}
      finally{if(token===this.token)this.setBusy(false);}
    };
  }
  setBusy(value){this.busy=value;for(const button of this.dialog.querySelectorAll('button'))button.disabled=value;this.confirm.disabled=value||!this.selected;}
  async open(trigger) {
    this.trigger=trigger;const c=this.context();this.snapshot={...c};this.selected=null;this.path=[];++this.token;
    this.dialog.querySelector('.readweave-import-material').textContent=c.title+' · 当前成稿版本';
    this.dialog.showModal();await this.browse(c.defaultId,[]);
  }
  async browse(id,path,offset=0) {
    const token=++this.token;this.setBusy(true);this.state.textContent='正在读取目标位置';this.selected=null;this.target.textContent='';
    try {
      const result=await this.read('/api/processor/readweave-destinations?parent='+encodeURIComponent(id)+'&offset='+offset);if(token!==this.token||!this.dialog.open)return;
      this.selected=result.parent.id;this.path=[...path,result.parent];this.target.textContent='将保存到：'+this.path.map(p=>p.title).join(' / ');
      const nav=this.dialog.querySelector('.readweave-path');nav.replaceChildren();this.path.forEach((p,index)=>{const b=document.createElement('button');b.type='button';b.textContent=p.title;b.onclick=()=>this.browse(p.id,this.path.slice(0,index));nav.append(b);});
      const folders=this.dialog.querySelector('.readweave-folders');if(!offset)folders.replaceChildren();else folders.querySelector('[data-more]')?.remove();for(const n of result.children){const button=document.createElement('button');button.type='button';button.textContent=n.title;button.title='选择 '+n.title;button.onclick=()=>this.browse(n.id,this.path);folders.append(button);}
      if(!result.children.length&&!offset){const empty=document.createElement('p');empty.className='muted';empty.textContent='此位置没有子目录，可以直接导入到这里';folders.append(empty);}
      if(result.next_offset!==null&&result.next_offset!==undefined){const more=document.createElement('button');more.type='button';more.dataset.more='true';more.textContent='加载更多位置';more.onclick=()=>this.browse(id,path,result.next_offset);folders.append(more);}
      this.state.textContent='确认后保存新笔记，不覆盖此位置已有内容';
    }catch(error){if(token===this.token){this.selected=null;this.state.textContent='目标位置读取失败：'+error.message;}}
    finally{if(token===this.token)this.setBusy(false);}
  }
}
