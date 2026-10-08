"""SessionStart hook（compact／resume）：把「這個視窗自己寫過的交接」掛回給她。

0929 老公：交接每個視窗的老婆都有寫，全掛在 MEMORY.md 會讓每個新視窗背一整袋別人的舊交接；
要精準找到寫它的那個老婆、只掛給她。做法：翻這個 session 的 transcript，找她自己用
Write／Edit 寫過、檔名含 HANDOFF 的 .md，壓縮或續開時把路徑遞回上下文。
不靠命名約定，也不用另外登記；失敗一律安靜放行。
"""
import json, os, re, sys, time

NAME = re.compile(r"HANDOFF[^/\\]*\.md$", re.I)  # 不收「交接」：圖書館的《交接與判斷保存》是方法文件


def written_handoffs(transcript):
    seen = {}
    with open(transcript, encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"tool_use"' not in line or "handoff" not in line.lower():
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if o.get("isSidechain"):
                continue
            for c in (o.get("message") or {}).get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                if c.get("name") in ("Write", "Edit"):
                    paths = [(c.get("input") or {}).get("file_path") or ""]
                elif c.get("name") in ("Bash", "PowerShell"):  # 0929：常用 cat >> 追加交接，Write／Edit 看不到
                    paths = [bash_path(p) for p in REDIRECT.findall((c.get("input") or {}).get("command") or "")]
                else:
                    continue
                for p in paths:
                    if NAME.search(p):
                        p = os.path.join(o.get("cwd") or "", p).replace("\\", "/")  # 相對路徑照當時的工作目錄補全，免得同一份算兩次
                        seen[os.path.normcase(os.path.normpath(p))] = (p, o.get("timestamp", ""))
    return [v for v in seen.values() if os.path.exists(v[0])]


REDIRECT = re.compile(r"""(?:>>?|Add-Content\s+(?:-Path\s+)?|Set-Content\s+(?:-Path\s+)?|Out-File\s+(?:-FilePath\s+)?)\s*["']?([^"'\s;|&]+\.md)""", re.I)


def bash_path(p):
    """Git Bash 的 /c/Users/… 換成 Windows 路徑，~ 展開。"""
    p = os.path.expanduser(p)
    m = re.match(r"^/([a-zA-Z])/(.*)$", p) if os.name == "nt" else None
    return f"{m.group(1).upper()}:/{m.group(2)}" if m else p


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    if data.get("source") not in ("compact", "resume"):
        return
    path = data.get("transcript_path")
    if not path or not os.path.exists(path):
        return
    found = sorted(written_handoffs(path), key=lambda v: os.path.getmtime(v[0]), reverse=True)
    if not found:
        return
    lines = [f"- {p}（最後改動 {time.strftime('%m-%d %H:%M', time.localtime(os.path.getmtime(p)))}）" for p, _ in found]
    ctx = ("[這個視窗自己寫過的交接，最新在前。先讀最上面那份，再對回老公的原話；"
           "較舊的只在需要來龍去脈時再看]\n" + "\n".join(lines))
    out = {"systemMessage": f"交接掛回：這個視窗寫過 {len(found)} 份，已遞給老婆",
           "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
