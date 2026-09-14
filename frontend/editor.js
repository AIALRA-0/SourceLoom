import {basicSetup} from 'codemirror';
import {EditorState} from '@codemirror/state';
import {EditorView, keymap} from '@codemirror/view';
import {markdown} from '@codemirror/lang-markdown';
import {undo, redo, indentWithTab} from '@codemirror/commands';
import {openSearchPanel} from '@codemirror/search';

export function createEditor(parent, text, onChange, onSave, savedState=null) {
  const view = new EditorView({parent, state:savedState||EditorState.create({doc:text, extensions:[
    basicSetup, markdown(), EditorView.lineWrapping,
    EditorState.phrases.of({'Find':'查找','Replace':'替换','next':'下一处','previous':'上一处','all':'全选匹配',
      'match case':'区分大小写','regexp':'正则表达式','by word':'完整单词','replace':'替换当前','replace all':'全部替换','close':'关闭',
      'Fold line':'折叠此节','Unfold line':'展开此节','to':'至'}),
    keymap.of([indentWithTab, {key:'Mod-s',run:()=>{onSave();return true}}]),
    EditorView.contentAttributes.of({'aria-label':'正文编辑器',spellcheck:'false'}),
    EditorView.updateListener.of(update=>{if(update.docChanged)onChange(update.state.doc.toString())}),
    EditorView.theme({'&':{height:'100%',fontSize:'14px'},'.cm-scroller':{overflow:'auto',fontFamily:'ui-monospace, Consolas, monospace'},'.cm-content':{padding:'16px 0',lineHeight:'1.8'},'.cm-gutters':{backgroundColor:'#fafafa',color:'#999',border:'none'},'&.cm-focused':{outline:'none'}})
  ]})});
  return {view,getText:()=>view.state.doc.toString(),setText:text=>view.dispatch({changes:{from:0,to:view.state.doc.length,insert:text}}),
    focus:()=>view.focus(),destroy:()=>view.destroy(),undo:()=>undo(view),redo:()=>redo(view),search:()=>openSearchPanel(view),
    wrap:(before,after=before)=>{const r=view.state.selection.main;view.dispatch({changes:{from:r.from,to:r.to,insert:before+view.state.sliceDoc(r.from,r.to)+after},selection:{anchor:r.from+before.length,head:r.to+before.length}});view.focus()},
    prefix:prefix=>{const r=view.state.selection.main,first=view.state.doc.lineAt(r.from),last=view.state.doc.lineAt(r.to);view.dispatch({changes:{from:first.from,to:last.to,insert:view.state.sliceDoc(first.from,last.to).split('\n').map(l=>prefix+l).join('\n')}});view.focus()}};
}
