// Tiny, safe markdown renderer. Escape FIRST, then apply a small set of formats.
// Supports: paragraphs, **bold**, *italic*, `code`, ``` fences, #-### headings, - / 1. lists,
// > quotes, GFM tables, http(s) links, and citation tags [P1] / [W2] -> buttons.

const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
export const escapeHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ESC[c]);

function inline(text) {
  // `text` is already escaped. Protect code spans first.
  const codes = [];
  let s = text.replace(/`([^`\n]+)`/g, (_, c) => {
    codes.push(c);
    return `\u0000${codes.length - 1}\u0000`;
  });
  // Links: [label](https://...) — only http(s); label/url already escaped.
  s = s.replace(/\[([^\]\n]{1,200})\]\((https?:\/\/[^\s)]{1,500})\)/g, (_, label, url) =>
    `<a href="${url}" target="_blank" rel="noopener noreferrer nofollow">${label}</a>`);
  // Citation groups: [P1], [W2], [P1, W2], [P1][P2]
  s = s.replace(/\[((?:\s*[PW]\d{1,2}\s*[,;]?\s*)+)\]/g, (_, group) =>
    group.match(/[PW]\d{1,2}/g).map((t) =>
      `<button type="button" class="cite cite-${t[0] === "P" ? "pdf" : "web"}" data-tag="${t}" aria-label="Source ${t}">${t}</button>`
    ).join(""));
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/__([^_\n]+)__/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>");
  s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
  return s;
}

const isTableSep = (line) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
const splitRow = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => c.trim());

export function renderMarkdown(src) {
  const lines = escapeHtml(src || "").replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  let para = [];
  const flush = () => {
    if (para.length) out.push(`<p>${inline(para.join(" "))}</p>`);
    para = [];
  };
  while (i < lines.length) {
    const line = lines[i];
    // fenced code
    if (/^\s*```/.test(line)) {
      flush();
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre><code>${buf.join("\n")}</code></pre>`);
      continue;
    }
    if (!line.trim()) { flush(); i++; continue; }
    const h = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (h) {
      flush();
      const level = Math.min(4, Math.max(3, h[1].length + 2));
      out.push(`<h${level}>${inline(h[2])}</h${level}>`);
      i++;
      continue;
    }
    // table
    if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      flush();
      const head = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(splitRow(lines[i++]));
      out.push(
        `<div class="table-wrap"><table><thead><tr>${head.map((c) => `<th>${inline(c)}</th>`).join("")}</tr></thead><tbody>` +
        rows.map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join("")}</tr>`).join("") +
        "</tbody></table></div>"
      );
      continue;
    }
    // lists
    const li = line.match(/^(\s*)([-*•]|\d{1,3}[.)])\s+(.*)$/);
    if (li) {
      flush();
      const ordered = /\d/.test(li[2]);
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(/^(\s*)([-*•]|\d{1,3}[.)])\s+(.*)$/);
        if (m && /\d/.test(m[2]) === ordered && m[1].length < 2) {
          items.push(m[3]);
          i++;
        } else if (m && m[1].length >= 2) {
          items[items.length - 1] += `<br>• ${m[3]}`;
          i++;
        } else if (lines[i].trim() && /^\s{2,}\S/.test(lines[i]) && items.length) {
          items[items.length - 1] += " " + lines[i].trim();
          i++;
        } else break;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>${items.map((t) => `<li>${inline(t)}</li>`).join("")}</${tag}>`);
      continue;
    }
    if (/^\s*&gt;\s?/.test(line)) {
      flush();
      const buf = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) buf.push(lines[i++].replace(/^\s*&gt;\s?/, ""));
      out.push(`<blockquote>${inline(buf.join(" "))}</blockquote>`);
      continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { flush(); out.push("<hr>"); i++; continue; }
    para.push(line.trim());
    i++;
  }
  flush();
  return out.join("");
}
