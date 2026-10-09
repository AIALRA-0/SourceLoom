import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {progressMarkup} from './historical_progress.js';

const running = current => ({
  progress: {
    steps: ['保存原件','清点信息','整理结构','改写正文','核对与修正','交付结果'],
    current,
    active: true,
    completed: false,
    label: '正在处理材料',
    started: 1000
  }
});

test('processing record is expanded and presents exactly five stages', () => {
  const html = progressMarkup(running(2));
  assert.match(html, /<section class="production-progress" aria-label="处理记录">/);
  assert.doesNotMatch(html, /<details|<summary/);
  assert.equal((html.match(/<li\b/g) || []).length, 5);
  assert.match(html, /aria-label="五个处理阶段"/);
  assert.match(html, /<li class="current" aria-current="step"><span>3<\/span>整理结构/);
});

test('internal delivery stage maps to visible stage five', () => {
  const html = progressMarkup(running(5));
  assert.match(html, /<li class="current" aria-current="step"><span>5<\/span>核对修正并交付/);
});

test('completed jobs mark all visible stages done', () => {
  const html = progressMarkup({progress: {...running(5).progress, active:false, completed:true}});
  assert.equal((html.match(/<li class="done"/g) || []).length, 5);
  assert.doesNotMatch(html, /aria-current="step"/);
});

test('current document and reading controls share layout rules within their scope', () => {
  const source = readFileSync(new URL('../sourceloom/static/processor_workbench.js', import.meta.url), 'utf8');
  assert.match(source, /const doc=el\('div','document-bar'\)/);
  assert.match(source, /left\.classList\.add\('pane-toolbar','source-toolbar'\)[^]*?const right=el\('div','pane-toolbar draft-toolbar'\)/);
  const styles = readFileSync(new URL('../sourceloom/static/processor_workbench.css', import.meta.url), 'utf8');
  assert.match(styles, /\.document-bar\s*\{[^}]*display:flex;[^}]*align-items:center/s);
  assert.match(styles, /\.pane-toolbar\s*\{[^}]*height:48px;[^}]*display:flex;[^}]*align-items:center/s);
  const sharedMenus=readFileSync(new URL('../sourceloom/static/processor_design.js',import.meta.url),'utf8');
  assert.match(sharedMenus,/e\.key!=='Escape'[^]*?\.tool-menu\[open\][^]*?last\.open=false/);
  assert.match(source, /ArrowLeft[^]*?ArrowRight[^]*?Home[^]*?End[^]*?\.focus\(\)/);
});

test('pending and unknown requests retain original identity and require an explicit new request', () => {
  const source = readFileSync(new URL('../sourceloom/static/processor.js', import.meta.url), 'utf8');
  assert.match(source, /\$\('#generate'\)\.disabled[^;]*pending[^]*?requests\(\)\.some\(isUnknown\)[^;]*另建一次独立模型请求/);
  assert.doesNotMatch(source, /\['planner','plan_review','writer'\]/);
});
