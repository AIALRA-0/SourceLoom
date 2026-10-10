// One-file handoff shares the normal source/task/result workflow.
export const START_MESSAGE='请用文件工具读取附件任务包中的 START_HERE.md，按其中要求处理完整材料；需要看图时实际打开包内对应图像，完成后返回完整 Markdown 文件。';
export class ManualHandoff {
  constructor(deps) {
    this.deps=deps;this.key='';this.token=0;this.data=null;
    this.root=deps.element('section');this.root.id='manual-upload-guide';
    this.download=document.querySelector('#task-pack-download');
    this.copy=document.querySelector('#copy-prompt');
    document.querySelector('#manual-handoff').prepend(this.root);
    const details=deps.element('div',undefined,'handoff-details');
    details.append(document.querySelector('#prompt-details'),document.querySelector('#attachment-details'));
    document.querySelector('#manual-handoff').append(details);
    this.draw();
  }
  message(){return START_MESSAGE;}
  async update(id,packDigest) {
    const key=JSON.stringify([id,packDigest]);if(key===this.key)return;
    this.key=key;const token=++this.token;this.data=null;this.draw();
    try{
      const data=await this.deps.api(`/api/processor/projects/${encodeURIComponent(id)}/manual-handoff`);
      if(token!==this.token||this.deps.current()!==id||data?.pack_digest!==packDigest)return;
      this.data=data;this.draw();
    }catch(e){
      if(token===this.token&&this.deps.current()===id)
        this.deps.notify('任务包摘要暂时无法读取；仍可下载正常完整任务包',true);
    }
  }
  draw() {
    const el=this.deps.element;
    this.root.replaceChildren();
    this.copy.textContent='复制开始指令';
    const steps=el('ol',undefined,'handoff-steps');this.root.append(steps);
    for(const text of ['下载任务包，在 GPT 对话中上传','复制开始指令，交给模型处理','取得完整 .md 或 .txt 文件，在这里上传成稿'])steps.append(el('li',text));
    const actions=el('div',undefined,'handoff-actions');
    if(this.data)this.download.href=this.data.package_url;
    actions.append(this.download,this.copy);
    const back=el('button','上传 GPT 成稿');back.type='button';
    back.addEventListener('click',this.deps.showResult);actions.append(back);
    this.root.append(actions,el('p','上传后检查预览，并点击保存图标保存新版本。任务包按内容生成唯一索引；下载成功不代表模型已读完附件。','muted'));
  }
}
