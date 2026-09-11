// ============================================================
// Markdown Renderer for Chat Messages & Telemetry Details
// Uses vendored marked.js with customized safe HTML rendering,
// custom code blocks with copy buttons, and fallback parser.
// ============================================================

(function () {
  'use strict';

  function escapeHtml(str) {
    if (str === undefined || str === null) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  // Copy button handler for code blocks
  window.copyCodeBlock = function (btn) {
    const block = btn.closest('.md-code-block');
    const codeEl = block && block.querySelector('.md-code code');
    if (!codeEl || !navigator.clipboard) return;
    navigator.clipboard.writeText(codeEl.textContent).then(() => {
      btn.textContent = 'copied';
      setTimeout(() => { btn.textContent = 'copy'; }, 1400);
    }).catch(() => {});
  };

  // Configure marked if available
  let markedConfigured = false;
  function ensureMarkedConfigured() {
    if (markedConfigured || typeof window.marked === 'undefined') return;
    try {
      const renderer = {
        html({ text }) {
          // Escape raw HTML so it displays safely as code/text rather than executing or breaking DOM
          return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        },
        code({ text, lang }) {
          const rawLang = (lang || 'text').trim().split(/\s+/)[0];
          const cleanLang = rawLang.replace(/[^a-zA-Z0-9_#-]/g, '') || 'text';
          const escCode = text
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;');
          return '\n<div class="md-code-block">\n' +
            '  <div class="md-code-header">\n' +
            '    <span class="md-code-lang">' + cleanLang + '</span>\n' +
            '    <button type="button" class="md-copy-btn" onclick="copyCodeBlock(this)">copy</button>\n' +
            '  </div>\n' +
            '  <pre class="md-code"><code class="language-' + cleanLang + '">' + escCode + '</code></pre>\n' +
            '</div>\n';
        },
        codespan({ text }) {
          return '<code class="md-inline-code">' + escapeHtml(text) + '</code>';
        },
        table(token) {
          // marked hands the renderer cell TOKENS, not rendered HTML. Joining
          // them directly is what produced rows of "[object Object]".
          const self = this;
          const cell = (c, tag) => {
            if (c == null) return '<' + tag + '></' + tag + '>';
            if (typeof c === 'string') return c;
            const align = c.align ? ' style="text-align:' + c.align + '"' : '';
            let inner = '';
            if (c.tokens && self.parser) inner = self.parser.parseInline(c.tokens);
            else if (typeof c.text === 'string') inner = escapeHtml(c.text);
            return '<' + tag + align + '>' + inner + '</' + tag + '>';
          };
          const header = Array.isArray(token.header)
            ? '<tr>' + token.header.map(c => cell(c, 'th')).join('') + '</tr>'
            : String(token.header || '');
          const rows = Array.isArray(token.rows)
            ? token.rows.map(r => '<tr>' + (Array.isArray(r) ? r.map(c => cell(c, 'td')).join('') : '') + '</tr>').join('')
            : String(token.rows || '');
          return '<div class="md-table-wrap"><table><thead>' + header + '</thead><tbody>' + rows + '</tbody></table></div>';
        },
        link({ href, title, text, tokens }) {
          // `text` is the raw markdown source of the label, not parsed HTML, so
          // emitting it directly let `[<img onerror=...>](ok)` execute. Parse the
          // tokens instead, and allowlist the scheme rather than denylisting a
          // few, after resolving entities so `javascript&#58;` cannot slip past.
          const raw = (href || '').trim();
          const probe = raw.replace(/&[#a-z0-9]+;/gi, '');
          const ok = /^(https?:|mailto:|#|\/|\.{0,2}\/)/i.test(probe);
          const safeHref = ok ? escapeHtml(raw) : '#';
          const titleAttr = title ? ' title="' + escapeHtml(title) + '"' : '';
          const label = (tokens && this.parser) ? this.parser.parseInline(tokens) : escapeHtml(text || '');
          return '<a href="' + safeHref + '" target="_blank" rel="noopener"' + titleAttr + '>' + label + '</a>';
        },
        image({ href, title, text }) {
          // Same allowlist for image sources.
          const raw = (href || '').trim();
          const probe = raw.replace(/&[#a-z0-9]+;/gi, '');
          const ok = /^(https?:|#|\/|\.{0,2}\/|data:image\/)/i.test(probe);
          if (!ok) return escapeHtml(text || '');
          const titleAttr = title ? ' title="' + escapeHtml(title) + '"' : '';
          return '<img src="' + escapeHtml(raw) + '" alt="' + escapeHtml(text || '') + '"' + titleAttr + '>';
        }
      };

      window.marked.use({ renderer, gfm: true, breaks: true });
      markedConfigured = true;
    } catch (err) {
      console.warn('Could not configure marked renderer:', err);
    }
  }

  // Fallback inline parser
  function fallbackInline(source) {
    const store = [];
    return source
      .replace(/`([^`]+)`/g, (m, c) => '\x01' + (store.push(c) - 1) + '\x01')
      .replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (m, alt, url) => '<img class="md-img" alt="' + alt + '" src="' + url + '">')
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, text, url) => '<a href="' + url + '" target="_blank" rel="noopener">' + text + '</a>')
      .replace(/\*\*([\s\S]+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^\w*])__([\s\S]+?)__(?!\w)/g, '$1<strong>$2</strong>')
      .replace(/(^|[^*\w])\*([^*\n]+?)\*(?!\*)/g, '$1<em>$2</em>')
      .replace(/(^|[^_\w])_([^_\n]+?)_(?!\w)/g, '$1<em>$2</em>')
      .replace(/~~([\s\S]+?)~~/g, '<del>$1</del>')
      .replace(/\x01(\d+)\x01/g, (m, n) => '<code class="md-inline-code">' + escapeHtml(store[Number(n)]) + '</code>');
  }

  // Fallback block parser
  function fallbackRender(src) {
    if (!src || !String(src).trim()) return '';
    const lines = String(src).replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let code = null;

    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];

      // Code fences
      if (code) {
        if (/^\s*(`{3,}|~{3,})\s*$/.test(line)) {
          const lang = code.lang || 'text';
          out.push(
            '<div class="md-code-block">' +
            '<div class="md-code-header"><span class="md-code-lang">' + escapeHtml(lang) + '</span>' +
            '<button type="button" class="md-copy-btn" onclick="copyCodeBlock(this)">copy</button></div>' +
            '<pre class="md-code"><code>' + escapeHtml(code.buf.join('\n')) + '</code></pre></div>'
          );
          code = null;
        } else {
          code.buf.push(line);
        }
        continue;
      }

      const fence = line.match(/^\s*(`{3,}|~{3,})\s*([a-zA-Z0-9_#-]*)/);
      if (fence) {
        code = { lang: fence[2] || 'text', buf: [] };
        continue;
      }

      if (!line.trim()) {
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        const lvl = Math.min(heading[1].length, 4);
        out.push('<h' + lvl + '>' + fallbackInline(escapeHtml(heading[2])) + '</h' + lvl + '>');
        continue;
      }

      out.push('<p>' + fallbackInline(escapeHtml(line)) + '</p>');
    }

    if (code) {
      out.push(
        '<div class="md-code-block">' +
        '<div class="md-code-header"><span class="md-code-lang">' + escapeHtml(code.lang || 'text') + '</span></div>' +
        '<pre class="md-code"><code>' + escapeHtml(code.buf.join('\n')) + '</code></pre></div>'
      );
    }

    return out.join('');
  }

  // Primary entrypoint
  window.renderMarkdown = function (src) {
    if (!src || !String(src).trim()) return '';
    ensureMarkedConfigured();

    if (typeof window.marked !== 'undefined' && typeof window.marked.parse === 'function') {
      try {
        return window.marked.parse(String(src));
      } catch (err) {
        console.error('marked.parse error, falling back:', err);
      }
    }

    return fallbackRender(src);
  };
})();
