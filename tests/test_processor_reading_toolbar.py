"""Exercise layout transitions without rereading or replacing the PDF document."""
import json
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]


def test_showing_same_width_source_draws_visible_pages_and_toolbar_tracks_split():
    script=r'''
    const fs=require('node:fs'),vm=require('node:vm'),arg=JSON.parse(fs.readFileSync(0,'utf8'));
    const source=fs.readFileSync(arg.root+'/sourceloom/static/processor_reader.js','utf8').replace(/^import[^\n]*\n/gm,'').replace('export class LinkedReader','class LinkedReader');
    const styles={},nodes={};const node=id=>nodes[id]||(nodes[id]={value:'source',clientWidth:400,clientHeight:120,classList:{contains:()=>false},style:{setProperty(k,v){styles[id+':'+k]=v;}},setAttribute(k,v){this[k]=v;}});
    const ctx={document:{getElementById:node,body:{dataset:{}}},clamp:(n,a,b)=>Math.max(a,Math.min(b,n)),console};vm.createContext(ctx);vm.runInContext(source+'\nglobalThis.Reader=LinkedReader;',ctx);
    const r=Object.create(ctx.Reader.prototype),calls={geometry:0,restore:0,relayout:0,draw:0};
    Object.assign(r,{active:true,positions:{left:{page:12},right:{block:'b3'}},refreshGeometry(){calls.geometry++;},restore(){calls.restore++;},capture(){return this.positions;},persist(){},visible:()=>true,pdfView:()=>({relayout(){calls.relayout++;},renderVisible(){calls.draw++;}})});
    r.layoutChange(()=>{});r.setShare(61);
    if(calls.relayout!==1||calls.draw!==1||calls.restore!==1)throw Error('shown equal-width pane was not redrawn');
    if(styles['reading-controls:--left-share']!=='61%'||styles['result-layout:--left-share']!=='61%')throw Error('pane and toolbar splits diverged');
    r.visible=()=>false;r.layoutChange(()=>{});if(calls.draw!==1)throw Error('hidden pane should not allocate canvases');
    process.stdout.write('true');
    '''
    result=subprocess.run([shutil.which('node'),'-e',script],input=json.dumps({'root':str(ROOT)}),text=True,encoding='utf-8',capture_output=True,check=True,timeout=10)
    assert result.stdout=='true'
