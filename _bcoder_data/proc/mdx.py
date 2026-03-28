"""
mdx — render last LLM reply as Markdown in the browser.
Usage: /proc run mdx
"""
import sys, re, tempfile, webbrowser


def fix_mermaid(text):
    """Replace spaces with underscores in bare multi-word Mermaid node names."""
    ARROW = re.compile(r'(\s*(?:-->|<--|---|-\.->|===>?|==>|--o|--x|<-->)(?:\|[^|]*\|)?\s*)')
    SKIP  = re.compile(r'^(graph|flowchart|sequenceDiagram|classDiagram|stateDiagram'
                       r'|gantt|pie|erDiagram|mindmap|gitGraph|\s*%%)', re.I)

    def fix_node(token):
        t = token.strip()
        if not t or t.startswith('"') or re.match(r'^\w+[\[\({]', t):
            return token
        if ' ' in t:
            return token.replace(t, re.sub(r'\s+', '_', t))
        return token

    def fix_block(m):
        lines = []
        has_decl = False
        for line in m.group(1).splitlines():
            s = line.strip()
            if SKIP.match(s) and not s.startswith('%%'):
                has_decl = True
            if SKIP.match(s):
                lines.append(line)
            else:
                parts = ARROW.split(line)
                lines.append(''.join(fix_node(p) if not ARROW.match(p) else p for p in parts))
        if not has_decl:
            lines.insert(0, 'graph LR')
        return '```mermaid\n' + '\n'.join(lines) + '\n```'

    return re.sub(r'```mermaid\n(.*?)```', fix_block, text, flags=re.DOTALL)


text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
text = text.replace('\r\n', '\n')
text = fix_mermaid(text)

f = tempfile.NamedTemporaryFile(delete=False, suffix=".html", mode="w", encoding="utf-8")
f.write(f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>1bcoder</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.css">
<script src="https://cdn.jsdelivr.net/npm/marked@9/marked.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/katex.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/katex@0.16.9/dist/contrib/auto-render.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
  body {{ font-family: sans-serif; max-width: 860px; margin: 40px auto; padding: 0 24px; }}
  pre  {{ background: #f6f8fa; border-radius: 6px; padding: 16px; overflow-x: auto; }}
  code {{ background: #f0f0f0; padding: 2px 5px; border-radius: 3px; }}
  pre code {{ background: none; padding: 0; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #d0d7de; padding: 8px 12px; }}
  th {{ background: #f6f8fa; }}
</style>
</head>
<body>
<div id="out"></div>
<script>
  const text = {repr(text)};
  document.getElementById('out').innerHTML = marked.parse(text);

  if (typeof renderMathInElement !== 'undefined') {{
    renderMathInElement(document.getElementById('out'), {{
      delimiters: [
        {{left: '$$', right: '$$', display: true}},
        {{left: '$',  right: '$',  display: false}},
        {{left: '\\\\[', right: '\\\\]', display: true}},
        {{left: '\\\\(', right: '\\\\)', display: false}}
      ],
      throwOnError: false
    }});
  }}

  if (typeof mermaid !== 'undefined') {{
    mermaid.initialize({{ startOnLoad: false }});
    (async () => {{
      for (const [i, el] of [...document.querySelectorAll('pre code.language-mermaid')].entries()) {{
        const src = el.textContent.trim();
        const div = document.createElement('div');
        el.closest('pre').replaceWith(div);
        try {{
          const {{ svg }} = await mermaid.render('m' + i, src);
          div.innerHTML = svg;
        }} catch(e) {{
          div.textContent = '[mermaid] ' + e.message;
          div.style.color = 'red';
        }}
      }}
    }})();
  }}
</script>
</body>
</html>""")
f.close()
webbrowser.open("file://" + f.name)
print(f"[mdx] {f.name}")
