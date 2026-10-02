// One-file handoff shares the normal source/task/result workflow.
export const START_MESSAGE='请用文件工具读取附件任务包中的 START_HERE.md，按其中要求处理完整材料；需要看图时实际打开包内对应图像，完成后返回完整 Markdown 文件。';
export class ManualHandoff {
  constructor(deps) {
    this.deps=deps;this.key='';this.token=0;this.data=null;
    this.root=deps.element('section');this.root.id='manual-upload-guide';
    this.download=document.querySelector('#task-pack-download');
    this.copy=document.querySelector('#copy-prompt');
    document.querySelector('#manual-handoff').prepend(this.root);
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
    this.root.append(el('p','下载一份完整任务包，在你选择的 xhigh 对话中上传，粘贴开始指令；完成后导回 Markdown。正文、图表和有效要求都在包内。'));
    const actions=el('div',undefined,'source-tools');
    if(this.data)this.download.href=this.data.package_url;
    actions.append(this.download,this.copy);
    const back=el('button','导回成稿');back.type='button';
    back.addEventListener('click',this.deps.showResult);actions.append(back);
    this.root.append(actions,el('p','目标会话需要实际解包、读取正文并查看必要图像；这一能力仍待该会话验证，不能仅凭 ZIP 下载成功算通过。现有成稿可以直接导回，不受通道验证影响。','muted'));
  }
}
