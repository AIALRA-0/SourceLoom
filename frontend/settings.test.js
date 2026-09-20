import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {buildSettingsPayload,maskSecret,nextLifecycleState,normalizeSettings} from '../sourceloom/static/settings.js';

test('masks provider secrets without retaining the raw value in normalized state',()=>{
 const state=normalizeSettings({providers:[{provider_id:'kuafu',api_key:'example-key',protocol:'responses',model:'deepseek-v4.1-flash'}]});
 assert.equal(state.providers[0].keyMasked,'sk••••le');
 assert.equal(state.providers[0].secretDraft,'');
 assert.equal(maskSecret('••••••••'),'••••••••');
});

test('builds a settings payload without sending an unchanged masked secret',()=>{
 const state=normalizeSettings({providers:[{provider_id:'kuafu',api_key_masked:'sk••••le'}],retrieval:{search_order:['TinyFish','Octen']}});
 const payload=buildSettingsPayload(state);
 assert.deepEqual(payload.interface.providers[0],{id:'kuafu',protocol:'chat_completions',model:'',priority:1});
 assert.deepEqual(payload.retrieval.search_order,['TinyFish','Octen']);
 assert.equal('api_key' in payload.interface.providers[0],false);
});

test('sends a replacement secret only from the in-memory draft',()=>{
 const state=normalizeSettings({providers:[{provider_id:'kuafu',api_key_masked:'••••••••'}]});
 state.providers[0].secretDraft='new-secret';
 assert.equal(buildSettingsPayload(state).interface.providers[0].api_key,'new-secret');
});

test('keeps the draft to probed to active lifecycle explicit',()=>{
 assert.equal(nextLifecycleState('save','active'),'draft');
 assert.equal(nextLifecycleState('probe','draft'),'probed');
 assert.equal(nextLifecycleState('activate','probed'),'active');
});

test('normalizes all five settings groups and safe defaults',()=>{
 const state=normalizeSettings({state:'active',interface:{providers:[]},retrieval:{},generation:{},cost:{},readweave:{}});
 assert.equal(state.lifecycle,'active');
 assert.deepEqual(state.searchOrder,['OpenAlex','TinyFish','Octen','Parallel']);
 assert.equal(state.generation.headingNumbering,'preserve');
 assert.equal(state.generation.mediaCollapsed,true);
 assert.equal(state.cost.multiplier,.15);
 assert.equal(state.sync.status,'未同步');
});

test('accepts the public provider metadata shape used by SourceLoom routes',()=>{
 const state=normalizeSettings({active:{provider_id:'kuafu',model:'deepseek-v4.1-flash',protocol:'responses',enabled:true,has_credentials:true,pricing:{cacheHitInputPerMillion:.054,cacheMissInputPerMillion:1.62,outputPerMillion:4.86}},roles:{fallback:{provider_id:'deepseek',model:'deepseek-chat',enabled:false}}});
 assert.deepEqual(state.providers.map(item=>item.id),['kuafu','deepseek']);
 assert.equal(state.providers[0].status,'ready');
 assert.equal(state.providers[0].protocol,'responses');
 assert.equal(state.cost.output,4.86);
});

test('does not persist settings or secrets in browser storage',()=>{
 const source=readFileSync(new URL('../sourceloom/static/settings.js',import.meta.url),'utf8');
 assert.equal(/localStorage|sessionStorage/.test(source),false);
});
