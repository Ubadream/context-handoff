"""wife-compact 發版：版號兩處一起加、驗證、用真對話模擬 A／C、更新安裝。一步失敗就停，不裝。

  python release.py [新版號，省略＝最後一碼加一] [--jsonl <拿來模擬的對話>] [--dry-run]

步驟：
  1. hooks/wife-compact.ts 的 VERSION 和 .claude-plugin/plugin.json 的 version 改成同一個
  2. claude plugin validate .（失敗照樣會被 update 裝上去，所以一定先過這關；0.1.8 出過事）
  3. node tests/simulate.mjs <最新的對話> A／C：每種都要跑完、孤兒 tool_use／tool_result 都是 0
  4. claude plugin update wife-compact@wife-local
裝完要重開 Claude 才生效（這步只有老公能按）。
--dry-run 只跑 2、3，不改版號、不裝。
"""
import argparse
import glob
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
TS = ROOT / "hooks" / "wife-compact.ts"
MANIFEST = ROOT / ".claude-plugin" / "plugin.json"
VERSION_LINE = re.compile(r"const VERSION = '([\d.]+)';")


def run(argv, **kw):
    r = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace", shell=(os.name == "nt" and argv[0] == "claude"), **kw)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def bump(current, wanted):
    if wanted:
        return wanted
    parts = current.split(".")
    parts[-1] = str(int(parts[-1]) + 1)
    return ".".join(parts)


def main():
    ap = argparse.ArgumentParser(description="wife-compact 發版")
    ap.add_argument("version", nargs="?")
    ap.add_argument("--jsonl", help="模擬用的對話；省略＝最近改過的 Claude 對話")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ts = TS.read_text(encoding="utf-8")
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    code_version = VERSION_LINE.search(ts).group(1)
    if code_version != manifest["version"]:
        sys.exit(f"版號兩處不一致：程式 {code_version}、plugin.json {manifest['version']}；先對齊再發")
    new = code_version if args.dry_run else bump(code_version, args.version)
    if not args.dry_run:
        TS.write_text(VERSION_LINE.sub(f"const VERSION = '{new}';", ts), encoding="utf-8", newline="")
        manifest["version"] = new
        MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"版號 {code_version} → {new}")

    code, out = run(["claude", "plugin", "validate", "."])
    if code != 0 or "Validation passed" not in out:
        sys.exit(f"validate 沒過，不裝：\n{out[-1500:]}")
    print("validate 通過")

    jsonl = args.jsonl or max(glob.glob(str(Path.home() / ".claude" / "projects" / "*" / "*.jsonl")), key=os.path.getmtime)
    for mode in "AC":
        code, out = run(["node", "--experimental-strip-types", "tests/simulate.mjs", jsonl, mode])
        orphans = re.search(r"孤兒 tool_use: (\d+) 孤兒 tool_result: (\d+)", out)
        if code != 0 or not orphans or orphans.groups() != ("0", "0"):
            sys.exit(f"模擬 {mode} 不過，不裝：\n{out[-1500:]}")
        size = re.search(r"輸入 .*", out)
        print(f"模擬 {mode}：{size.group(0) if size else ''}，孤兒 0")

    if args.dry_run:
        print("dry-run：沒改版號、沒裝")
        return
    code, out = run(["claude", "plugin", "update", "wife-compact@wife-local"])
    if code != 0:
        sys.exit(f"update 失敗：\n{out[-1500:]}")
    print(out.strip().splitlines()[-1])
    print(f"已裝 {new}；重開 Claude 才生效")


if __name__ == "__main__":
    main()
