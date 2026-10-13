"use strict";

// Deliberately small Markdown subset (pipe tables, rules, task items, ~~strike~~ and bare or
// <angle> web addresses included). HTML (except a `<br>` in a table cell), images, reference links and multiline inline
// delimiters stay text. Unclosed fences stay readable as code.
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
  // A bare web address ends at the first character an address cannot hold (a Korean particle
  // right after it stays text), then loses the punctuation that ends its sentence and a ")"
  // or "]" it did not open.
  function bareAddress(text) {
    let url = text.slice(0, 2048).match(/^https?:\/\/[A-Za-z0-9\-._~:/?#[\]@!$&'()*+,;=%]+/i)?.[0] || "";
    const count = (char) => url.split(char).length - 1;
    while (url && (/[.,;:!?'*]$/.test(url) || (url.endsWith(")") && count("(") < count(")")) ||
                   (url.endsWith("]") && count("[") < count("]")))) url = url.slice(0, -1);
    return /^https?:\/\/[^/?#]/i.test(url) ? url : "";
  }
  function inline(text, budget, depth = 0, links = true) {
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
    const runAt = (cursor) => text.slice(cursor).match(text[cursor] === "*" ? /^\*+/ : /^_+/)[0];
    const wordChar = (char) => /[\p{L}\p{N}]/u.test(char || "");
    // Where the closing `marker` of a span opened just before `start` begins.
    // A run that opens an inner span (`*a **b** c*`) is skipped up to its own
    // closer; a closing run longer than the marker (`***`) closes with its
    // first characters and leaves the rest to the enclosing span.
    const markedEnd = (marker, start, nest = 0) => {
      for (let cursor = start; cursor < text.length;) {
        spend(budget, "work");
        if (text[cursor] === "\\") { cursor += 2; continue; }
        if (text[cursor] === "`") {
          const codeMarker = text.slice(cursor).match(/^`+/)[0];
          const end = codeEnd(codeMarker, cursor + codeMarker.length);
          cursor = end < 0 ? cursor + codeMarker.length : end + codeMarker.length;
          continue;
        }
        if (text[cursor] !== marker[0]) { cursor++; continue; }
        const run = runAt(cursor);
        const underscore = marker[0] === "_";
        const canClose = run.length <= 3 && !/\s/.test(text[cursor - 1] || " ") &&
          !(underscore && wordChar(text[cursor + run.length]));
        if (canClose && run.length === marker.length) return cursor;
        const canOpen = nest < 3 && run.length <= 3 && !/\s/.test(text[cursor + run.length] || " ") &&
          !(underscore && wordChar(text[cursor - 1]));
        if (canOpen && (run.length !== marker.length || !canClose)) {
          const inner = markedEnd(run, cursor + run.length, nest + 1);
          if (inner >= 0) {
            const runEnd = inner + runAt(inner).length;
            if (runEnd - (inner + run.length) >= marker.length && !(underscore && wordChar(text[runEnd])))
              return inner + run.length;
            cursor = runEnd;
            continue;
          }
        }
        if (canClose && run.length > marker.length) return cursor;
        cursor += run.length;
      }
      return -1;
    };
    // The "]" that closes a link label opened just before `start`: nested
    // brackets are counted, escapes and code spans skipped.
    const lastClose = text.lastIndexOf("]");
    const labelEnd = (start) => {
      if (start > lastClose) return -1;
      let nesting = 0;
      for (let cursor = start; cursor < text.length && cursor - start < 1000; cursor++) {
        spend(budget, "work");
        const value = text[cursor];
        if (value === "\\") { cursor++; continue; }
        if (value === "`") {
          const codeMarker = text.slice(cursor).match(/^`+/)[0];
          const end = codeEnd(codeMarker, cursor + codeMarker.length);
          cursor = (end < 0 ? cursor : end) + codeMarker.length - 1;
          continue;
        }
        if (value === "[") nesting++;
        else if (value === "]" && !nesting--) return cursor;
      }
      return -1;
    };
    for (let index = 0; index < text.length;) {
      spend(budget, "work");
      const char = text[index];
      if (char === "\\" && /[\\`*_[\]{}()#+.!>~-]/.test(text[index + 1] || "")) {
        buffer += text[index + 1]; index += 2; continue;
      }
      if (char === "<" && links) {
        const angle = text.slice(index, index + 2050).match(/^<(https?:\/\/[^\s<>]+)>/i);
        const href = angle ? safeHref(angle[1]) : "";
        if (href) {
          spend(budget, "work", angle[0].length);
          flush(); out.push(make(budget, "link", { children: [make(budget, "text", { text: angle[1] })],
            destination: angle[1], href, auto: true }));
          index += angle[0].length; continue;
        }
      }
      // Not the destination of a link or image left as text ("![그림](https://…)").
      if ((char === "h" || char === "H") && links && !/[\p{L}\p{N}_/]/u.test(text[index - 1] || "") &&
          !text.startsWith("](", index - 2)) {
        const url = bareAddress(text.slice(index));
        const href = url ? safeHref(url) : "";
        if (href) {
          spend(budget, "work", url.length);
          flush(); out.push(make(budget, "link", { children: [make(budget, "text", { text: url })],
            destination: url, href, auto: true }));
          index += url.length; continue;
        }
      }
      if (char === "~" && text.startsWith("~~", index) && !/\s/.test(text[index + 2] || " ")) {
        const end = find("~~", index + 2);
        if (end > index + 2 && !/\s/.test(text[end - 1])) {
          flush(); out.push(make(budget, "del", { children: inline(text.slice(index + 2, end), budget, depth + 1, links) }));
          index = end + 2; continue;
        }
        buffer += "~~"; index += 2; continue;
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
        // Only "[label](" with this bracket's own "]" is a link; "[P1] … [a](url)"
        // keeps "[P1] … " as text.
        const close = labelEnd(index + 1);
        if (close >= 0 && text[close + 1] === "(") {
          let cursor = close + 2;
          let nesting = 1;
          for (; cursor < text.length && nesting; cursor++) {
            spend(budget, "work");
            if (text[cursor] === "(") nesting++;
            if (text[cursor] === ")") nesting--;
          }
          if (!nesting) {
            const destination = text.slice(close + 2, cursor - 1);
            flush(); out.push(make(budget, "link", {
              // No link inside a link's own label.
              children: inline(text.slice(index + 1, close), budget, depth + 1, false),
              destination, href: safeHref(destination),
            }));
            index = cursor; continue;
          }
        }
        buffer += char; index++; continue;
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
            children: inline(text.slice(index + run.length, end), budget, depth + 1, links),
          }));
          index = end + run.length; continue;
        }
        buffer += run; index += run.length; continue;
      }
      buffer += char; index++;
    }
    flush(); return out;
  }
  // Columns of leading whitespace (a tab advances to the next multiple of 4).
  function leadWidth(text) {
    let width = 0;
    for (const char of text) {
      if (char === " ") width++;
      else if (char === "\t") width += 4 - (width % 4);
      else break;
    }
    return width;
  }
  // The line without up to `width` columns of leading whitespace.
  function dedent(text, width) {
    let column = 0; let index = 0;
    while (index < text.length && column < width) {
      if (text[index] === " ") column++;
      else if (text[index] === "\t") {
        const next = column + 4 - (column % 4);
        if (next > width) return " ".repeat(next - width) + text.slice(index + 1);
        column = next;
      } else break;
      index++;
    }
    return text.slice(index);
  }
  // A fence may be indented (it often is inside list items); its content loses
  // the same indentation. Backtick fences take no backticks in their info.
  function fence(line) {
    const match = line.match(/^([ \t]*)(?:(`{3,})([^`]*)|(~{3,})(.*))$/);
    return match ? { indent: leadWidth(match[1]), marker: match[2] || match[4], info: match[3] ?? match[5] ?? "" } : null;
  }
  function quote(line) { return line.match(/^ {0,3}>[ \t]?(.*)$/); }
  function heading(line) { return line.match(/^ {0,3}(#{1,6})[ \t]+(.*)$/); }
  // "2026. 10. 10. 회의" is a Korean date, not the 2026th item of a list.
  const DATE_LINE = /^[ \t]*\d{4}\.[ \t]+\d{1,2}\.(?:[ \t]|$)/;
  function listItem(line) {
    if (DATE_LINE.test(line)) return null;
    const unordered = line.match(/^([ \t]*)([-+*])([ \t]+)(.*)$/);
    const ordered = unordered ? null : line.match(/^([ \t]*)(\d{1,9})[.)]([ \t]+)(.*)$/);
    const match = unordered || ordered;
    if (!match) return null;
    const depth = leadWidth(match[1]);
    const marker = ordered ? match[2].length + 1 : 1;
    // Where the item's own text starts; deeper lines belong to the item.
    const gap = leadWidth(" ".repeat(depth + marker) + match[3]) - depth - marker;
    return { indent: match[1], depth, ordered: !!ordered, number: ordered ? Number(match[2]) : null,
      content: depth + marker + (gap > 4 ? 1 : gap), text: match[4] };
  }
  // Whether a line ends the paragraph above it. As in CommonMark, only a list
  // starting at 1 (or a bullet) interrupts a paragraph: "…\n2026. 10. 10." and
  // "…\n1000. 항목" stay text.
  // GFM pipe tables: a header row, then a delimiter row with as many cells (`---`, `:--`,
  // `--:`, `:-:`). Pipes inside code spans and escaped pipes (\|) stay in their cell.
  function cells(line) {
    let text = line.trim();
    if (text.startsWith("|")) text = text.slice(1);
    if (text.endsWith("|") && !text.endsWith("\\|")) text = text.slice(0, -1);
    const out = []; let cell = ""; let ticks = 0;
    for (let index = 0; index < text.length; index++) {
      const char = text[index];
      if (char === "\\" && text[index + 1] === "|") { cell += "|"; index++; continue; }
      if (char === "`") {
        let run = 1; while (text[index + run] === "`") run++;
        ticks = ticks === run ? 0 : ticks || run; cell += text.slice(index, index + run); index += run - 1; continue;
      }
      if (char === "|" && !ticks) { out.push(cell.trim()); cell = ""; continue; }
      cell += char;
    }
    out.push(cell.trim());
    return out;
  }
  // A cell's lines: answers break a line inside a cell with `<br>`, which outside code
  // spans is a line break, not text.
  function cellLines(text, budget) {
    const parts = []; let start = 0;
    for (let index = 0; index < text.length; index++) {
      if (text[index] === "`") {
        let run = 1; while (text[index + run] === "`") run++;
        const end = text.indexOf("`".repeat(run), index + run);
        index = (end < 0 ? index : end) + run - 1; continue;
      }
      const br = text[index] === "<" ? text.slice(index, index + 6).match(/^<br ?\/?>/i) : null;
      if (br) { parts.push(text.slice(start, index)); start = index + br[0].length; index = start - 1; }
    }
    parts.push(text.slice(start));
    return parts.map((part) => inline(part.trim(), budget));
  }
  function delimiterRow(line) {
    if (!line.includes("-") || !/^ {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$/.test(line)) return null;
    return cells(line).map((cell) => cell.startsWith(":") && cell.endsWith(":") ? "center"
      : cell.endsWith(":") ? "right" : cell.startsWith(":") ? "left" : "");
  }
  function tableAt(lines, index) {
    const header = lines[index]?.text; const next = lines[index + 1]?.text;
    if (!header || next == null || !header.includes("|") || leadWidth(header) > 3) return null;
    const align = delimiterRow(next);
    if (!align || (!header.trim().startsWith("|") && !next.includes("|") && align.length < 2)) return null;
    const head = cells(header);
    return head.length === align.length ? { head, align } : null;
  }
  // A thematic break: three or more of one of - * _ alone on a line (spaces between allowed).
  function rule(line) {
    return /^ {0,3}(?:(?:-[ \t]*){3,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,})$/.test(line);
  }
  function interrupts(line) {
    if (!line.trim() || fence(line) || quote(line) || heading(line) || rule(line)) return true;
    const item = listItem(line);
    return !!item && !!item.text.trim() && (!item.ordered || item.number === 1);
  }
  // An item's lines: its first line, then every line indented to its text,
  // blank lines followed by more of it, and lazy continuations of its
  // paragraph. The body is parsed again as blocks (sub-lists, paragraphs,
  // fences); its first paragraph stays inline next to the marker.
  function parseList(lines, start, budget, depth) {
    if (depth > MAX_DEPTH) throw new Error("format limit");
    const first = listItem(lines[start].text);
    const items = [];
    let index = start;
    const sibling = (line) => {
      const item = listItem(line.text);
      return !!item && item.depth === first.depth && item.ordered === first.ordered;
    };
    while (index < lines.length) {
      const item = listItem(lines[index].text);
      if (!item || item.depth !== first.depth || item.ordered !== first.ordered) break;
      const content = [{ text: item.text, eol: lines[index].eol }];
      let blank = 0;
      let fenced = fence(item.text);
      let paragraph = !fenced && !!item.text.trim();
      index++;
      while (index < lines.length) {
        spend(budget, "work");
        const line = lines[index];
        if (!line.text.trim()) { blank++; index++; continue; }
        const width = leadWidth(line.text);
        const nested = listItem(line.text);
        const inside = width >= item.content || (!!nested && nested.depth > item.depth && !sibling(line));
        // Inside a list any marker line ("5) …" too) starts an item, never continues one.
        const lazy = !blank && paragraph && !fenced && !nested && !interrupts(line.text);
        if (!inside && !lazy) break;
        for (; blank; blank--) content.push({ text: "", eol: lines[index - blank].eol });
        const text = dedent(line.text, Math.min(width, item.content));
        content.push({ text, eol: line.eol });
        const opened = fence(text);
        if (fenced) {
          const close = text.match(/^[ \t]*(`+|~+)[ \t]*$/);
          if (close && close[1][0] === fenced.marker[0] && close[1].length >= fenced.marker.length) fenced = null;
          paragraph = false;
        } else if (opened) { fenced = opened; paragraph = false; } else paragraph = !heading(text);
        index++;
      }
      if (blank) {
        // Blank lines between two items keep the list going (a loose list);
        // otherwise they are left to whatever follows the list.
        if (index < lines.length && sibling(lines[index]))
          for (; blank; blank--) content.push({ text: "", eol: lines[index - blank].eol });
        else index -= blank;
      }
      let lead = 0;
      if (content[0].text.trim() && !interrupts(content[0].text) && !listItem(content[0].text)) {
        lead = 1;
        while (lead < content.length && !interrupts(content[lead].text)) lead++;
      }
      items.push(make(budget, "item", { ...item,
        lines: content.slice(0, lead).map((line) => ({ children: inline(line.text, budget), eol: line.eol })),
        nested: blocks(content.slice(lead), budget, depth + 1) }));
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
        const body = [];
        index++;
        while (index < lines.length) {
          const close = lines[index].text.match(/^([ \t]*)(`+|~+)[ \t]*$/);
          if (close && leadWidth(close[1]) <= Math.max(3, opened.indent) && close[2][0] === opened.marker[0] &&
              close[2].length >= opened.marker.length) { index++; break; }
          body.push({ text: dedent(lines[index].text, opened.indent), eol: lines[index].eol }); index++;
        }
        out.push(make(budget, "fence", { text: lineText(body), language: opened.info.trim().match(/^[A-Za-z0-9_+-]{1,32}(?:\s|$)/)?.[0].trim() || "" }));
        continue;
      }
      if (quote(line.text)) {
        const quoted = [];
        while (index < lines.length && quote(lines[index].text)) {
          quoted.push({ text: quote(lines[index].text)[1], eol: lines[index].eol }); index++;
        }
        out.push(make(budget, "quote", { children: blocks(quoted, budget, depth + 1) })); continue;
      }
      if (rule(line.text)) { out.push(make(budget, "rule", { text: line.text + line.eol })); index++; continue; }
      const title = heading(line.text);
      if (title) {
        out.push(make(budget, "heading", { level: title[1].length,
          children: inline(title[2].replace(/[ \t]+#+[ \t]*$/, ""), budget), eol: line.eol })); index++; continue;
      }
      if (listItem(line.text)) {
        const list = parseList(lines, index, budget, depth); out.push(list.node); index = list.next; continue;
      }
      const table = tableAt(lines, index);
      if (table) {
        const start = index; const rows = [];
        index += 2;
        while (index < lines.length && lines[index].text.includes("|") && !interrupts(lines[index].text)) {
          spend(budget, "work");
          const row = cells(lines[index].text);
          rows.push(table.align.map((_, column) => cellLines(row[column] || "", budget))); index++;
        }
        out.push(make(budget, "table", { align: table.align, head: table.head.map((cell) => cellLines(cell, budget)), rows,
          text: lineText(lines.slice(start, index)) }));
        continue;
      }
      const paragraph = [];
      do {
        paragraph.push({ children: inline(lines[index].text, budget), eol: lines[index].eol }); index++;
      } while (index < lines.length && !interrupts(lines[index].text) && !tableAt(lines, index));
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
    return nodes.map((node) => node.kind === "link" ? (node.auto ? node.destination : `${inlinePlain(node.children)} (${node.destination})`)
      : node.children ? inlinePlain(node.children) : node.text).join("");
  }
  function blockPlain(nodes) {
    return nodes.map((node) => {
      if (["blank", "fence", "fallback", "table", "rule"].includes(node.kind)) return node.text;
      if (node.kind === "heading") return inlinePlain(node.children) + node.eol;
      if (node.kind === "paragraph") return node.lines.map((line) => inlinePlain(line.children) + line.eol).join("");
      if (node.kind === "quote") return blockPlain(node.children);
      if (node.kind === "list") return node.items.map((item) => {
        // The item's further lines keep the indentation they had under the marker.
        const pad = " ".repeat(item.content);
        const head = item.lines.map((line, index) => (index ? pad : "") + inlinePlain(line.children) + line.eol).join("");
        const rest = blockPlain(item.nested).replace(/(^|\r\n|\r|\n)(?=[^\r\n])/g, `$1${pad}`);
        // An item that starts with a block (a fence, a sub-list) starts it after the marker.
        return item.indent + (node.ordered ? `${item.number}. ` : "• ") +
          (head ? head + rest : rest.startsWith(pad) ? rest.slice(pad.length) : rest);
      }).join("");
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
      if (value.kind === "rule") { container.append(element("hr", "message-rule")); continue; }
      if (value.kind === "fallback") { container.append(element("div", "message-format-fallback", value.text)); continue; }
      if (value.kind === "fence") {
        const lines = lineCount(value.text);
        const foldAfter = Number.isInteger(options?.foldAfter) ? options.foldAfter : FOLD_LINES;
        const foldable = lines > foldAfter;
        const block = element("div", `message-code-block${foldable ? " is-foldable is-folded" : ""}`);
        // A header row above the code holds the language and the copy icon, so
        // a long first line scrolls under neither.
        if (value.language || typeof options?.onCopy === "function") {
          const head = element("div", "message-code-head");
          head.append(element("span", "message-code-lang", value.language));
          copyButton(head, value.text, "code", options);
          block.append(head);
        }
        const pre = element("pre", "message-code");
        const code = element("code", "", value.text); if (value.language) code.setAttribute("data-language", value.language);
        pre.append(code); block.append(pre);
        if (foldable) foldToggle(block, lines);
        container.append(block); continue;
      }
      if (value.kind === "table") {
        // Wider than the bubble, it scrolls sideways on its own (style.css .message-table-wrap).
        const wrap = element("div", "message-table-wrap"); const table = element("table", "message-table");
        const row = (cellsOf, tag) => {
          const tr = element("tr");
          cellsOf.forEach((cell, column) => {
            const td = element(tag); if (value.align[column]) td.setAttribute("data-align", value.align[column]);
            cell.forEach((line, index) => { if (index) td.append(element("br")); appendInline(td, line); });
            tr.append(td);
          });
          return tr;
        };
        const head = element("thead"); head.append(row(value.head, "th"));
        const body = element("tbody"); for (const cellsOf of value.rows) body.append(row(cellsOf, "td"));
        table.append(head, body); wrap.append(table); container.append(wrap); continue;
      }
      if (value.kind === "quote") {
        const block = element("div", "message-quote-block"); const quoted = element("blockquote", "message-quote");
        appendBlocks(quoted, value.children, options); block.append(quoted);
        container.append(block); continue;
      }
      if (value.kind === "list") {
        const list = element(value.ordered ? "ol" : "ul");
        if (value.ordered) {
          list.setAttribute("start", String(value.items[0].number));
          // Room for long numbers ("1000.") inside the bubble (style.css: ol[data-digits]).
          const digits = Math.max(...value.items.map((item) => String(item.number).length));
          if (digits > 2) list.setAttribute("data-digits", String(Math.min(digits, 6)));
        }
        for (const item of value.items) {
          const li = element("li"); if (value.ordered) li.setAttribute("value", String(item.number));
          item.lines.forEach((line, index) => {
            if (index) li.append(element("br"));
            let children = line.children;
            // A task item ("- [ ] 할 일", "- [x] 완료"): a check mark instead of the brackets.
            const task = !index && children[0]?.kind === "text" ? children[0].text.match(/^\[([ xX])\][ \t]+/) : null;
            if (task) {
              const done = task[1] !== " ";
              const box = element("span", `message-task${done ? " is-done" : ""}`, done ? "☑" : "☐");
              box.setAttribute("role", "img"); box.setAttribute("aria-label", done ? "완료" : "할 일");
              li.setAttribute("data-task", done ? "done" : "open"); li.append(box);
              children = [{ ...children[0], text: children[0].text.slice(task[0].length) }, ...children.slice(1)];
            }
            appendInline(li, children);
          });
          appendBlocks(li, item.nested, options); list.append(li);
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
