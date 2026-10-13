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

test('a pipe table draws as a table that scrolls on its own, and copies as it was written', () => {
  const format = formatter(); const root = element();
  const text = '비교 결과입니다.\n| 상품 | 쿠팡 **정가** | `a|b` |\n|:---|---:|:-:|\n| 앰플 | 32,000 | 링크 [여기](https://example.test/a) |\n| 크림 | 18,500 |\n\n끝.';
  format.render(root, text);
  const [wrap] = nodes(root).filter((node) => node.className === 'message-table-wrap');
  assert.ok(wrap, 'a table wrapper scrolls sideways');
  assert.equal(byTag(root, 'table').length, 1);
  assert.deepEqual(byTag(root, 'th').map(visibleText), ['상품', '쿠팡 정가', 'a|b']);
  assert.deepEqual(byTag(root, 'th').map((node) => node.attributes['data-align']), ['left', 'right', 'center']);
  assert.deepEqual(byTag(root, 'td').map(visibleText), ['앰플', '32,000', '링크 여기', '크림', '18,500', '']);
  assert.equal(byTag(root, 'strong').length, 1); assert.equal(byTag(root, 'a').length, 1);
  assert.equal(visibleText(byTag(root, 'p')[0]), '비교 결과입니다.', 'the paragraph before it stays a paragraph');
  assert.equal(format.plainText(text), text, 'a table copies as the Markdown it was');
  // An unpaired backtick in a cell is text: the cells after it stay apart.
  format.render(root, "| a | b | c |\n|---|---|---|\n| it`s | 둘 | 셋 |\n| 넷 | `x|y` | 다섯 |");
  assert.deepEqual(byTag(root, 'td').map(visibleText), ['it`s', '둘', '셋', '넷', 'x|y', '다섯']);
  // A `<br>` in a cell breaks its line there; inside a code span it stays text.
  const broken = '| 항목 | 설명 |\n|---|---|\n| 첫째<br>둘째<BR/>셋째 | `x<br>y` |';
  format.render(root, broken);
  const [first, second] = byTag(root, 'td');
  assert.equal(byTag(first, 'br').length, 2); assert.equal(visibleText(first), '첫째\n둘째\n셋째');
  assert.equal(byTag(second, 'br').length, 0); assert.equal(visibleText(second), 'x<br>y');
  assert.equal(format.plainText(broken), broken);
  // Long runs a pattern would backtrack on are read in one pass.
  const started = Date.now();
  format.render(root, 'a|b\n---' + ' '.repeat(60000) + 'x');
  assert.equal(byTag(root, 'table').length, 0);
  format.render(root, Array.from({ length: 31 }, () => 'http://a.test' + ')'.repeat(2030)).join(' '));
  assert.equal(byTag(root, 'a')[0].attributes.href, 'http://a.test/');
  assert.ok(Date.now() - started < 1000, 'linear');
  // Not a table: no delimiter row, a column count that differs, inside a code block.
  for (const plain of ['a | b\nc | d', '| a | b |\n| --- |\n| c | d |', '```\n| a | b |\n|---|---|\n```']) {
    format.render(root, plain);
    assert.equal(byTag(root, 'table').length, 0, plain);
  }
});

test('web addresses become links, without trailing punctuation or the particle after them', () => {
  const format = formatter(); const root = element();
  const text = '배포 주소: https://example.test/path?q=1 입니다. PR은 https://github.com/o/r/pull/12에서, (https://a.test/x) 그리고 <https://b.test/y>.';
  format.render(root, text);
  assert.deepEqual(byTag(root, 'a').map((node) => node.attributes.href),
    ['https://example.test/path?q=1', 'https://github.com/o/r/pull/12', 'https://a.test/x', 'https://b.test/y']);
  assert.ok(byTag(root, 'a').every((node) => node.attributes.target === '_blank' && /noopener/.test(node.attributes.rel)));
  assert.equal(format.plainText(text), text.replace('<https://b.test/y>', 'https://b.test/y'), 'copied once, as written');
  // Not inside a code span, a link label or an image; never a javascript: address.
  format.render(root, '`https://code.test/x` [https://label.test](https://dest.test) ![그림](https://img.test/a.png) javascript:alert(1)');
  assert.deepEqual(byTag(root, 'a').map((node) => node.attributes.href), ['https://dest.test/']);
  // Korean where a part of the address begins is the address; right after ASCII it is the particle.
  format.render(root, 'https://ko.wikipedia.org/wiki/대한민국 문서, https://a.test/경로/b?q=한글&x=1 그리고 https://c.test/12에서 끝.');
  assert.deepEqual(byTag(root, 'a').map(visibleText),
    ['https://ko.wikipedia.org/wiki/대한민국', 'https://a.test/경로/b?q=한글&x=1', 'https://c.test/12']);
  assert.equal(byTag(root, 'a')[0].attributes.href, 'https://ko.wikipedia.org/wiki/%EB%8C%80%ED%95%9C%EB%AF%BC%EA%B5%AD');
  // A particle alone after a part's start is the sentence's; other scripts are not taken in.
  format.render(root, 'http://localhost:8447/에서 확인, https://d.test/repo/를 열고 https://e.test/?y=에서. https://f.test/путь 끝');
  assert.deepEqual(byTag(root, 'a').map(visibleText),
    ['http://localhost:8447/', 'https://d.test/repo/', 'https://e.test/?y=', 'https://f.test/']);
});

test('rules, task items and struck text draw as such and copy as written', () => {
  const format = formatter(); const root = element();
  const text = '위\n---\n- [ ] 할 일\n- [x] 끝난 일\n- 보통 항목\n\n~~취소~~ 그리고 ~ 물결 ~';
  format.render(root, text);
  assert.equal(byTag(root, 'hr').length, 1);
  assert.deepEqual(byTag(root, 'li').map((node) => node.attributes['data-task'] || ''), ['open', 'done', '']);
  assert.deepEqual(nodes(root).filter((node) => /message-task/.test(node.className || '')).map(visibleText), ['☐', '☑']);
  assert.deepEqual(byTag(root, 'del').map(visibleText), ['취소']);
  assert.match(visibleText(root), /~ 물결 ~/);
  assert.equal(format.plainText('~~취소~~'), '취소');
  // A "~~" inside code does not end the strike.
  format.render(root, '~~`a~~b` 전체~~ 뒤');
  assert.deepEqual(byTag(root, 'del').map(visibleText), ['a~~b 전체']);
  assert.deepEqual(byTag(root, 'code').map(visibleText), ['a~~b']);
  assert.equal(format.plainText('위\n---\n아래'), '위\n---\n아래');
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

test('B1: a link takes only its own bracket, so "[P1] …", "a[0]" and "[redacted]" before it stay text', () => {
  const format = formatter(); const root = element();
  for (const [text, before] of [['[P1] x [a](https://e.com)', '[P1] x '], ['a[0] [a](https://e.com)', 'a[0] '],
    ['[redacted] 와 [a](https://e.com)', '[redacted] 와 '], ['[참고] 자세한 내용은 [a](https://e.com)를', '[참고] 자세한 내용은 ']]) {
    format.render(root, text);
    const links = byTag(root, 'a');
    assert.equal(links.length, 1, text); assert.equal(visibleText(links[0]), 'a', text);
    assert.equal(visibleText(root).startsWith(before), true, text);
    assert.equal(format.plainText(text).startsWith(before + 'a (https://e.com'), true, text);
  }
  format.render(root, '[a [b] c](https://e.com) 와 [x\\]y](https://e.com)');
  assert.deepEqual(byTag(root, 'a').map(visibleText), ['a [b] c', 'x]y']);
  format.render(root, '결과: [P1] 실패 (원인 미상)'); assert.equal(byTag(root, 'a').length, 0);
  assert.equal(visibleText(root), '결과: [P1] 실패 (원인 미상)');
});

test('B3: list items keep continuation lines, blank-separated items, sub-items and indented fences together', () => {
  const format = formatter(); const root = element(); const copied = [];
  const continued = '1. **첫 단계**\n   설명 줄입니다\n2. 두 번째';
  format.render(root, continued);
  assert.equal(byTag(root, 'ol').length, 1); assert.equal(byTag(root, 'li').length, 2);
  assert.equal(byTag(root, 'p').length, 0, 'the continuation is not a paragraph outside the list');
  assert.equal(visibleText(byTag(root, 'li')[0]), '첫 단계\n설명 줄입니다');
  assert.equal(format.plainText(continued), '1. 첫 단계\n   설명 줄입니다\n2. 두 번째');
  const loose = '- 하나\n\n- 둘\n\n  - 둘의 안쪽\n\n끝';
  format.render(root, loose);
  assert.equal(byTag(root, 'ul').length, 2, 'one list and its sub-list, not three lists');
  assert.equal(visibleText(byTag(byTag(root, 'li')[1], 'ul')[0]), '둘의 안쪽');
  assert.equal(format.plainText(loose), '• 하나\n\n• 둘\n\n  • 둘의 안쪽\n\n끝');
  for (const [text, code] of [['1. 설치\n   ```bash\n   npm install\n   ```\n2. 실행', 'npm install\n'],
    ['- 상위\n    ```js\n    const a = 1;\n    ```\n- 다음', 'const a = 1;\n'],
    ['  ```\n  들여쓴 펜스\n    안쪽\n  ```', '들여쓴 펜스\n  안쪽\n']]) {
    copied.length = 0;
    format.render(root, text, { onCopy: (content) => copied.push(content) });
    assert.equal(byTag(root, 'code')[0].textContent, code, text);
    assert.doesNotMatch(visibleText(root), /```/, text);
    byTag(root, 'button')[0].events.click(); assert.deepEqual(copied, [code]);
    assert.deepEqual(Array.from(format.codeBlocks(text)), [code]);
  }
  assert.equal(byTag((format.render(root, '1. 설치\n   ```bash\n   npm i\n   ```\n2. 실행'), root), 'ol').length, 1);
  // A list ends at a blank line that something else follows.
  format.render(root, '- a\n\n문단'); assert.equal(byTag(root, 'p').length, 1); assert.equal(visibleText(byTag(root, 'li')[0]), 'a');
});

test('B9: only a list starting at 1 interrupts a paragraph, Korean dates never start a list and long numbers get room', () => {
  const format = formatter(); const root = element();
  format.render(root, '오늘 일정\n2026. 10. 10. 회의 정리\n다음 줄');
  assert.equal(byTag(root, 'ol').length, 0); assert.equal(byTag(root, 'p').length, 1);
  format.render(root, '2026. 10. 10. 회의 정리'); assert.equal(byTag(root, 'ol').length, 0);
  format.render(root, '설명\n1000. 항목'); assert.equal(byTag(root, 'ol').length, 0);
  format.render(root, '설명\n1. 하나\n2. 둘'); assert.equal(byTag(root, 'ol').length, 1);
  format.render(root, '1000. 천 번째\n1001. 다음');
  assert.equal(byTag(root, 'ol')[0].attributes.start, '1000'); assert.equal(byTag(root, 'ol')[0].attributes['data-digits'], '4');
  format.render(root, '1. 하나'); assert.equal(byTag(root, 'ol')[0].attributes['data-digits'], undefined);
  const css = fs.readFileSync(path.join(__dirname, '../web/style.css'), 'utf8');
  assert.match(css, /\.message-markdown ol\[data-digits="4"\]/);
});

test('B11·B12: nested emphasis closes on its own marker and a one-line ```code``` is inline code, not a fence', () => {
  const format = formatter(); const root = element();
  format.render(root, '*기울임 **굵게** 끝*');
  assert.equal(byTag(root, 'em').length, 1); assert.equal(byTag(byTag(root, 'em')[0], 'strong').length, 1);
  assert.equal(format.plainText('*기울임 **굵게** 끝*'), '기울임 굵게 끝');
  assert.equal(format.plainText('*a *b* c*'), 'a b c');
  format.render(root, '```인라인 세 개```\n다음 줄');
  assert.equal(byTag(root, 'pre').length, 0); assert.equal(byTag(root, 'code')[0].textContent, '인라인 세 개');
  assert.match(visibleText(root), /다음 줄/);
});

test('B7·B13: a code block has a header row with its language and the copy icon above the scrolling code', () => {
  const format = formatter(); const root = element();
  format.render(root, '```python\n' + 'x = 1  # '.repeat(30) + '\n```', { onCopy() {} });
  const block = nodes(root).find((node) => /message-code-block/.test(node.className));
  const [head, pre] = block.children;
  assert.equal(head.className, 'message-code-head'); assert.equal(pre.tagName, 'PRE');
  assert.equal(visibleText(byTag(head, 'span')[0]), 'python');
  assert.equal(byTag(head, 'button').length, 1); assert.equal(byTag(pre, 'button').length, 0);
  format.render(root, '```\ncode\n```');
  assert.equal(nodes(root).some((node) => node.className === 'message-code-head'), false, 'no language and no copy: no header');
  const css = fs.readFileSync(path.join(__dirname, '../web/style.css'), 'utf8');
  assert.match(css, /\.message-code-head \.message-block-copy \{[^}]*position: relative;/);
});
