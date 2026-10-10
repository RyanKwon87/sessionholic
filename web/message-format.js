"use strict";

// Deliberately small Markdown subset. HTML, images, reference links, tables and
// multiline inline delimiters stay text. Unclosed fences stay readable as code.
// Limits bound parsing and DOM work; fallback always retains the complete text.
const MessageFormat = (() => {
  const MAX_CHARS = 65536;
  const MAX_LINES = 1024;
  const MAX_DEPTH = 8;
  const MAX_NODES = 4096;
  const MAX_WORK = 262144;
  // Code blocks longer than this fold to their first lines behind "모두 보기".
  const FOLD_LINES = 25;

  function sourceOf(value) { return value == null ? "" : String(value); }
  function spend(budget, kind, amount = 1) {
    budget[kind] -= amount;
    if (budget[kind] < 0) throw new Error("format limit");
  }
  function make(budget, kind, data) {
    spend(budget, "nodes");
    return { kind, ...data };
  }
  function linesOf(text) {
    const lines = [];
    let start = 0;
    for (let index = 0; index < text.length; index++) {
      if (text[index] !== "\n" && text[index] !== "\r") continue;
      const end = index;
      if (text[index] === "\r" && text[index + 1] === "\n") index++;
      lines.push({ text: text.slice(start, end), eol: text.slice(end, index + 1) });
      start = index + 1;
    }
    if (start < text.length) lines.push({ text: text.slice(start), eol: "" });
    return lines;
  }
  function lineText(lines) { return lines.map((line) => line.text + line.eol).join(""); }
  function safeHref(value) {
    if (!/^https?:\/\//i.test(value) || /[\s\u0000-\u001f\u007f]/.test(value)) return "";
    try {
      const url = new URL(value);
      return ["http:", "https:"].includes(url.protocol) ? url.href : "";
    } catch { return ""; }
  }
  function inline(text, budget, depth = 0) {
    if (depth > MAX_DEPTH) return [make(budget, "text", { text })];
    const out = [];
    let buffer = "";
    const flush = () => { if (buffer) { out.push(make(budget, "text", { text: buffer })); buffer = ""; } };
    const find = (needle, start) => {
      const index = text.indexOf(needle, start);
      spend(budget, "work", (index < 0 ? text.length : index) - start + needle.length);
      return index;
    };
    const codeEnd = (marker, start) => {
      let end = find(marker, start);
      while (end >= 0 && (text[end - 1] === "`" || text[end + marker.length] === "`"))
        end = find(marker, end + marker.length);
      return end;
    };
    const markedEnd = (marker, start) => {
      for (let cursor = start; cursor < text.length;) {
        spend(budget, "work");
        if (text[cursor] === "\\") { cursor += 2; continue; }
        if (text[cursor] === "`") {
          const codeMarker = text.slice(cursor).match(/^`+/)[0];
          const end = codeEnd(codeMarker, cursor + codeMarker.length);
          cursor = end < 0 ? cursor + codeMarker.length : end + codeMarker.length;
          continue;
        }
        if (text[cursor] === marker[0]) {
          const run = text.slice(cursor).match(marker[0] === "*" ? /^\*+/ : /^_+/)[0];
          const end = cursor + run.length - marker.length;
          if (run.length >= marker.length && run.length <= 3 && !/\s/.test(text[cursor - 1] || " ") &&
              !(marker[0] === "_" && /[\p{L}\p{N}]/u.test(text[cursor + run.length] || ""))) return end;
          cursor += run.length;
        } else cursor++;
      }
      return -1;
    };
    for (let index = 0; index < text.length;) {
      spend(budget, "work");
      const char = text[index];
      if (char === "\\" && /[\\`*_[\]{}()#+.!>~-]/.test(text[index + 1] || "")) {
        buffer += text[index + 1]; index += 2; continue;
      }
      if (char === "<" && (text.startsWith("<!--", index) || /^<\/?[A-Za-z]/.test(text.slice(index)))) {
        const comment = text.startsWith("<!--", index);
        const end = find(comment ? "-->" : ">", index + (comment ? 4 : 1));
        if (end < 0) { buffer += text.slice(index); index = text.length; continue; }
        const next = end + (comment ? 3 : 1);
        buffer += text.slice(index, next); index = next; continue;
      }
      if (char === "`") {
        const marker = text.slice(index).match(/^`+/)[0];
        const end = codeEnd(marker, index + marker.length);
        if (end >= 0) {
          flush(); out.push(make(budget, "code", { text: text.slice(index + marker.length, end) }));
          index = end + marker.length; continue;
        }
        buffer += marker; index += marker.length; continue;
      }
      if (char === "[" && text[index - 1] !== "!") {
        const labelEnd = find("](", index + 1);
        if (labelEnd >= 0) {
          let cursor = labelEnd + 2;
          let nesting = 1;
          for (; cursor < text.length && nesting; cursor++) {
            spend(budget, "work");
            if (text[cursor] === "(") nesting++;
            if (text[cursor] === ")") nesting--;
          }
          if (!nesting) {
            const destination = text.slice(labelEnd + 2, cursor - 1);
            flush(); out.push(make(budget, "link", {
              children: inline(text.slice(index + 1, labelEnd), budget, depth + 1),
              destination, href: safeHref(destination),
            }));
            index = cursor; continue;
          }
        }
      }
      if (char === "*" || char === "_") {
        const run = text.slice(index).match(char === "*" ? /^\*+/ : /^_+/)[0];
        if (run.length > 3 || /\s/.test(text[index + run.length] || " ") ||
            (char === "_" && /[\p{L}\p{N}]/u.test(text[index - 1] || "") && /[\p{L}\p{N}]/u.test(text[index + run.length] || ""))) {
          buffer += run; index += run.length; continue;
        }
        const end = markedEnd(run, index + run.length);
        if (end > index + run.length) {
          flush(); out.push(make(budget, run.length === 1 ? "em" : run.length === 2 ? "strong" : "strongEm", {
            children: inline(text.slice(index + run.length, end), budget, depth + 1),
          }));
          index = end + run.length; continue;
        }
        buffer += run; index += run.length; continue;
      }
      buffer += char; index++;
    }
    flush(); return out;
  }
  function fence(line) { return line.match(/^ {0,3}(`{3,}|~{3,})(.*)$/); }
  function quote(line) { return line.match(/^ {0,3}>[ \t]?(.*)$/); }
  function heading(line) { return line.match(/^ {0,3}(#{1,6})[ \t]+(.*)$/); }
  function listItem(line) {
    const unordered = line.match(/^([ \t]*)([-+*])[ \t]+(.*)$/);
    const ordered = line.match(/^([ \t]*)(\d{1,9})[.)][ \t]+(.*)$/);
    const match = unordered || ordered;
    return match ? { indent: match[1], depth: match[1].replace(/\t/g, "    ").length,
      ordered: !!ordered, number: ordered ? Number(match[2]) : null, text: match[3] } : null;
  }
  function special(line) { return !line.trim() || fence(line) || quote(line) || heading(line) || listItem(line); }
  function parseList(lines, start, budget, depth) {
    if (depth > MAX_DEPTH) throw new Error("format limit");
    const first = listItem(lines[start].text);
    const items = [];
    let index = start;
    while (index < lines.length) {
      const item = listItem(lines[index].text);
      if (!item || item.depth !== first.depth || item.ordered !== first.ordered) break;
      const value = make(budget, "item", { ...item, children: inline(item.text, budget), eol: lines[index].eol, nested: [] });
      items.push(value); index++;
      while (index < lines.length && listItem(lines[index].text)?.depth > first.depth) {
        const nested = parseList(lines, index, budget, depth + 1);
        value.nested.push(nested.node); index = nested.next;
      }
    }
    return { node: make(budget, "list", { ordered: first.ordered, items }), next: index };
  }
  function blocks(lines, budget, depth = 0) {
    if (depth > MAX_DEPTH) throw new Error("format limit");
    const out = [];
    for (let index = 0; index < lines.length;) {
      const line = lines[index];
      if (!line.text.trim()) { out.push(make(budget, "blank", { text: line.text + line.eol })); index++; continue; }
      const opened = fence(line.text);
      if (opened) {
        const marker = opened[1]; const body = [];
        index++;
        while (index < lines.length) {
          const close = lines[index].text.match(/^ {0,3}(`+|~+)[ \t]*$/);
          if (close && close[1][0] === marker[0] && close[1].length >= marker.length) { index++; break; }
          body.push(lines[index]); index++;
        }
        out.push(make(budget, "fence", { text: lineText(body), language: opened[2].trim().match(/^[A-Za-z0-9_+-]{1,32}(?:\s|$)/)?.[0].trim() || "" }));
        continue;
      }
      if (quote(line.text)) {
        const quoted = [];
        while (index < lines.length && quote(lines[index].text)) {
          quoted.push({ text: quote(lines[index].text)[1], eol: lines[index].eol }); index++;
        }
        out.push(make(budget, "quote", { children: blocks(quoted, budget, depth + 1) })); continue;
      }
      const title = heading(line.text);
      if (title) {
        out.push(make(budget, "heading", { level: title[1].length,
          children: inline(title[2].replace(/[ \t]+#+[ \t]*$/, ""), budget), eol: line.eol })); index++; continue;
      }
      if (listItem(line.text)) {
        const list = parseList(lines, index, budget, depth); out.push(list.node); index = list.next; continue;
      }
      const paragraph = [];
      do {
        paragraph.push({ children: inline(lines[index].text, budget), eol: lines[index].eol }); index++;
      } while (index < lines.length && !special(lines[index].text));
      out.push(make(budget, "paragraph", { lines: paragraph }));
    }
    return out;
  }
  function parse(value) {
    const text = sourceOf(value);
    if (text.length > MAX_CHARS) return [{ kind: "fallback", text }];
    const lines = linesOf(text);
    if (lines.length > MAX_LINES) return [{ kind: "fallback", text }];
    try { return blocks(lines, { nodes: MAX_NODES, work: MAX_WORK }); }
    catch { return [{ kind: "fallback", text }]; }
  }
  function inlinePlain(nodes) {
    return nodes.map((node) => node.kind === "link" ? `${inlinePlain(node.children)} (${node.destination})`
      : node.children ? inlinePlain(node.children) : node.text).join("");
  }
  function blockPlain(nodes) {
    return nodes.map((node) => {
      if (["blank", "fence", "fallback"].includes(node.kind)) return node.text;
      if (node.kind === "heading") return inlinePlain(node.children) + node.eol;
      if (node.kind === "paragraph") return node.lines.map((line) => inlinePlain(line.children) + line.eol).join("");
      if (node.kind === "quote") return blockPlain(node.children);
      if (node.kind === "list") return node.items.map((item) => item.indent + (node.ordered ? `${item.number}. ` : "• ") +
        inlinePlain(item.children) + item.eol + blockPlain(item.nested)).join("");
      return "";
    }).join("");
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function appendInline(container, nodes) {
    for (const value of nodes) {
      if (value.kind === "text") { container.append(element("span", "", value.text)); continue; }
      if (value.kind === "code") { container.append(element("code", "", value.text)); continue; }
      if (value.kind === "link" && !value.href) {
        container.append(element("span", "", `${inlinePlain(value.children)} (${value.destination})`)); continue;
      }
      const node = element(value.kind === "link" ? "a" : value.kind === "strongEm" ? "strong" : value.kind);
      if (value.kind === "link") {
        node.setAttribute("href", value.href); node.setAttribute("target", "_blank"); node.setAttribute("rel", "noopener noreferrer");
      }
      if (value.kind === "strongEm") {
        const emphasis = element("em"); appendInline(emphasis, value.children); node.append(emphasis);
      } else appendInline(node, value.children);
      container.append(node);
    }
  }
  // A small icon in the code block's corner; whole messages and quotes are
  // copied from the message menu instead.
  function copyButton(parent, text, kind, options) {
    if (typeof options?.onCopy !== "function") return;
    const button = element("button", "message-block-copy");
    button.setAttribute("type", "button"); button.setAttribute("aria-label", "코드 블록 복사");
    button.setAttribute("title", "코드 복사");
    button.addEventListener("click", () => {
      // The caller owns clipboard permissions and success/failure feedback.
      try { Promise.resolve(options.onCopy(text, kind)).catch(() => {}); } catch { /* caller handles feedback */ }
    });
    parent.append(button);
  }
  function lineCount(text) {
    const lines = text.split(/\r\n|\r|\n/);
    if (lines.length && lines[lines.length - 1] === "") lines.pop();
    return lines.length;
  }
  // Folded: the first lines show under a fade; the toggle keeps the count.
  function foldToggle(block, lines) {
    const toggle = element("button", "message-code-toggle", `모두 보기 · ${lines}줄`);
    toggle.setAttribute("type", "button"); toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", () => {
      const folded = / is-folded(?: |$)/.test(" " + block.className);
      block.className = folded ? block.className.replace(/\s*\bis-folded\b/, "") : `${block.className} is-folded`;
      toggle.textContent = folded ? "접기" : `모두 보기 · ${lines}줄`;
      toggle.setAttribute("aria-expanded", String(folded));
    });
    block.append(toggle);
  }
  function appendBlocks(container, nodes, options) {
    for (const value of nodes) {
      if (value.kind === "blank") continue;
      if (value.kind === "fallback") { container.append(element("div", "message-format-fallback", value.text)); continue; }
      if (value.kind === "fence") {
        const lines = lineCount(value.text);
        const foldAfter = Number.isInteger(options?.foldAfter) ? options.foldAfter : FOLD_LINES;
        const foldable = lines > foldAfter;
        const block = element("div", `message-code-block${foldable ? " is-foldable is-folded" : ""}`);
        const pre = element("pre", "message-code");
        const code = element("code", "", value.text); if (value.language) code.setAttribute("data-language", value.language);
        pre.append(code); block.append(pre); copyButton(block, value.text, "code", options);
        if (foldable) foldToggle(block, lines);
        container.append(block); continue;
      }
      if (value.kind === "quote") {
        const block = element("div", "message-quote-block"); const quoted = element("blockquote", "message-quote");
        appendBlocks(quoted, value.children, options); block.append(quoted);
        container.append(block); continue;
      }
      if (value.kind === "list") {
        const list = element(value.ordered ? "ol" : "ul");
        if (value.ordered) list.setAttribute("start", String(value.items[0].number));
        for (const item of value.items) {
          const li = element("li"); if (value.ordered) li.setAttribute("value", String(item.number));
          appendInline(li, item.children); appendBlocks(li, item.nested, options); list.append(li);
        }
        container.append(list); continue;
      }
      const node = element(value.kind === "heading" ? `h${value.level}` : "p");
      if (value.kind === "heading") appendInline(node, value.children);
      else value.lines.forEach((line, index) => { if (index) node.append(element("br")); appendInline(node, line.children); });
      container.append(node);
    }
  }
  // The exact text of every fenced block, quoted ones included.
  function fences(nodes, out = []) {
    for (const node of nodes) {
      if (node.kind === "fence") out.push(node.text);
      else if (node.kind === "quote") fences(node.children, out);
      else if (node.kind === "list") for (const item of node.items) fences(item.nested, out);
    }
    return out;
  }
  return Object.freeze({
    FOLD_LINES,
    render(container, text, options = {}) {
      container.replaceChildren(); container.classList.add("message-markdown");
      appendBlocks(container, parse(text), options);
    },
    plainText(text) { return blockPlain(parse(text)); },
    codeBlocks(text) { return fences(parse(text)); },
  });
})();
