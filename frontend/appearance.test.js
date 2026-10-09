import {test} from 'node:test';
import assert from 'node:assert/strict';
import {readPreferences,applyTheme} from '../sourceloom/static/processor_appearance.js';

test('restricted or malformed local preferences preserve safe defaults',()=>{
  for(const storage of [{getItem(){throw Error('restricted');}},{getItem:()=>'{broken'},{getItem:()=>JSON.stringify({theme:'unknown',density:'unknown',inspector:'true'})}])
    assert.deepEqual(readPreferences(storage),{theme:'system',density:'comfortable',inspector:false});
});
test('explicit themes remain stable when the OS theme changes',()=>{
  const root={dataset:{}};
  applyTheme(root,{theme:'dark',density:'compact'},false);
  assert.deepEqual(root.dataset,{theme:'dark',density:'compact'});
  applyTheme(root,{theme:'light',density:'comfortable'},true);
  assert.deepEqual(root.dataset,{theme:'light',density:'comfortable'});
  applyTheme(root,{theme:'system',density:'comfortable'},true);
  assert.equal(root.dataset.theme,'dark');
});

test('unavailable storage property itself does not prevent opening the workbench',()=>{
  const original=Object.getOwnPropertyDescriptor(globalThis,'localStorage');
  try {
    Object.defineProperty(globalThis,'localStorage',{configurable:true,get(){throw Error('restricted property');}});
    assert.deepEqual(readPreferences(),{theme:'system',density:'comfortable',inspector:false});
  } finally {
    if(original)Object.defineProperty(globalThis,'localStorage',original);
    else delete globalThis.localStorage;
  }
});
