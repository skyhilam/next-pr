"""Local page for task state, daemon health, and run logs. Loopback only."""
import json
import re
import sqlite3
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from . import engine
from .common import Blocked, lock
from .state import Store, config

try:
    from .providers import extract_contract as _extract_contract
except ImportError:
    _CONTRACT_STATUSES = {'completed', 'blocked', 'rate_limited', 'no_blockers', 'blockers'}

    def _extract_contract(text):
        """Same contract scan as providers.extract_contract, for trees that lack it."""
        if not isinstance(text, str):
            return None
        text = text.strip()
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*|\s*```$', '', text).strip()
        try:
            value = json.loads(text)
        except ValueError:
            value = None
        if isinstance(value, dict) and value.get('status') in _CONTRACT_STATUSES:
            return value
        found = None
        decoder = json.JSONDecoder()
        for match in re.finditer(r'\{', text):
            try:
                value, _end = decoder.raw_decode(text, match.start())
            except ValueError:
                continue
            if isinstance(value, dict) and value.get('status') in _CONTRACT_STATUSES:
                found = value
        return found

RUN_ID = re.compile(r'[0-9a-f]{16,64}')
STAGE_LABELS = {
    'queued': '排隊', 'handling': '拆任務', 'clarifying': '等你答', 'opening': '開 PR',
    'working': '寫作中', 'reading': '讀取中', 'coding': '寫作中', 'fixing': '修正中',
    'validation': '驗證', 'review': '等 CI', 'ready': '準備 CI', 'ci': '等 CI',
    'merging': '合併中', 'mergeable': '可以人手合', 'merged': '已合併',
    'answered': '已回覆', 'blocked': '停住', 'cancelled': '已取消',
}
STEP_OF = {
    'queued': 'queue', 'handling': 'split', 'clarifying': 'split',
    'opening': 'work', 'working': 'work', 'reading': 'work', 'coding': 'work',
    'fixing': 'work', 'validation': 'work', 'review': 'ci', 'ready': 'ci', 'ci': 'ci',
    'merging': 'merge', 'mergeable': 'merge', 'merged': 'merge', 'answered': 'merge',
}
NOTE_PHRASES = (
    ('No local validation command', '沒有本地驗證，等 CI 全綠先合'),
    ('Merge waits for all-green CI', '等 GitHub CI 全綠先合'),
    ('Waiting for dependencies to merge', '等前面的 PR 合併'),
    ('Waiting for the warm directory', '等工作目錄空出來'),
    ('repository validation argv is not configured', '未設定本地驗證'),
    ('CLI permission denied', '工具權限被拒'),
    ('Head changed before CI', '提交變咗，要重新對 CI'),
    ('child scope escapes the request', '子任務超出範圍'),
    ('child acceptance must be a string', '驗收格式不對'),
    ('worker did not emit the required final JSON contract', '回覆缺少結束合約'),
    ('CLI stopped on an unanswered prompt', '工具停喺問題沒有答'),
)
TERMINAL_STAGES = {'merged', 'cancelled', 'answered'}


def plain_note(note):
    text = note or ''
    for needle, label in NOTE_PHRASES:
        if needle in text:
            return label
    return text


def present_task(task):
    """Short label and stepper key. Waiting to open is still the queue, not writing."""
    stage = task.get('stage') or ''
    note = task.get('note') or ''
    anchor = task.get('resume_stage') if stage == 'blocked' and task.get('resume_stage') else stage
    if stage == 'opening' and ('Waiting for dependencies' in note or 'Waiting for the warm directory' in note):
        return '等前面', 'queue'
    if task.get('paused') and stage not in TERMINAL_STAGES | {'blocked'}:
        return '已暫停', STEP_OF.get(anchor, 'queue')
    return STAGE_LABELS.get(stage, stage), STEP_OF.get(anchor, 'queue')


PAGE = """<!doctype html>
<meta charset="utf-8">
<title>next-pr</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { height: 100%; }
  body { margin: 0; min-height: 100vh; display: flex; flex-direction: column;
    background: #121212; color: #ececec;
    font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "PingFang TC", "Noto Sans TC", sans-serif; }
  header, main { padding: 12px 16px; }
  main { flex: 1 1 auto; min-height: 0; width: 100%; max-width: 1120px; margin: 0 auto;
    display: flex; flex-direction: column; gap: 10px; }
  header { flex: 0 0 auto; display: flex; justify-content: space-between; gap: 16px; align-items: baseline;
    border-bottom: 1px solid #2c2c2c; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  #now { font-size: 16px; margin: 0; }
  #flash { margin: 0; }
  .muted { color: #9a9a9a; }
  .ok { color: #8fd48f; } .bad { color: #f0a0a0; } .wait { color: #e6c87a; }
  .workspace { flex: 1 1 auto; min-height: 0; display: flex; flex-direction: column; gap: 12px; }
  #tasks { flex: 1 1 auto; min-height: 0; overflow: auto; }
  #tasks article { background: #1b1b1b; border: 1px solid #333; border-radius: 10px; padding: 14px 16px; margin: 0 0 12px; }
  #tasks article.stop { border-color: #6e4040; }
  .top { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; }
  .title { font-size: 17px; }
  .badge { border: 1px solid #555; border-radius: 999px; padding: 2px 10px; font-size: 13px; white-space: nowrap; }
  .steps { display: flex; gap: 8px; list-style: none; padding: 0; margin: 12px 0; flex-wrap: wrap; }
  .steps li { color: #777; font-size: 13px; padding-top: 4px; border-top: 2px solid #333; min-width: 4.5em; }
  .steps li.done { color: #8fd48f; border-color: #3d6b3d; }
  .steps li.now { color: #fff; border-color: #d7c07a; font-weight: 650; }
  .steps li.stop { color: #f0a0a0; border-color: #a45; font-weight: 650; }
  .plain { margin: 8px 0; }
  .item { display: flex; justify-content: space-between; gap: 8px; align-items: center; padding: 6px 0; border-top: 1px solid #2a2a2a; }
  .item span { flex: 1 1 auto; min-width: 0; }
  button { background: #2a2a2a; color: inherit; border: 1px solid #555; border-radius: 6px; padding: 4px 10px; white-space: nowrap; flex: 0 0 auto; }
  button.primary { background: #31422c; border-color: #6d8f64; }
  .actions { margin-top: 10px; display: flex; gap: 8px; }
  #viewer[hidden] { display: none; }
  #viewer:not([hidden]) { flex: 1.4 1 0; min-height: 0; display: flex; flex-direction: column;
    background: #161616; border: 1px solid #2e2e2e; border-radius: 12px; }
  .viewer-bar { padding: 10px 14px; border-bottom: 1px solid #2a2a2a; align-items: center; }
  #viewer-body { flex: 1 1 auto; min-height: 0; overflow: auto; padding: 4px 14px 18px; }
  .msg { margin: 0; padding: 14px 0; border-bottom: 1px solid #262626; }
  .who { font-size: 12px; font-weight: 650; margin-bottom: 6px; }
  .msg-prompt .who { color: #e6c87a; }
  .msg-say .who { color: #b9cdf7; }
  .msg-tool .who { color: #9ec1ff; }
  .msg-error .who { color: #f0a0a0; }
  .md { color: #ececec; overflow-wrap: anywhere; }
  .md > :first-child { margin-top: 0; }
  .md h1, .md h2, .md h3, .md h4, .md h5, .md h6 { font-weight: 650; line-height: 1.3; margin: 0.75em 0 0.3em; }
  .md h1 { font-size: 1.35em; } .md h2 { font-size: 1.2em; } .md h3 { font-size: 1.08em; }
  .md h4, .md h5, .md h6 { font-size: 1em; }
  .md p { margin: 0.45em 0; }
  .md ul, .md ol { margin: 0.4em 0; padding-left: 1.3em; }
  .md li { margin: 0.15em 0; }
  .md a { color: #9ec1ff; }
  .md-inline { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    background: #2a2a2a; padding: 0.1em 0.35em; border-radius: 4px; font-size: 0.92em; }
  pre { margin: 0; font: 13px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
  .md-code { white-space: pre-wrap; word-break: break-word; overflow: auto; background: #0e0e0e;
    border: 1px solid #2c2c2c; border-radius: 8px; padding: 10px 12px; }
  .thought { color: #8a8a8a; }
  .thought summary, .msg-thought { color: #8a8a8a; }
  .term { background: #0e0e0e; border: 1px solid #2a2a2a; border-radius: 8px; padding: 8px 10px; }
  .term-call + .term-call { margin-top: 8px; padding-top: 8px; border-top: 1px solid #242424; }
  .term-cmd { color: #e4e4e4; white-space: pre-wrap; word-break: break-word; }
  .term-prompt { color: #8fd48f; }
  .term-arg { color: #e6c87a; }
  .term-out { margin-top: 6px; white-space: pre-wrap; word-break: break-word; color: #c8c8c8; }
  .term-fold summary { cursor: pointer; color: #9a9a9a; }
  .term-err { white-space: pre-wrap; word-break: break-word; color: #f0a0a0; background: #241616;
    border: 1px solid #6e4040; border-radius: 8px; padding: 10px 12px; }
  #activity { white-space: pre-wrap; max-height: 28vh; overflow: auto; background: #1b1b1b; padding: 10px; margin: 8px 0 0; }
  details.log { margin-top: 4px; }
  summary { cursor: pointer; }
  @media (min-width: 860px) {
    .workspace:has(#viewer:not([hidden])) { flex-direction: row; align-items: stretch; }
    .workspace:has(#viewer:not([hidden])) #tasks { flex: 0 0 340px; }
  }
  @media (max-width: 640px) {
    header { flex-direction: column; align-items: flex-start; }
    .top { flex-direction: column; }
    .viewer-bar { flex-direction: row; align-items: center; }
    .md-code, .term-cmd, .term-out, .term-err { font-size: 12px; }
  }
</style>
<header>
  <div><h1>next-pr</h1><div id="daemon" class="muted">讀取中</div></div>
  <div id="providers" class="muted"></div>
</header>
<main>
  <p id="now">讀取中</p>
  <p id="flash" class="muted"></p>
  <div class="workspace">
    <section id="viewer" hidden>
      <div class="top viewer-bar"><div class="title" id="viewer-title">對話</div>
        <button onclick="closeTalk()">關閉</button></div>
      <div id="viewer-body"></div>
    </section>
    <div id="tasks"></div>
  </div>
  <details class="log">
    <summary>協調器紀錄</summary>
    <pre id="activity" class="muted"></pre>
  </details>
</main>
<script>
const STEPS = [['排隊','queue'],['拆任務','split'],['寫作','work'],['等 CI','ci'],['合併','merge']];
const ROLE = {handle:'拆任務', code:'寫程式', design:'設計', accept:'驗收', integrate:'整合', review:'審查', validation:'驗證'};
const ITEM = {pending:'未開始', running:'進行中', done:'完成', blocked:'停住'};
let busy = false;
async function act(id, action) {
  if (busy) return;
  if (action === 'cancel' && !confirm('取消任務 ' + id + '？')) return;
  busy = true;
  const flash = document.getElementById('flash');
  flash.textContent = '處理中…';
  flash.className = 'wait';
  try {
    const response = await fetch('/tasks/' + id + '/' + action, {
      method: 'POST', signal: AbortSignal.timeout(8000)});
    const body = await response.json();
    flash.textContent = body.blocked || ('已更新：' + (body.stage || action));
    flash.className = body.blocked ? 'bad' : 'ok';
  } catch (error) {
    flash.textContent = '沒有回應：' + error;
    flash.className = 'bad';
  } finally {
    busy = false;
    refresh();
  }
}
const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({
  '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
let state = {runs: []};
let openRun = null;
let openKeys = new Set();
let talkSignature = '';
let talkTicket = 0;
function latestRun(taskId, role) {
  const matches = state.runs.filter(run => run.task_id === taskId && run.role === role);
  return matches.length ? matches[matches.length - 1] : null;
}
function inlineMarkdown(text) {
  const codes = [];
  let body = text.replace(/`([^`\\n]+)`/g, (_, code) => {
    codes.push('<code class="md-inline">' + code + '</code>');
    return '%%C' + (codes.length - 1) + '%%';
  });
  body = body.replace(/\\[([^\\]]+)\\]\\((https?:\\/\\/[^\\s)]+)\\)/g,
    '<a href="$2" rel="noopener noreferrer">$1</a>');
  body = body.replace(/\\*\\*([^*]+)\\*\\*/g, '<strong>$1</strong>');
  body = body.replace(/__([^_]+)__/g, '<strong>$1</strong>');
  body = body.replace(/\\*([^*]+)\\*/g, '<em>$1</em>');
  body = body.replace(/(^|[\\s])_([^_\\s][^_]*)_(?=$|[\\s])/g, '$1<em>$2</em>');
  return body.replace(/%%C(\\d+)%%/g, (_, index) => codes[Number(index)] || '');
}
function renderMarkdown(source) {
  const lines = esc(source).replace(/\\r\\n/g, '\\n').replace(/\\r/g, '\\n').split('\\n');
  const blocks = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.startsWith('```')) {
      const buf = [];
      index += 1;
      while (index < lines.length && !lines[index].startsWith('```')) {
        buf.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push('<pre class="md-code"><code>' + buf.join('\\n') + '</code></pre>');
      continue;
    }
    if (!line.trim()) { index += 1; continue; }
    const heading = /^(#{1,6})\\s+(.+)$/.exec(line);
    if (heading) {
      const level = heading[1].length;
      blocks.push('<h' + level + '>' + inlineMarkdown(heading[2]) + '</h' + level + '>');
      index += 1;
      continue;
    }
    if (/^\\s*[-*]\\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\\s*[-*]\\s+/.test(lines[index])) {
        items.push('<li>' + inlineMarkdown(lines[index].replace(/^\\s*[-*]\\s+/, '')) + '</li>');
        index += 1;
      }
      blocks.push('<ul>' + items.join('') + '</ul>');
      continue;
    }
    if (/^\\s*\\d+\\.\\s+/.test(line)) {
      const items = [];
      while (index < lines.length && /^\\s*\\d+\\.\\s+/.test(lines[index])) {
        items.push('<li>' + inlineMarkdown(lines[index].replace(/^\\s*\\d+\\.\\s+/, '')) + '</li>');
        index += 1;
      }
      blocks.push('<ol>' + items.join('') + '</ol>');
      continue;
    }
    const para = [];
    while (index < lines.length && lines[index].trim()
        && !lines[index].startsWith('```')
        && !/^(#{1,6})\\s+/.test(lines[index])
        && !/^\\s*[-*]\\s+/.test(lines[index])
        && !/^\\s*\\d+\\.\\s+/.test(lines[index])) {
      para.push(lines[index]);
      index += 1;
    }
    blocks.push('<p>' + para.map(inlineMarkdown).join('<br>') + '</p>');
  }
  return blocks.join('');
}
function callsOf(message) {
  if (Array.isArray(message.calls) && message.calls.length) return message.calls;
  const command = message.command || '';
  const output = message.output || '';
  return [{
    tool: message.tool || 'tool',
    command: command,
    output: output || (command ? '' : (message.text || ''))
  }];
}
function shellTool(name) {
  return /terminal|bash|shell/i.test(name || '');
}
function renderTool(message, index) {
  const calls = callsOf(message);
  const body = calls.map((call, callIndex) => {
    const tool = String(call.tool || 'tool');
    const command = String(call.command || '');
    const output = String(call.output || '');
    let head;
    if (shellTool(tool) && command) {
      head = '<div class="term-cmd"><span class="term-prompt">$ </span>' + esc(command) + '</div>';
    } else {
      head = '<div class="term-cmd"><span class="term-prompt">⏺ </span>' + esc(tool)
        + (command ? '<span class="term-arg"> ' + esc(command) + '</span>' : '') + '</div>';
    }
    let out = '';
    if (output) {
      const rows = output.split('\\n');
      const shown = '<pre class="term-out">' + esc(output) + '</pre>';
      const long = rows.length > 16 || output.length > 800;
      if (long) {
        const key = 'out-' + index + '-' + callIndex;
        const open = openKeys.has(key) ? ' open' : '';
        out = '<details class="term-fold" data-key="' + key + '"' + open + '>'
          + '<summary>輸出 ' + rows.length + ' 行</summary><div class="term-out-wrap">'
          + shown + '</div></details>';
      } else {
        out = shown;
      }
    }
    return '<div class="term-call">' + head + out + '</div>';
  }).join('');
  const title = message.title ? '<div class="who">' + esc(message.title) + '</div>' : '';
  return '<article class="msg msg-tool">' + title + '<div class="term">' + body + '</div></article>';
}
function renderMessage(message, index) {
  const kind = message.kind || 'say';
  if (kind === 'tool') return renderTool(message, index);
  if (kind === 'error') {
    return '<article class="msg msg-error"><div class="who">' + esc(message.title || '錯誤')
      + '</div><pre class="term-err">' + esc(message.text || '') + '</pre></article>';
  }
  const html = renderMarkdown(message.text || '');
  if (kind === 'thought') {
    const key = 'thought-' + index;
    const open = openKeys.has(key) ? ' open' : '';
    return '<article class="msg msg-thought"><details class="thought" data-key="' + key + '"' + open + '>'
      + '<summary>' + esc(message.title || '思考') + '</summary><div class="md">' + html
      + '</div></details></article>';
  }
  const tone = kind === 'prompt' ? 'msg-prompt' : 'msg-say';
  return '<article class="msg ' + tone + '"><div class="who">' + esc(message.title || '')
    + '</div><div class="md">' + html + '</div></article>';
}
function renderThread(messages) {
  if (!messages.length) return '<p class="muted">呢次運行未有對話。</p>';
  return messages.map(renderMessage).join('');
}
function closeTalk() {
  openRun = null;
  openKeys = new Set();
  talkSignature = '';
  document.getElementById('viewer').hidden = true;
  document.getElementById('viewer-body').innerHTML = '';
}
async function showTalk(id) {
  if (openRun !== id) {
    openKeys = new Set();
    talkSignature = '';
  }
  openRun = id;
  await loadTalk();
}
async function loadTalk() {
  const id = openRun;
  if (!id) return;
  const ticket = ++talkTicket;
  const viewer = document.getElementById('viewer');
  const log = document.getElementById('viewer-body');
  const response = await fetch('/runs/' + id + '/transcript');
  if (openRun !== id || ticket !== talkTicket) return;
  const body = await response.json();
  if (openRun !== id || ticket !== talkTicket) return;
  viewer.hidden = false;
  const run = state.runs.find(item => item.id === id);
  document.getElementById('viewer-title').textContent = run
    ? (ROLE[run.role] || run.role) + ' · ' + (run.when || '') : '對話';
  if (body.blocked) {
    talkSignature = '';
    log.textContent = body.blocked;
    return;
  }
  const messages = body.messages || [];
  const signature = JSON.stringify(messages);
  if (signature === talkSignature) return;
  const gap = log.scrollHeight - log.scrollTop - log.clientHeight;
  const follow = talkSignature !== '' && gap < 48;
  const scroll = log.scrollTop;
  const folded = new Set();
  log.querySelectorAll('details[data-key]').forEach(node => {
    if (node.open) folded.add(node.dataset.key);
  });
  openKeys = folded;
  talkSignature = signature;
  log.innerHTML = renderThread(messages);
  const running = !!(run && run.exit_code === null);
  if (running && follow) log.scrollTop = log.scrollHeight;
  else log.scrollTop = scroll;
}
document.getElementById('viewer-body').addEventListener('toggle', event => {
  const node = event.target;
  if (!node || !node.dataset || !node.dataset.key) return;
  if (!event.currentTarget.contains(node)) return;
  if (node.open) openKeys.add(node.dataset.key);
  else openKeys.delete(node.dataset.key);
}, true);
function taskCard(task) {
  const here = STEPS.findIndex(step => step[1] === task.step);
  const steps = STEPS.map((step, index) => {
    let cls = '';
    if (task.stage === 'merged' || task.stage === 'answered') cls = 'done';
    else if (task.stage === 'blocked' && index === here) cls = 'stop';
    else if (here >= 0 && index < here) cls = 'done';
    else if (index === here) cls = 'now';
    return '<li class="' + cls + '">' + step[0] + '</li>';
  }).join('');
  const items = (task.items || []).map(item => {
    const run = latestRun(task.id, item.role);
    const talk = run ? '<button onclick="showTalk(\\'' + run.id + '\\')">對話</button>' : '';
    return '<div class="item"><span>' + esc(ITEM[item.status] || item.status) + ' · '
      + esc(ROLE[item.role] || item.role) + ' · ' + esc(item.title) + '</span>' + talk + '</div>';
  }).join('');
  const pr = task.pr_url ? '<a href="' + esc(task.pr_url) + '">PR ' + esc(task.pr_number) + '</a>' : '';
  const terminal = ['merged','cancelled','answered'].includes(task.stage);
  let actions = '';
  if (task.stage === 'blocked' || task.paused)
    actions += '<button class="primary" onclick="act(\\'' + task.id + '\\',\\'resume\\')">繼續</button>';
  if (!terminal && task.stage !== 'blocked')
    actions += '<button onclick="act(\\'' + task.id + '\\',\\'pause\\')">暫停</button>';
  if (!terminal)
    actions += '<button onclick="act(\\'' + task.id + '\\',\\'cancel\\')">取消</button>';
  const badge = task.stage === 'blocked' || task.stage === 'cancelled' ? 'bad'
    : task.stage === 'merged' || task.stage === 'answered' ? 'ok' : 'wait';
  return '<article class="' + (task.stage === 'blocked' ? 'stop' : '') + '"><div class="top"><div>'
    + '<div class="title">' + esc(task.title) + '</div><div class="muted">' + pr
    + (pr ? ' · ' : '') + esc(task.id.slice(0, 8)) + '</div></div>'
    + '<div class="badge ' + badge + '">' + esc(task.stage_label) + '</div></div>'
    + '<ol class="steps">' + steps + '</ol>'
    + (task.plain_note ? '<p class="plain">' + esc(task.plain_note) + '</p>' : '')
    + items
    + '<div class="actions">' + actions + '</div></article>';
}
function nowLine(tasks) {
  const live = tasks.filter(task => !['merged','cancelled','answered'].includes(task.stage));
  if (!live.length) return '沒有進行中的任務';
  return live.map(task => task.title + '，' + (task.plain_note || task.stage_label)).join('。') + '。';
}
async function refresh() {
  state = await (await fetch('/api/state')).json();
  const daemon = document.getElementById('daemon');
  daemon.textContent = state.daemon ? (state.paused ? '協調器已暫停' : '協調器運行中') : '協調器未運行';
  daemon.className = state.daemon && !state.paused ? 'ok' : 'bad';
  const quiet = state.providers.filter(item => item.enabled && !item.billing_confirmed);
  const held = state.providers.filter(item => item.paused);
  document.getElementById('providers').textContent = quiet.length || held.length
    ? quiet.map(item => item.name + ' 未確認').concat(held.map(item => item.name + ' 暫停')).join(' · ')
    : '工具就緒';
  document.getElementById('now').textContent = nowLine(state.tasks);
  document.getElementById('tasks').innerHTML = state.tasks.map(taskCard).join('') || '<p class="muted">沒有任務</p>';
  document.getElementById('activity').textContent = state.activity || '未有紀錄';
  const live = state.runs.find(item => item.id === openRun);
  if (live && live.exit_code === null) loadTalk();
}
refresh();
setInterval(refresh, 2000);
</script>
"""


def daemon_running(home):
    path = Path(home) / 'daemon.lock'
    if not path.exists():
        return False
    import fcntl
    handle = path.open('a+')
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False
    finally:
        handle.close()


def snapshot(store):
    pauses = store.meta('provider_pauses', {})
    cfg = config(store.home)
    tasks = []
    for task in store.tasks():
        label, step = present_task(task)
        pr_url = task.get('pr_url')
        if not pr_url and task.get('pr_number'):
            pr_url = f'https://github.com/{task["repo"]}/pull/{task["pr_number"]}'
        tasks.append({
            'id': task['id'], 'title': task['title'], 'repo': task['repo'], 'stage': task['stage'],
            'stage_label': label, 'step': step, 'paused': bool(task.get('paused')),
            'note': task.get('note') or '', 'plain_note': plain_note(task.get('note') or ''),
            'pr_number': task.get('pr_number'), 'pr_url': pr_url,
            'items': [{'id': item.get('id'), 'role': item.get('role'), 'title': item.get('title'),
                       'status': item.get('status')} for item in task.get('items') or []],
        })
    runs = []
    for run in sorted(store.runs(), key=lambda item: item.get('created_at') or 0):
        receipt = run.get('receipt') or {}
        runs.append({
            'id': run['id'], 'task_id': run['task_id'], 'role': run['role'], 'provider': run['provider'],
            'exit_code': receipt.get('exit_code'),
            'when': time_label(run.get('created_at')),
        })
    activity = store.home / 'activity.log'
    text = activity.read_text(errors='replace') if activity.exists() else ''
    return {
        'daemon': daemon_running(store.home),
        'paused': store.meta('paused', False),
        'providers': [{'name': name, 'enabled': bool(item.get('enabled')),
                       'billing_confirmed': bool(item.get('billing_confirmed')),
                       'paused': bool(pauses.get(name))}
                      for name, item in cfg['providers'].items()],
        'tasks': tasks,
        'runs': runs,
        'activity': '\n'.join(text.splitlines()[-40:]),
    }


def time_label(value):
    import time
    if not value:
        return ''
    return time.strftime('%H:%M:%S', time.localtime(value))


def run_dir(home, run_id):
    if not RUN_ID.fullmatch(run_id or ''):
        raise Blocked('unknown log')
    path = (Path(home) / 'runs' / run_id).resolve()
    if Path(home).resolve() not in path.parents:
        raise Blocked('unknown log')
    return path


def log_text(home, run_id, which):
    if which not in {'stdout', 'stderr'}:
        raise Blocked('unknown log')
    path = (run_dir(home, run_id) / f'{which}.log')
    if not path.is_file():
        return ''
    return '\n'.join(path.read_text(errors='replace').splitlines()[-200:])


def _clip(value, limit=2000):
    text = value if isinstance(value, str) else ''
    return text if len(text) <= limit else text[:limit] + '\n…已截短'


def _tool_input(raw):
    if not isinstance(raw, dict):
        return ''
    for key in ('target_file', 'path', 'file_path', 'command', 'cmd', 'pattern', 'query', 'url'):
        if isinstance(raw.get(key), str):
            return raw[key]
    return _clip(json.dumps(raw, ensure_ascii=False), 300)


def _tool_result(event):
    content = event.get('content')
    if isinstance(content, list):
        bits = [item.get('content') for item in content
                if isinstance(item, dict) and isinstance(item.get('content'), str)]
        if bits:
            return _clip('\n'.join(bits))
    raw = event.get('rawOutput')
    if isinstance(raw, dict):
        file_body = raw.get('FileContent')
        if isinstance(file_body, dict) and isinstance(file_body.get('content'), str):
            return _clip(file_body['content'])
        return _clip(json.dumps(raw, ensure_ascii=False))
    if isinstance(raw, str):
        return _clip(raw)
    return ''


def _events(text):
    try:
        whole = json.loads(text)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        return [whole]
    events = []
    for line in text.splitlines():
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _contract_text(result):
    value = _extract_contract(result)
    if not isinstance(value, dict) or not isinstance(value.get('summary'), str):
        return None
    lines = [value['summary']]
    for child in value.get('children') or []:
        if isinstance(child, dict):
            lines.append(f"{child.get('role') or ''} · {child.get('title') or ''}")
    return '\n'.join(lines)


def _call_record(item):
    """Tool name, command or input, and output stay three separate fields."""
    tool = item.get('tool') or item['title'].split('·')[-1].strip() or 'tool'
    return {'tool': tool, 'command': item.get('command', ''), 'output': item.get('output', '')}


def _group_tools(messages):
    """One row for a run of tool calls. A single call stays as its own row."""
    grouped, bucket = [], []

    def flush_bucket():
        if len(bucket) == 1:
            grouped.append(bucket[0])
        elif bucket:
            counts = {}
            for item in bucket:
                name = item['title'].split('·')[-1].strip() or 'tool'
                counts[name] = counts.get(name, 0) + 1
            summary = '、'.join(f'{name} {count}' for name, count in counts.items())
            text = '\n\n'.join(f"{item['title']}\n{item['text']}".strip() for item in bucket)
            grouped.append({'kind': 'tool', 'title': f'工具 {len(bucket)} 次 · {summary}', 'text': text,
                            'calls': [_call_record(item) for item in bucket]})
        bucket.clear()

    for message in messages:
        if message['kind'] == 'tool':
            bucket.append(message)
        else:
            flush_bucket()
            grouped.append(message)
    flush_bucket()
    return grouped


def _dialogue(text):
    events = _events(text)
    if len(events) == 1 and isinstance(events[0].get('result'), str):
        spoken = _contract_text(events[0]['result']) or events[0]['result']
        return [{'kind': 'say', 'title': '回覆', 'text': spoken}]
    messages, tools, current = [], {}, None

    def flush():
        nonlocal current
        if current and current['parts']:
            messages.append({'kind': current['kind'], 'title': current['title'],
                             'text': ''.join(current['parts'])})
        current = None

    for event in events:
        kind = event.get('type')
        if kind == 'thought' and isinstance(event.get('data'), str):
            if not current or current['kind'] != 'thought':
                flush()
                current = {'kind': 'thought', 'title': '思考', 'parts': []}
            current['parts'].append(event['data'])
        elif kind == 'text' and isinstance(event.get('data'), str):
            if not current or current['kind'] != 'say':
                flush()
                current = {'kind': 'say', 'title': '回覆', 'parts': []}
            current['parts'].append(event['data'])
        elif kind == 'tool_call':
            flush()
            text_input = _tool_input(event.get('rawInput'))
            tool = str(event.get('toolName') or 'tool')
            call = {'tool': tool, 'command': text_input, 'output': ''}
            messages.append({'kind': 'tool', 'title': '工具 · ' + tool, 'text': text_input,
                             'tool': tool, 'command': text_input, 'output': '', 'calls': [call]})
            if event.get('toolCallId'):
                tools[event['toolCallId']] = len(messages) - 1
        elif kind == 'tool_call_update' and event.get('status') == 'completed':
            result = _tool_result(event)
            slot = tools.get(event.get('toolCallId'))
            if result and slot is not None:
                messages[slot]['text'] = (messages[slot]['text'] + '\n\n' + result).strip()
                previous = messages[slot].get('output') or ''
                output = (previous + '\n\n' + result).strip() if previous else result
                messages[slot]['output'] = output
                if messages[slot].get('calls'):
                    messages[slot]['calls'][-1]['output'] = output
        elif kind == 'error':
            flush()
            messages.append({'kind': 'error', 'title': '錯誤', 'source': 'model',
                             'text': str(event.get('message') or event)})
    flush()
    if messages:
        for message in messages:
            if message['kind'] == 'say':
                spoken = _contract_text(message['text'])
                if spoken:
                    message['text'] = spoken
        return _group_tools(messages)
    stripped = text.strip()
    if stripped:
        return [{'kind': 'say', 'title': '回覆', 'text': _clip(stripped, 8000)}]
    return []


def transcript(home, run_id):
    """Readable prompt, speech, and tool calls for one run. Raw logs stay on disk."""
    directory = run_dir(home, run_id)
    manifest = {}
    manifest_path = directory / 'manifest.json'
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text())
        except ValueError:
            manifest = {}
    messages = []
    prompt = directory / 'prompt.txt'
    if prompt.is_file():
        messages.append({'kind': 'prompt', 'title': '交給 agent', 'text': prompt.read_text(errors='replace')})
    stdout = (directory / 'stdout.log').read_text(errors='replace') if (directory / 'stdout.log').is_file() else ''
    messages.extend(_dialogue(stdout))
    stderr_path = directory / 'stderr.log'
    if stderr_path.is_file():
        stderr = stderr_path.read_text(errors='replace').strip()
        if stderr:
            messages.append({'kind': 'error', 'title': 'stderr', 'source': 'stderr',
                             'text': _clip(stderr, 4000)})
    return {'id': run_id, 'provider': manifest.get('provider') or '',
            'role': manifest.get('role') or '', 'messages': messages}


def act(store, task_id, action, attempts=20):
    """Take state.lock without waiting out a stuck coordinator. attempts=1 is for tests."""
    if action not in {'pause', 'resume', 'cancel'}:
        raise Blocked('unknown action')
    path = store.home / 'state.lock'
    held = f'lock held: {path.name}'
    for attempt in range(attempts):
        try:
            with lock(path, blocking=False):
                cfg = config(store.home)
                task = store.task(task_id)
                if action == 'resume':
                    engine.resume_task(store, cfg, task, None)
                else:
                    engine.stop_task(store, task, cancel=action == 'cancel')
                return {'ok': True, 'stage': store.task(task_id)['stage']}
        except Blocked as error:
            if str(error) != held:
                raise
            if attempt + 1 < attempts:
                time.sleep(0.1)
    raise Blocked('協調器忙碌，請再按一次')


class Handler(BaseHTTPRequestHandler):
    home = None

    def log_message(self, *_args):
        return

    def finish_json(self, build, status=200):
        store = Store(self.home)
        try:
            self.send_json(build(store), status)
        except (Blocked, ValueError, KeyError, sqlite3.OperationalError) as error:
            message = '協調器忙碌，請再按一次' if isinstance(error, sqlite3.OperationalError) else str(error)
            self.send_json({'blocked': message}, 400)
        finally:
            store.close()

    def send_json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == '/':
            body = PAGE.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif parsed.path == '/api/state':
            self.finish_json(snapshot)
        elif parsed.path.startswith('/runs/'):
            _runs, run_id, which = parsed.path.split('/')[1:]
            if which == 'transcript':
                self.finish_json(lambda store: transcript(store.home, run_id))
            else:
                self.finish_json(lambda store: {'text': log_text(store.home, run_id, which)})
        else:
            self.send_json({'blocked': 'not found'}, 404)

    def do_POST(self):
        parts = urlparse(self.path).path.split('/')
        if len(parts) == 4 and parts[1] == 'tasks':
            self.finish_json(lambda store: act(store, parts[2], parts[3]))
        else:
            self.send_json({'blocked': 'not found'}, 404)


def serve(home, host, port):
    if host != '127.0.0.1':
        raise Blocked('the status page listens on 127.0.0.1 only')
    Handler.home = home
    server = ThreadingHTTPServer((host, port), Handler)
    print(f'http://{host}:{port}/', flush=True)
    server.serve_forever()
