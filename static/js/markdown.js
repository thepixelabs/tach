// ============================================================
// Minimal Markdown renderer for chat messages
// Converts AI/user text into styled, safe HTML.
// All input is HTML-escaped before transformation, so the
// output can be inserted via innerHTML without XSS risk.
// ============================================================

(function () {
  'use strict';

  function esc(str) {
    if (str === undefined || str === null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // Only allow safe URL schemes (blocks javascript:, data:, etc.)
  function okUrl(url) {
    if (!url) return false;
    if (/^(javascript|vbscript|data):/i.test(url.trim())) return false;
    return /^(https?:\/\/|mailto:|#|\/)/i.test(url.trim());
  }

  // Inline markdown: code spans, images, links, bold, italic, strike.
  // Input must already be HTML-escaped.
  function inline(source) {
    const store = [];
    let out = source
      // protect inline code first so other rules can't touch it
      .replace(/`([^`\n]+)`/g, (m, c) => '\x01' + (store.push(c) - 1) + '\x01')
      .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) =>
        okUrl(url) ? '<img class="md-img" alt="' + alt + '" src="' + url + '">' : m)
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) =>
        okUrl(url) ? '<a href="' + url + '" target="_blank" rel="noopener">' + text + '</a>' : text)
      .replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^\w*])__([\s\S]+?)__(?!\w)/g, '$1<strong>$2</strong>')
      .replace(/(^|[^*\w])\*([^*\n]+?)\*(?!\*)/g, '$1<em>$2</em>')
      .replace(/(^|[^_\w])_([^_\n]+?)_(?!\w)/g, '$1<em>$2</em>')
      .replace(/~~([\s\S]+?)~~/g, '<del>$1</del>');
    return out.replace(/\x01(\d+)\x01/g, (m, n) =>
      '<code class="md-inline-code">' + store[Number(n)] + '</code>');
  }

  // Block-level markdown: fences, headings, hr, quotes, lists, tables.
  function render(src) {
    if (!src || !String(src).trim()) return '';

    const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let para = [];
    let quote = null;
    let listStack = [];
    let code = null; // { lang, buf }

    const flushPara = () => {
      if (para.length) {
        out.push('<p>' + para.map(l => inline(esc(l))).join('<br>') + '</p>');
        para = [];
      }
    };
    const flushQuote = () => {
      if (quote) {
        out.push('<blockquote>' + quote.map(l => inline(esc(l))).join('<br>') + '</blockquote>');
        quote = null;
      }
    };
    const closeLists = () => {
      while (listStack.length) out.push('</' + listStack.pop() + '>');
    };
    const closeCode = () => {
      if (!code) return;
      const lang = code.lang || 'text';
      out.push(
        '<div class="md-code-block">' +
        '<div class="md-code-header">' +
        '<span class="md-code-lang">' + esc(lang) + '</span>' +
        '<button type="button" class="md-copy-btn" onclick="copyCodeBlock(this)">copy</button>' +
        '</div>' +
        '<pre class="md-code"><code>' + esc(code.buf.join('\n')) + '</code></pre>' +
        '</div>'
      );
      code = null;
    };

    const isTableSep = l =>
      /-/.test(l) && /^[\s:|\-]+$/.test(l) && /\|/.test(l);

    const parseRow = l =>
      l.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];

      // Inside a fenced code block: raw lines until the closing fence
      if (code) {
        if (/^\s*(`{3,}|~{3,})\s*$/.test(line)) closeCode();
        else code.buf.push(line);
        continue;
      }

      const fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/);
      if (fence) {
        flushPara(); flushQuote(); closeLists();
        code = { lang: fence[2].replace(/[+#.]/g, ''), buf: [] };
        continue;
      }

      if (!line.trim()) {
        flushPara(); flushQuote(); closeLists();
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.+?)\s*#*\s*$/);
      if (heading) {
        flushPara(); flushQuote(); closeLists();
        const lvl = Math.min(heading[1].length, 4);
        out.push('<h' + lvl + '>' + inline(esc(heading[2])) + '</h' + lvl + '>');
        continue;
      }

      if (/^\s*([-*_])\s*(?:\1\s*){2,}$/.test(line)) {
        flushPara(); flushQuote(); closeLists();
        out.push('<hr class="md-hr">');
        continue;
      }

      const qm = line.match(/^\s*>\s?(.*)$/);
      if (qm && !/^\s*>\s*$/.test(line) ? line.trim() === '>' || qm[1].trim() ? qm : null : null) {
        // falls through when empty quote marker; handled below
      }
      if (qm) {
        flushPara(); closeLists();
        (quote = quote || []).push(qm[1]);
        continue;
      }

      const ulM = line.match(/^(\s*)([-*+])\s+(.*)$/);
      const olM = line.match(/^(\s*)(\d+[.)])\s+(.*)$/);
      const liM = ulM || olM;
      if (liM) {
        flushPara(); flushQuote();
        const type = ulM ? 'ul' : 'ol';
        const depth = Math.min(Math.floor(liM[1].replace(/\t/g, '  ').length / 2), 5);
        const target = depth + 1;
        while (listStack.length > target) out.push('</' + listStack.pop() + '>');
        if (listStack.length === target) {
          if (listStack[target - 1] !== type) {
            out.push('</' + listStack.pop() + '>');
            listStack.push(type);
          }
        } else {
          while (listStack.length < target) listStack.push(type);
        }
        out.push('<li>' + inline(esc(liM[3])) + '</li>');
        continue;
      }

      // Table: header row + separator row
      if (line.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        flushPara(); flushQuote(); closeLists();
        const header = parseRow(line);
        const aligns = parseRow(lines[i + 1]).map(c =>
          /^:-+:/.test(c) ? 'center' : (/-+:$/.test(c) ? 'right' : ''));
        i++;
        const rows = [];
        while (i + 1 < lines.length && lines[i + 1].includes('|') && lines[i + 1].trim()) {
          i++;
          rows.push(parseRow(lines[i]));
        }
        let t = '<div class="md-table-wrap"><table><thead><tr>' +
          header.map((c, ci) =>
            '<th' + (aligns[ci] ? ' style="text-align:' + aligns[ci] + '"' : '') + '>' +
            inline(esc(c)) + '</th>').join('') +
          '</tr></thead><tbody>';
        rows.forEach(r => {
          t += '<tr>' + header.map((_, ci) =>
            '<td' + (aligns[ci] ? ' style="text-align:' + aligns[ci] + '"' : '') + '>' +
            inline(esc(r[ci] || '')) + '</td>').join('') + '</tr>';
        });
        t += '</tbody></table></div>';
        out.push(t);
        continue;
      }

      flushQuote();
      para.push(line);
    }

    closeCode();
    flushPara();
    flushQuote();
    closeLists();
    return out.join('');
  }

  // Copy a code block's contents (button lives in the rendered header)
  window.copyCodeBlock = function (btn) {
    const block = btn.closest('.md-code-block');
    const codeEl = block && block.querySelector('.md-code code');
    if (!codeEl || !navigator.clipboard) return;
    navigator.clipboard.writeText(codeEl.textContent).then(() => {
      btn.textContent = 'copied';
      setTimeout(() => { btn.textContent = 'copy'; }, 1400);
    }).catch(() => {});
  };

  window.renderMarkdown = render;
})();
