"use strict";

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../web/message-format.js'), 'utf8');

function element(tag = 'div') {
  let text = '';
  const value = {
    tagName: tag.toUpperCase(), children: [], attributes: {}, events: {}, className: '',
    classList: { add(name) { value.className += ' ' + name; } },
    append(...nodes) { value.children.push(...nodes); },
    replaceChildren(...nodes) { text = ''; value.children = [...nodes]; },
    setAttribute(name, content) { assert.doesNotMatch(name, /^on/i); value.attributes[name] = String(content); },
    addEventListener(name, callback) { value.events[name] = callback; },
  };
  Object.defineProperty(value, 'textContent', { get: () => text, set: (content) => { text = String(content); value.children = []; } });
  Object.defineProperty(value, 'innerHTML', { get() { throw new Error('innerHTML is forbidden'); }, set() { throw new Error('innerHTML is forbidden'); } });
  return value;
}
function formatter() {
  const context = vm.createContext({ URL, document: { createElement: element } });
  vm.runInContext(source, context);
  return vm.runInContext('MessageFormat', context);
}
function nodes(root) { return [root, ...root.children.flatMap(nodes)]; }
function byTag(root, tag) { return nodes(root).filter((node) => node.tagName === tag.toUpperCase()); }
function visibleText(root) { return root.tagName === 'BR' ? '\n' : root.textContent + root.children.map(visibleText).join(''); }

test('paragraphs, line breaks, headings and emphasis use native elements and copy without Markdown markers', () => {
  const format = formatter(); const root = element();
  const text = '# 요약\n\n**굵게**와 *기울임*\n다음 줄의 __강조__ 와 _설명_';
  format.render(root, text);
  assert.equal(byTag(root, 'h1').length, 1); assert.equal(byTag(root, 'p').length, 1);
  assert.equal(byTag(root, 'strong').length, 2); assert.equal(byTag(root, 'em').length, 2);
  assert.equal(byTag(root, 'br').length, 1);
  assert.equal(format.plainText(text), '요약\n\n굵게와 기울임\n다음 줄의 강조 와 설명');
  assert.doesNotMatch(visibleText(root), /\*\*|__|# 요약/);
});

test('nested emphasis and inline code retain every code character, including comment and comparison symbols', () => {
  const format = formatter(); const root = element();
  const text = '**굵게와 *기울임*** / *기울임과 **굵게*** / ***둘 다*** / **`a>b**c`** / *`a/*b*/c`*';
  format.render(root, text);
  assert.equal(format.plainText(text), '굵게와 기울임 / 기울임과 굵게 / 둘 다 / a>b**c / a/*b*/c');
  assert.deepEqual(byTag(root, 'code').map((node) => node.textContent), ['a>b**c', 'a/*b*/c']);
  assert.equal(format.plainText('`` const x = `**literal**`; ``'), ' const x = `**literal**`; ');
  assert.equal(format.plainText('file_name_inside and a > b and **`**literal**`**'), 'file_name_inside and a > b and **literal**');
});

test('blockquote prefixes are structural rather than visible and quotes carry no copy buttons of their own', () => {
  const format = formatter(); const root = element(); const copied = [];
  const text = '> **첫 문장**\n> 다음 문장: a > b\n> > 중첩 인용\n';
  format.render(root, text, { onCopy: (content, kind) => copied.push([content, kind]) });
  assert.equal(byTag(root, 'blockquote').length, 2);
  assert.equal(format.plainText(text), '첫 문장\n다음 문장: a > b\n중첩 인용\n');
  assert.doesNotMatch(visibleText(root), /> 첫|> 다음|> 중첩/);
  // The message menu copies the whole message; only code keeps a small icon.
  assert.equal(byTag(root, 'button').length, 0);
  assert.equal(copied.length, 0);
});

test('fenced code and quoted code copies preserve tabs, spaces, CRLF, symbols and blank lines exactly', () => {
  const format = formatter(); const root = element(); const copied = [];
  const code = '\tif (a > b) { /* **comment** */ }\r\n\r\n  > literal\r\n';
  const text = '```js\r\n' + code + '```\r\n';
  format.render(root, text, { onCopy: (content, kind) => copied.push([content, kind]) });
  assert.equal(format.plainText(text), code); assert.equal(byTag(root, 'pre').length, 1);
  assert.equal(byTag(root, 'code')[0].textContent, code); assert.equal(byTag(root, 'code')[0].attributes['data-language'], 'js');
  byTag(root, 'button')[0].events.click(); assert.deepEqual(copied, [[code, 'code']]);
  const quoted = '> ```\r\n>   /* keep */ a > b\r\n> ```\r\n';
  assert.equal(format.plainText(quoted), '  /* keep */ a > b\r\n');
  assert.equal(format.plainText('~~~txt\n  *raw* > value\n'), '  *raw* > value\n');
});

test('ordered, unordered and nested lists preserve sequence and indentation in plain copies', () => {
  const format = formatter(); const root = element();
  const text = '- **첫 항목**\n  - 안쪽 항목\n- 두 번째\n\n3. 시작\n5) 다섯 번째\n';
  format.render(root, text);
  assert.equal(byTag(root, 'ul').length, 2); assert.equal(byTag(root, 'ol').length, 1);
  assert.equal(byTag(root, 'li').length, 5); assert.equal(byTag(root, 'ol')[0].attributes.start, '3');
  assert.deepEqual(byTag(root, 'li').slice(-2).map((node) => node.attributes.value), ['3', '5']);
  assert.equal(format.plainText(text), '• 첫 항목\n  • 안쪽 항목\n• 두 번째\n\n3. 시작\n5. 다섯 번째\n');
});

test('links retain their URLs in copies and only absolute HTTP or HTTPS destinations become anchors', () => {
  const format = formatter(); const root = element();
  const text = '[**문서**](https://docs.example/path_(v1)) [HTTP](http://example.test/) [위험](javascript:alert(1)) [상대](/login) [data](data:text/html,boom)';
  format.render(root, text);
  const links = byTag(root, 'a'); assert.equal(links.length, 2);
  assert.equal(links[0].attributes.href, 'https://docs.example/path_(v1)');
  assert.equal(links[0].attributes.target, '_blank'); assert.equal(links[0].attributes.rel, 'noopener noreferrer');
  assert.equal(format.plainText(text), '문서 (https://docs.example/path_(v1)) HTTP (http://example.test/) 위험 (javascript:alert(1)) 상대 (/login) data (data:text/html,boom)');
  for (const href of ['//example.test/', 'file:///tmp/example', 'https://example.test/\u0001x', 'java&#x73;cript:alert(1)']) {
    format.render(root, `[표시](${href})`); assert.equal(byTag(root, 'a').length, 0);
  }
});

test('raw HTML, scripts, SVG, handlers and images remain inert text rather than DOM capabilities', () => {
  const format = formatter(); const root = element();
  const payload = '<script>alert(1)</script> <img src=x onerror="alert(1)"> <svg onload=alert(1)> ![이미지](https://example.test/a.png)';
  format.render(root, payload);
  assert.equal(byTag(root, 'script').length, 0); assert.equal(byTag(root, 'img').length, 0); assert.equal(byTag(root, 'svg').length, 0);
  assert.equal(byTag(root, 'a').length, 0); assert.equal(visibleText(root), payload);
  assert.equal(format.plainText(payload), payload);
  assert.equal(nodes(root).some((node) => Object.keys(node.attributes).some((name) => /^on/.test(name))), false);
  assert.equal(source.includes('innerHTML'), false);
});

test('unfinished delimiters, escaped syntax and unsupported tables stay readable with no dropped content', () => {
  const format = formatter(); const root = element();
  const text = '**미완성\n[링크](미완성\n| a | b |\n| --- | --- |\n';
  format.render(root, text); assert.equal(format.plainText(text), text);
  assert.equal(format.plainText('\\*문자\\* 와 \\> 기호'), '*문자* 와 > 기호');
  assert.equal(format.plainText('####### 제목 아님'), '####### 제목 아님');
  format.render(root, '이전'); format.render(root, '새 내용'); assert.equal(visibleText(root), '새 내용');
});

test('large, deeply nested and adversarial inputs use a complete-text fallback with bounded DOM growth', { timeout: 1500 }, () => {
  const format = formatter();
  for (const text of ['**large**'.repeat(9000), '> quote\n'.repeat(1025), '> '.repeat(30) + 'nested', '*a '.repeat(17000)]) {
    const root = element(); format.render(root, text);
    assert.equal(root.children.length, 1); assert.equal(root.children[0].className, 'message-format-fallback');
    assert.equal(visibleText(root), text); assert.equal(format.plainText(text), text);
  }
  const html = '<!--'.repeat(16000); const root = element(); format.render(root, html);
  assert.equal(visibleText(root), html); assert.equal(format.plainText(html), html);
  assert.equal(nodes(root).length <= 3, true);
});

test('copy buttons are optional, fixed aria labels are used and rejected callbacks do not create unhandled promises', async () => {
  const format = formatter(); const root = element();
  format.render(root, '> 문장\n\n```\ncode\n```'); assert.equal(byTag(root, 'button').length, 0);
  format.render(root, '```\ncode\n```', { onCopy: () => Promise.reject(new Error('caller reports this')) });
  assert.equal(byTag(root, 'button')[0].attributes['aria-label'], '코드 블록 복사');
  byTag(root, 'button')[0].events.click(); await new Promise((resolve) => setImmediate(resolve));
});

test('a code block longer than 25 lines folds behind "모두 보기" with its line count, and the toggle unfolds and folds it', () => {
  const format = formatter(); const root = element();
  assert.equal(format.FOLD_LINES, 25);
  const lines = (count) => Array.from({ length: count }, (_, index) => `line ${index + 1}`).join('\n') + '\n';
  format.render(root, '```\n' + lines(25) + '```', { onCopy() {} });
  assert.equal(nodes(root).some((node) => /is-folded/.test(node.className)), false, 'exactly 25 lines stay open');
  assert.equal(byTag(root, 'button').length, 1, 'only the copy icon');
  format.render(root, '```js\n' + lines(26) + '```', { onCopy() {} });
  const block = nodes(root).find((node) => /message-code-block/.test(node.className));
  assert.match(block.className, /is-foldable is-folded/);
  const toggle = nodes(root).find((node) => node.className === 'message-code-toggle');
  assert.equal(toggle.textContent, '모두 보기 · 26줄');
  assert.equal(toggle.attributes['aria-expanded'], 'false');
  toggle.events.click();
  assert.doesNotMatch(block.className, /is-folded/);
  assert.equal(toggle.textContent, '접기'); assert.equal(toggle.attributes['aria-expanded'], 'true');
  toggle.events.click();
  assert.match(block.className, /is-folded/);
  assert.equal(toggle.textContent, '모두 보기 · 26줄');
  // Folding never changes what is copied, and the threshold is adjustable by the caller.
  assert.equal(byTag(root, 'code')[0].textContent, lines(26));
  format.render(root, '```\n' + lines(4) + '```', { foldAfter: 3 });
  assert.match(nodes(root).find((node) => /message-code-block/.test(node.className)).className, /is-folded/);
  // CRLF and a missing final newline count the same lines.
  format.render(root, '```\r\n' + lines(26).replace(/\n/g, '\r\n').replace(/\r\n$/, '') + '\r\n```');
  assert.equal(nodes(root).find((node) => node.className === 'message-code-toggle').textContent, '모두 보기 · 26줄');
});

test('code blocks are listed exactly, including quoted and list-nested fences, for the message menu', () => {
  const format = formatter();
  assert.deepEqual(Array.from(format.codeBlocks('설명\n\n```py\nprint(1)\n```\n\n> ```\n> a > b\n> ```\n')), ['print(1)\n', 'a > b\n']);
  assert.deepEqual(Array.from(format.codeBlocks('코드 없음')), []);
  assert.deepEqual(Array.from(format.codeBlocks('`인라인`만 있음')), [], 'inline code is not a block');
});
