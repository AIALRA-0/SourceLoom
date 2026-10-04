import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
const source = fs.readFileSync(new URL('../sourceloom/static/processor_web_channels.js', import.meta.url), 'utf8');
const {webChannels, webGenerationChannel} = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'));
const web = {id:'router', execution_channel:'chatgpt_web', mode:'chat', available:true, model:'web-model', supports_visual:false};

test('a verified ordinary Web route remains available without adding model options', () => {
  const [channel] = webChannels([web]);
  assert.equal(channel.available, true);
  assert.equal(channel.label, 'Web Chat');
  assert.equal(channel.model, web.model);
  assert.equal(webGenerationChannel([web], 'router'), 'router');
  assert.equal(Object.hasOwn(channel, 'effort'), false);
});

test('other providers are never presented as a fallback', () => {
  assert.deepEqual(webChannels([{id:'api', available:true}, {id:'runner', available:true}]), []);
  assert.throws(() => webGenerationChannel([web, {id:'api', available:true}], 'api'), /未发送/);
});

for (const [name, patch] of [
  ['Codex', {execution_channel:'codex'}],
  ['missing channel', {execution_channel:undefined}],
  ['missing chat mode', {mode:undefined}],
  ['agent mode', {mode:'agent'}],
  ['missing availability', {available:undefined}],
  ['denied permission', {available:false}],
]) {
  test('new generation fails closed for '+name, () => {
    const raw = [{...web, ...patch}];
    assert.equal(webChannels(raw)[0].available, false);
    assert.throws(() => webGenerationChannel(raw, 'router'), /未发送/);
  });
}

test('visual capability must be explicitly supported before sending a visual material', () => {
  assert.throws(() => webGenerationChannel([web], 'router', true), /未发送/);
  assert.throws(() => webGenerationChannel([{...web, supports_visual:undefined}], 'router', true), /未发送/);
  assert.equal(webGenerationChannel([{...web, supports_visual:true}], 'router', true), 'router');
});

test('server reason is retained and legacy Codex model is not shown as a Web option', () => {
  assert.equal(webChannels([{...web, available:false, reason:'业务凭据未授权'}])[0].reason, '业务凭据未授权');
  const invalid = webChannels([{...web, execution_channel:'codex', model:'codex-model'}])[0];
  assert.equal(invalid.model, null);
  assert.match(invalid.reason, /不会切换凭据/);
});

test('map input is supported but boolean legacy capabilities do not grant permission', () => {
  assert.equal(webChannels({router:{...web}})[0].available, true);
  assert.equal(webChannels({router:true, api:true})[0].available, false);
});

test('incomplete capability responses leave manual handoff usable and never permit a POST', () => {
  for (const raw of [null, 'invalid', [null, {}, false]]) {
    assert.deepEqual(webChannels(raw), []);
    assert.throws(() => webGenerationChannel(raw, 'router'), /未发送/);
  }
});

test('generate handler validates the selected route before POST and its request has no effort', () => {
  const app = fs.readFileSync(new URL('../sourceloom/static/processor.js', import.meta.url), 'utf8');
  const handler = app.slice(app.indexOf("$('#generate').addEventListener('click'"), app.indexOf("$('#result-markdown').addEventListener('input'"));
  assert.ok(handler.indexOf('webGenerationChannel(') < handler.indexOf("post(pidPath('/generate')"));
  assert.match(handler, /\{channel,request_id:requestId\}/);
  assert.doesNotMatch(handler, /effort|execution_channel|codex/);
});
