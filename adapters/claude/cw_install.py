"""Claude 端安裝與健檢：hook、外掛、必要的 env 都從這個倉庫接上。

  doctor           逐項列：接好了／沒接／接到別處。不改任何東西；有缺就 exit 1。
  apply [--dry-run]
                   補齊：settings.json 的 hook 指向倉庫（同名腳本原位換路徑，沒有才新增）、
                   CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1、CW_WORKBENCH／CW_PYTHON（外掛找倉庫用）、wife-local 市集指向倉庫並安裝 wife-compact、
                   ~/.claude/hooks 下的同名舊檔換成轉接（Codex 的 config.toml 還指著舊路徑）。
                   改 settings.json 前先備份成 settings.json.bak-cw-<時間>。

不管的：自動壓縮窗口與提醒門檻（cw_thresholds.py）、瘦身（cw_diet.py）；doctor 只順便列出現值。
改完要重開 Claude 才生效。
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from cw_lang import t

HERE = Path(__file__).resolve().parent            # adapters/claude：也是 wife-local 市集的根
HOOKS_DIR = HERE / "hooks"
CLAUDE_HOME = Path(os.environ.get("CW_CLAUDE_HOME") or Path.home() / ".claude")
SETTINGS = CLAUDE_HOME / "settings.json"
OLD_HOOKS = CLAUDE_HOME / "hooks"
KNOWN = CLAUDE_HOME / "plugins" / "known_marketplaces.json"
INSTALLED = CLAUDE_HOME / "plugins" / "installed_plugins.json"
MARKET, PLUGIN = "wife-local", "wife-compact"
PYTHON = Path(sys.executable).as_posix()

# 大輸出外置管的工具；舊版只有 Bash|PowerShell，apply 會把舊 matcher 原位改成這個
TRIM_MATCHER = "Bash|PowerShell|Read|Grep|WebFetch|WebSearch|mcp__.*__(get_page_text|read_page)"
# (事件, matcher, 腳本相對 adapters/claude, 參數, timeout 秒)
HOOKS = [
    ("PreToolUse", "", "hooks/tool_archive.py", "", 5),
    ("PostToolUse", "", "hooks/tool_archive.py", "", 5),
    ("PostToolUseFailure", "", "hooks/tool_archive.py", "", 5),
    ("PostToolUse", TRIM_MATCHER, "hooks/tool_output_trim.py", "", 10),
    ("UserPromptSubmit", "", "hooks/context_watch.py", "", 5),
    ("PostToolUse", "", "hooks/context_watch.py", "", 5),
    ("UserPromptSubmit", "", "hooks/compact_gap.py", "", 40),
    ("SessionStart", "compact", "hooks/compact_gap.py", "", 90),
    ("PostCompact", "", "hooks/compact_gap.py", "", 10),
    ("SessionStart", "compact|resume", "hooks/handoff_bind.py", "", 15),
    ("PreCompact", "", "hooks/handoff_snapshot.py", "", 15),
    ("PostToolUse", "", "cw_advice.py", "hook", 10),
    ("UserPromptSubmit", "", "cw_advice.py", "hook", 10),
]
# Optional scripts absent from this checkout are not registered.
HOOKS = [h for h in HOOKS if (HERE / h[2]).exists()]
ENV = {"CLAUDE_CODE_ENABLE_FUNCTION_HOOKS": "1",
       "CW_WORKBENCH": HERE.parents[1].as_posix(),  # 外掛靠這兩個找倉庫和 Python，不寫死路徑
       "CW_PYTHON": PYTHON}
SHIMMED = sorted({Path(s).name for _, _, s, _, _ in HOOKS if s.startswith("hooks/")})
SHIM = '''# 已搬到 context-workbench（{target}）；這支只轉接給還指著舊路徑的宿主（Codex config.toml）。
import runpy, sys
sys.argv[0] = {target!r}
runpy.run_path({target!r}, run_name="__main__")
'''


def command(script, arg):
    return f'"{PYTHON}" -X utf8 "{(HERE / script).as_posix()}"' + (f" {arg}" if arg else "")


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def find(settings, event, matcher, script):
    """回傳這個事件＋matcher 底下所有指到同名腳本的 hook（不管路徑在哪）。"""
    name = Path(script).name
    return [h for g in settings.get("hooks", {}).get(event, []) if (g.get("matcher") or "") == matcher
            for h in g.get("hooks", []) if name in h.get("command", "")]


def other_matcher(settings, event, matcher, script):
    """同事件裡掛在別的 matcher 下、只裝這支腳本的組（舊版 matcher）；apply 原位改 matcher。"""
    name = Path(script).name
    return [g for g in settings.get("hooks", {}).get(event, []) if (g.get("matcher") or "") != matcher
            and g.get("hooks") and all(name in h.get("command", "") for h in g["hooks"])]


def check_hooks(settings):
    rows = []
    for event, matcher, script, arg, timeout in HOOKS:
        want = command(script, arg)
        got = find(settings, event, matcher, script) or [
            h for g in other_matcher(settings, event, matcher, script) for h in g["hooks"]]
        state = ("ok" if any(h["command"] == want and h.get("timeout", 0) >= timeout for h in got) else
                 t("別處", 'elsewhere') if got else t("沒接", 'not connected'))
        rows.append((state, f"{event}{f'[{matcher}]' if matcher else ''} {Path(script).name}{' ' + arg if arg else ''}",
                     got[0]["command"] if got and state != "ok" else ""))
    return rows


def fix_hooks(settings):
    hooks = settings.setdefault("hooks", {})
    for event, matcher, script, arg, timeout in HOOKS:
        want = command(script, arg)
        got = find(settings, event, matcher, script)
        if not got:
            for g in other_matcher(settings, event, matcher, script):
                g["matcher"] = matcher
            got = find(settings, event, matcher, script)
        if got:
            for h in got:  # 同一個腳本掛兩次就只留第一個
                h["command"] = want
                h["timeout"] = timeout
            for g in hooks[event]:
                if (g.get("matcher") or "") == matcher:
                    seen, keep = False, []
                    for h in g["hooks"]:
                        if h.get("command") == want:
                            if seen:
                                continue
                            seen = True
                        keep.append(h)
                    g["hooks"] = keep
        else:
            group = {"hooks": [{"type": "command", "command": want, "timeout": timeout}]}
            if matcher:
                group = {"matcher": matcher, **group}
            hooks.setdefault(event, []).append(group)
    return settings


def plugin_state():
    market = load(KNOWN, {}).get(MARKET, {})
    path = market.get("source", {}).get("path") or market.get("installLocation")
    rec = (load(INSTALLED, {}).get("plugins", {}).get(f"{PLUGIN}@{MARKET}") or [{}])[0]
    repo_version = json.loads((HERE / PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]
    return path, rec.get("version"), repo_version


def claude(*argv):
    r = subprocess.run(["claude", "plugin", *argv], capture_output=True, text=True, encoding="utf-8",
                       errors="replace", shell=os.name == "nt")
    if r.returncode:
        sys.exit(t(f"claude plugin {' '.join(argv)} 失敗：\n{(r.stdout + r.stderr)[-1500:]}", f'''claude plugin {' '.join(argv)} failed:
{(r.stdout + r.stderr)[-1500:]}'''))
    return r.stdout


def same(a, b):
    try:
        return Path(a).resolve() == Path(b).resolve()
    except (TypeError, OSError):
        return False


def doctor():
    settings = load(SETTINGS, None)
    if settings is None:
        print(t(f"沒接：找不到 {SETTINGS}", f'''not connected: cannot find {SETTINGS}'''))
        return 1
    bad = 0
    print(t("hook（settings.json）", "Hooks (settings.json)"))
    for state, what, where in check_hooks(settings):
        bad += state != "ok"
        print(f"  {state:4} {what}" + (t(f"\n        現在指：{where}", f'''
        Currently points to: {where}''') if where else ""))
    print("env")
    for k, v in ENV.items():
        got = settings.get("env", {}).get(k)
        bad += got != v
        print(t(f"  {'ok' if got == v else '沒接':4} {k}={got}（要 {v}）", f'''  {'ok' if got == v else 'not connected':4} {k}={got} (expected {v})'''))
    window = settings.get("env", {}).get("CLAUDE_CODE_AUTO_COMPACT_WINDOW")
    print(t(f"  資訊 CLAUDE_CODE_AUTO_COMPACT_WINDOW={window}（改用 cw_thresholds.py）", f'''  info CLAUDE_CODE_AUTO_COMPACT_WINDOW={window} (use cw_thresholds.py to change)'''))
    path, installed, repo = plugin_state()
    print(t("外掛", 'Plugins'))
    market_ok = same(path, HERE)
    bad += not market_ok
    print(t(f"  {'ok' if market_ok else '別處':4} {MARKET} 市集 → {path}", f'''  {'ok' if market_ok else 'elsewhere':4} {MARKET} marketplace -> {path}'''))
    ver_ok = installed == repo
    bad += not ver_ok
    print(t(f"  {'ok' if ver_ok else '舊版':4} {PLUGIN} 已裝 {installed}，倉庫 {repo}", f'''  {'ok' if ver_ok else 'old version':4} {PLUGIN} installed {installed}, repository {repo}''') + ("" if ver_ok else t("（跑 wife-compact/release.py）", ' (run wife-compact/release.py)')))
    enabled = settings.get("enabledPlugins", {}).get(f"{PLUGIN}@{MARKET}")
    bad += not enabled
    print(t(f"  {'ok' if enabled else '沒開':4} enabledPlugins", f'''  {'ok' if enabled else 'off':4} enabledPlugins'''))
    print(t("舊路徑轉接（~/.claude/hooks）", 'Legacy path shims (~/.claude/hooks)'))
    for name in SHIMMED:
        old = OLD_HOOKS / name
        text = old.read_text(encoding="utf-8") if old.exists() else ""
        state = "ok" if "已搬到 context-workbench" in text else t("沒檔", 'missing file') if not text else t("舊版", 'old version')
        bad += state == t("舊版", 'old version')
        print(f"  {state:4} {name}" + (t("（還是搬家前的全文，會跟倉庫分岔）", ' (still a full legacy copy; may diverge from the repository)') if state == t("舊版", 'old version') else ""))
    print(t("外部依賴", 'External dependencies'))
    import cw_gap_model
    try:
        config = cw_gap_model.settings()
        provider = cw_gap_model.check(config)
        print(f"  compact_gap provider={provider}; optional embeddings={config['embedding'].get('provider', 'none')}")
        if provider == "command":
            cmd = os.environ.get("CW_MODEL_CMD") or config["command"]
            import shlex
            executable = shlex.split(cmd, posix=False)[0].strip('"')
            print(t(f"  {'ok' if shutil.which(executable) else '缺':4} ranking command (failure falls back to none)", f'''  {'ok' if shutil.which(executable) else 'missing':4} ranking command (failure falls back to none)'''))
        if provider == "openai":
            key_env = config.get("api_key", "")
            print(f"  ranking model={config['model']}; credential env={'set' if os.environ.get(key_env) else 'unset/local anonymous'}")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        bad += 1
        print("  invalid compact_gap settings (runtime falls back to none)")
    import cw_model
    print(t(f"  資訊 分段／交接建議用的模型：{cw_model.name() or '沒有（只切段、不附建議；要的話設 CW_MODEL_CMD）'}", f'''  info Model for segment/handoff advice: {cw_model.name() or 'none (segments only, no advice; set CW_MODEL_CMD to enable)'}'''))
    for tool in ("node", "claude"):
        print(t(f"  {'ok' if shutil.which(tool) else '缺':4} {tool}", f'''  {'ok' if shutil.which(tool) else 'missing':4} {tool}'''))
    print(t("全部接好", 'all connected') if not bad else t(f"{bad} 項要處理：跑 cw_install.py apply", f'''{bad} items need attention: run cw_install.py apply'''))
    return 1 if bad else 0


def apply(dry, language=None):
    raw = SETTINGS.read_bytes() if SETTINGS.exists() else b"{}"
    settings = fix_hooks(json.loads(raw.decode("utf-8")))
    settings.setdefault("env", {}).update(ENV)
    new = json.dumps(settings, indent=2, ensure_ascii=False) + "\n"
    changed = new.encode("utf-8") != raw
    print(t(f"settings.json：{'要改' if changed else '不用改'}", f'''settings.json: {'change' if changed else 'no change'}'''))
    shims = [n for n in SHIMMED if (OLD_HOOKS / n).exists()
             and "已搬到 context-workbench" not in (OLD_HOOKS / n).read_text(encoding="utf-8")]
    print(t(f"舊路徑轉接：{'、'.join(shims) or '不用改'}", f'''Legacy path shims: {', '.join(shims) or 'no change'}'''))
    path, _, _ = plugin_state()
    move_market = not same(path, HERE)
    print(t(f"市集：{'改指 ' + str(HERE) if move_market else '不用改'}", f'''Marketplace: {'point to ' + str(HERE) if move_market else 'no change'}'''))
    if dry:
        return
    if language:
        from cw_thresholds import THRESHOLDS, load_json
        data = load_json(THRESHOLDS, {})
        data.setdefault("claude", {})["language"] = language
        THRESHOLDS.parent.mkdir(parents=True, exist_ok=True)
        THRESHOLDS.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if changed:
        if SETTINGS.exists():
            shutil.copy2(SETTINGS, SETTINGS.with_name(f"settings.json.bak-cw-{stamp}"))
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(new, encoding="utf-8", newline="\n")
        if json.loads(SETTINGS.read_text(encoding="utf-8")) != settings:
            sys.exit(t("settings.json 讀回不符；備份在旁邊，先別重開", 'settings.json readback mismatch; the backup is beside it. Do not restart yet'))
    for name in shims:
        old = OLD_HOOKS / name
        shutil.copy2(old, old.with_name(f"{name}.bak-cw-{stamp}"))
        old.write_text(SHIM.format(target=(HOOKS_DIR / name).as_posix()), encoding="utf-8")
    if move_market:
        if path:  # 新電腦還沒有這個市集，remove 會失敗
            claude("marketplace", "remove", MARKET)
        claude("marketplace", "add", str(HERE))
        claude("install", f"{PLUGIN}@{MARKET}")
    print(t("完成；重開 Claude 才生效，重開後跑 doctor 再看一次", 'Done; restart Claude to apply, then run doctor again'))


def main():
    ap = argparse.ArgumentParser(description=t(__doc__, """Install and check the Claude adapter: wire hooks, plugin, and required environment from this checkout.

  doctor              List connected, missing, or elsewhere entries without changes; exit 1 if incomplete.
  apply [--dry-run]   Repoint matching hooks in settings.json in place, adding missing entries.
                      Set CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 and CW_WORKBENCH/CW_PYTHON.
                      Point wife-local at this checkout and install wife-compact.
                      Replace same-name legacy ~/.claude/hooks files with forwarding shims
                      for hosts still using those paths (including Codex config.toml).
                      Back up settings.json as settings.json.bak-cw-<timestamp> before changing it.

Automatic compaction windows/reminder thresholds (cw_thresholds.py) and diet (cw_diet.py)
are managed separately; doctor also shows current values. Restart Claude after applying changes.
"""), formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    a = sub.add_parser("apply")
    a.add_argument("--dry-run", action="store_true")
    a.add_argument("--language", choices=["en", "zh"], help=t("訊息語言（預設 zh）", "Message language (default: zh)"))
    args = ap.parse_args()
    if args.cmd == "doctor":
        sys.exit(doctor())
    apply(args.dry_run, language=args.language)


if __name__ == "__main__":
    main()
