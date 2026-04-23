"""Search the web, fetch top N pages, summarize with LLM. Usage: /flow webask <question> [-d N]"""
import re as _re


def run(chat, args: str):
    depth = 3
    m = _re.search(r"-d\s+(\d+)", args)
    if m:
        depth = int(m.group(1))
        args = (args[:m.start()] + args[m.end():]).strip()
    question = args.strip()
    if not question:
        print("usage: /flow webask <question> [-d N]")
        return

    print(f"[webask] searching: {question} ...")
    try:
        results = chat._web_ddg_search(question, n=depth + 2)
    except Exception as e:
        print(f"[webask] search failed: {e}")
        return
    if not results:
        print("[webask] no search results")
        return

    fetched = []
    for title, url, snippet in results:
        if len(fetched) >= depth:
            break
        if not url.startswith("http"):
            continue
        print(f"[webask] fetching ({len(fetched)+1}/{depth}): {url}")
        try:
            import requests as _r
            resp = _r.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=12)
            text = chat._web_strip_html(resp.content)[:3000]
            fetched.append(f"## {title}\nSource: {url}\n\n{text}")
        except Exception as e:
            print(f"[webask] skip {url}: {e}")

    if not fetched:
        print("[webask] could not fetch any pages")
        return

    combined = "\n\n---\n\n".join(fetched)
    prompt = (
        f"Based on the following web content, answer this question:\n"
        f"{question}\n\n"
        f"Web content:\n{combined}"
    )
    temp_msgs = [{"role": "system", "content": chat._role},
                 {"role": "user",   "content": prompt}]
    chat._sep("AI")
    reply = chat._stream_chat(temp_msgs)
    if reply:
        chat.last_reply = reply
        chat._last_output = reply
        chat.messages.append({"role": "user",      "content": f"[webask: {question}]"})
        chat.messages.append({"role": "assistant", "content": reply})
