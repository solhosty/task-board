const assert = require('node:assert/strict');
const { renderChatText, renderHarnessEvents } = require('../static/chat-format.js');
assert.equal(renderChatText('Hello\nworld'), '<p>Hello<br>world</p>');
assert.match(
  renderChatText('## Summary\n\n**Done** with `code`'),
  /<h3>Summary<\/h3><p><strong>Done<\/strong> with <code>code<\/code><\/p>/,
);
assert.equal(
  renderChatText('```rust\nstruct Foo {\n}\n```'),
  '<pre><code>struct Foo {\n}</code></pre>',
);
assert.match(
  renderChatText('- one\n- two\n\nDone'),
  /<ul><li>one<\/li><li>two<\/li><\/ul><p>Done<\/p>/,
);
assert.equal(
  renderChatText('<script>alert(1)</script>'),
  '<p>&lt;script&gt;alert(1)&lt;/script&gt;</p>',
);
assert.equal(
  renderChatText('```\n<img onerror="alert(1)">'),
  '<pre><code>&lt;img onerror=&quot;alert(1)&quot;&gt;</code></pre>',
);
assert.equal(renderChatText('`**literal**`'), '<p><code>**literal**</code></p>');
console.log('PASS: 7 chat formatting checks, including HTML escaping and code blocks.');
const transcript = renderHarnessEvents(
  [
    { kind: 'message', text: 'I will inspect the code.', status: '' },
    {
      kind: 'reasoning',
      text: 'I will check the parser before changing it.',
      status: '',
      event_id: 'reason1',
    },
    {
      kind: 'command',
      text: 'cat src/main.rs',
      status: 'completed',
      output: '<script>not HTML</script>',
      event_id: 'tool1',
    },
    { kind: 'message', text: 'I found the issue. Here is the fix.', status: '' },
    { kind: 'message', text: 'Done.', status: '' },
  ],
  ['Done.'],
  4,
);
assert.ok(transcript.indexOf('I will inspect') < transcript.indexOf('<details'));
assert.match(
  transcript,
  /<\/details><\/details><div class="event-message message-content"><p>I found the issue/,
);
assert.ok(!transcript.includes('<script>'));
assert.ok(!transcript.includes('Done.'));
assert.match(transcript, /Reasoning summary/);
assert.match(transcript, /check the parser/);
console.log(
  'PASS: natural language remains visible around collapsed tool embeds; final reply is not duplicated.',
);
