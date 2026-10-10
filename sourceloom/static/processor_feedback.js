// A single surface owns global messages; local upload state stays beside its file.
export class Feedback {
  constructor(host, message, elapsed) {
    this.host=host;this.message=message;this.elapsed=elapsed;this.serial=0;
    this.close=document.createElement('button');this.close.type='button';
    this.close.className='feedback-close';this.close.textContent='×';
    this.close.setAttribute('aria-label','关闭通知');host.append(this.close);
    this.close.onclick=()=>this.hide();
  }
  show(text,state='success') {
    this.clear();const serial=++this.serial;
    this.host.hidden=!text;this.host.dataset.state=state;
    this.message.textContent=text;this.elapsed.textContent='';
    if(!text)return;
    if(state==='running') {
      const start=performance.now();
      this.ticker=setInterval(()=>{this.elapsed.textContent=`已等待 ${Math.floor((performance.now()-start)/1000)} 秒`;},1000);
    } else if(state==='success')this.timer=setTimeout(()=>{if(serial===this.serial)this.hide();},6000);
  }
  clear(){clearInterval(this.ticker);clearTimeout(this.timer);}
  hide(){this.clear();++this.serial;this.host.hidden=true;}
}
