// Exercise the shipped page against a snapshot and transcripts from a real Store.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const elements = new Map();
const requests = [];
const context = vm.createContext({
  document: {
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, {
        innerHTML: '', textContent: '', hidden: false, scrollTop: 0,
        scrollHeight: 100, clientHeight: 100,
        addEventListener() {}, querySelectorAll() { return []; },
      });
      return elements.get(id);
    },
  },
  matchMedia: () => ({matches: false}),
  fetch: async path => {
    requests.push(path);
    const id = path.split('/')[2];
    assert.ok(input.logs[id], 'requested a real run');
    return {json: async () => input.logs[id]};
  },
  snapshot: input.state,
});
const script = input.page.split('<script>')[1].split('</script>')[0];
vm.runInContext(script.replace('refresh();\nsetInterval(refresh, 2000);', ''), context);
vm.runInContext('state = snapshot', context);

async function check() {
  const row = vm.runInContext('taskRow(state.tasks[0])', context);
  assert.equal((row.match(/>對話<\/button>/g) || []).length, 2);
  assert.ok(row.includes('寫程式 · codex · 已完成'));
  assert.ok(row.includes('寫程式 · codex · 運行中'));
  assert.ok(row.includes('暫停') && row.includes('取消'));
  assert.ok(row.indexOf(input.state.runs[1].id) < row.indexOf(input.state.runs[0].id));
  assert.ok(!vm.runInContext('taskRow(state.tasks[1])', context).includes('>對話<'));
  for (const run of input.state.runs) {
    const button = row.match(new RegExp(`onclick="(showTalk\\('${run.id}'\\))"`));
    assert.ok(button, 'each run has a working conversation button');
    await vm.runInContext(button[1], context);
    assert.equal(requests.at(-1), `/runs/${run.id}/transcript`);
    assert.equal(elements.get('viewer-title').textContent, input.state.tasks[0].title);
    const thread = elements.get('viewer-body').innerHTML;
    assert.ok(thread.includes('<h1>Request</h1>'));
    assert.ok(thread.includes('<ul><li>Keep <code class="md-inline">code</code></li></ul>'));
    assert.ok(thread.includes('<h2>Reply</h2>'));
    assert.equal(elements.get('viewer-close').hidden, false);
  }
  const markdown = vm.runInContext('renderMarkdown("1. one\\n2. two\\n\\n```js\\n<script>\\n```\\n\\n<b>raw</b>")', context);
  assert.ok(markdown.includes('<ol><li>one</li><li>two</li></ol>'));
  assert.ok(markdown.includes('<pre class="md-code"><code>&lt;script&gt;</code></pre>'));
  assert.ok(markdown.includes('&lt;b&gt;raw&lt;/b&gt;'));
  const terminal = vm.runInContext(`renderMessage({kind:'tool', calls:[
    {tool:'shell', command:'echo <hi>', output:'<hi>'},
    {tool:'read_file', command:'a.py', output:'contents'}]}, 0)`, context);
  assert.ok(terminal.includes('<span class="term-prompt">$ </span>echo &lt;hi&gt;'));
  assert.ok(terminal.includes('<pre class="term-out">&lt;hi&gt;</pre>'));
  assert.ok(terminal.includes('read_file') && terminal.includes('contents'));
  assert.ok(vm.runInContext("renderMessage({kind:'error', text:'<failure>'}, 1)", context)
    .includes('<pre class="term-err">&lt;failure&gt;</pre>'));
  vm.runInContext('closeTalk()', context);
  assert.equal(elements.get('viewer-close').hidden, true);
  console.log('Store run entries, conversation loading, Markdown and terminal checks passed');
}
check().catch(error => { console.error(error); process.exitCode = 1; });
