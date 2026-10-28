# Copyright 2026 Ubadream
# SPDX-License-Identifier: Apache-2.0
"""Which handoff files did this Codex thread itself write?

Reads the thread's rollout and keeps the targets of tool calls that wrote a file named
HANDOFF*.md (or <session id>.md) and finished successfully: apply_patch with its success receipt,
or a shell command with exit code 0. A call that only reads a handoff, writes some other file, or
fails does not count. Paths are resolved against the call's workdir or the turn's cwd, never by
running anything. Newest write first.

Why not just the newest HANDOFF*.md in the working directory: one thread may keep its handoff
elsewhere, and several threads may share one directory.
"""
from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

LITERAL = r"""(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')"""
PS_PATH = rf"(?:\(Join-Path\s+\$\w+\s+{LITERAL}\)|{LITERAL}|\$\w+)"


def literal(value):
    if value.startswith('"'):
        try:
            return json.loads(value)
        except ValueError:
            return None
    try:
        return ast.literal_eval(value) if value.startswith("'") else None
    except (ValueError, SyntaxError):
        return None


def call_units(payload, cwd):
    """(tool, text, workdir) for a direct tool call, or for each call inside an `exec` script."""
    name = payload.get("name", "").split(".")[-1]
    text = payload.get("input") or payload.get("arguments") or ""
    if not isinstance(text, str):
        return
    if name == "apply_patch":
        yield name, text, cwd
    elif name in ("exec_command", "shell", "shell_command"):
        try:
            args = json.loads(text)
        except ValueError:
            return
        command = args.get("cmd") or args.get("command") or ""
        if isinstance(command, list):
            command = " ".join(map(str, command))
        yield "exec_command", command, args.get("workdir") or cwd
    elif name == "exec":
        for match in re.finditer(r"tools\.apply_patch\(\s*(" + LITERAL + r")\s*\)", text):
            if (patch := literal(match[1])) is not None:
                yield "apply_patch", patch, cwd
        for match in re.finditer(r"tools\.exec_command\(\s*\{(.*?)\}\s*\)", text, re.S):
            body = match[1]
            cmd = re.search(r"""["']?cmd["']?\s*:\s*(""" + LITERAL + r")", body)
            workdir = re.search(r"""["']?workdir["']?\s*:\s*(""" + LITERAL + r")", body)
            if cmd and (command := literal(cmd[1])) is not None:
                yield "exec_command", command, (literal(workdir[1]) if workdir else None) or cwd


def ps_literal(value):
    # Unlike JS/JSON, PowerShell does not use backslash as a string escape.
    if value.startswith('"') and any(char in value for char in "$`"):
        return None  # interpolation cannot be resolved without running code
    return value[1:-1].replace("''", "'") if value.startswith("'") else value[1:-1]


REDIRECT = re.compile(r"(?:^|[\s;|&(])(?:\d?>>?|&>>?)\s*(" + LITERAL + r"|[^\s;|&<>()'\"]+)")
TEE = re.compile(r"\btee\s+(?:-a\s+|--append\s+)?(" + LITERAL + r"|[^\s;|&<>()'\"]+)")


def shell_targets(command, cwd):
    """Literal targets of shell / PowerShell / Python write statements; a nearby read is not a target."""
    command = re.sub(r"""(?ms)@(['"])\r?\n.*?^\s*\1@""", "''", command)  # here-strings are data
    # so are POSIX heredoc bodies; keep the rest of the line that opens one (cat > FILE <<'EOF')
    command = re.sub(r"""(?ms)<<-?\s*(['"]?)(\w+)\1([^\n]*)\n.*?^\s*\2\s*$""", r"\3", command)
    variables = {"pwd": cwd}
    for statement in re.split(r"[;\r\n]+", command):
        assignment = re.fullmatch(r"\s*\$(\w+)\s*=\s*(.*?)\s*", statement)
        if assignment:
            key, value = assignment[1].lower(), assignment[2]
            variables.pop(key, None)  # an unknown reassignment invalidates the old value
            if re.fullmatch(r"\(Get-Location\)\.Path|\$PWD(?:\.Path)?", value, re.I):
                variables[key] = cwd
            elif re.fullmatch(LITERAL, value):
                variables[key] = ps_literal(value)

        def resolve(expression):
            joined = re.fullmatch(r"\(Join-Path\s+\$(\w+)\s+(" + LITERAL + r")\)", expression, re.I)
            if joined:
                base, tail = variables.get(joined[1].lower()), ps_literal(joined[2])
                return os.path.join(base, tail) if base and tail else None
            return variables.get(expression[1:].lower()) if expression.startswith("$") else ps_literal(expression)

        dotnet = re.match(r"\s*\[(?:System\.)?IO\.File\]::(?:WriteAllText|AppendAllText|WriteAllLines|AppendAllLines)"
                          r"\(\s*(" + PS_PATH + r")\s*,", statement, re.I)
        content = re.match(r"\s*(?:Set-Content|Add-Content)\s+(?:(?:-LiteralPath|-Path)\s+)?(" + PS_PATH + r")(?=\s|$)",
                           statement, re.I)
        outfile = re.search(r"\|\s*(?:Set-Content|Add-Content|Out-File)\s+(?:(?:-LiteralPath|-FilePath|-Path)\s+)?("
                            + PS_PATH + r")(?=\s|$)", statement, re.I)
        target = dotnet or content or outfile
        if target and (path := resolve(target[1])):
            yield path
        for match in [*REDIRECT.finditer(statement), *TEE.finditer(statement)]:
            word = match[1]
            path = ps_literal(word) if word[0] in "'\"" else (None if "$" in word else word)
            if path:
                yield os.path.expanduser(path)
        python = re.match(r"\s*(?:Path|pathlib\.Path)\((" + LITERAL + r")\)\.write_(?:text|bytes)\(", statement)
        if python:
            try:
                yield ast.literal_eval(python[1])
            except (ValueError, SyntaxError):
                continue


def output_success(payload):
    """A completed shell result with exit code 0, or apply_patch's success receipt."""
    output = payload.get("output", "")
    blocks = output if isinstance(output, list) else [{"text": output}]
    codes, patch_ok = [], False
    for block in blocks:
        text = block.get("text", "") if isinstance(block, dict) else str(block)
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            obj = None
        if isinstance(obj, dict):
            if obj.get("isError"):
                return False
            if obj.get("exit_code") is not None:
                codes.append(obj["exit_code"])
            meta = obj.get("metadata")
            if isinstance(meta, dict) and meta.get("exit_code") is not None:
                codes.append(meta["exit_code"])
        codes.extend(int(n) for n in re.findall(r"Process exited with code (-?\d+)", text))
        patch_ok |= "Success. Updated the following files:" in text
    return all(code == 0 for code in codes) if codes else patch_ok


def written_handoffs(rollout, session_id=""):
    """Existing handoff files this thread wrote successfully, most recent write first."""
    pending, seen, cwd = {}, {}, ""
    with open(rollout, encoding="utf-8", errors="replace") as handle:
        for sequence, line in enumerate(handle):
            if not any(tag in line for tag in ('"session_meta"', '"turn_context"', '"response_item"')):
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue  # a live rollout may end with an unfinished line
            payload = obj.get("payload") or {}
            if obj.get("type") in {"session_meta", "turn_context"}:
                cwd = payload.get("cwd") or cwd
                continue
            if obj.get("type") != "response_item":
                continue
            kind, call_id = payload.get("type"), payload.get("call_id")
            if kind in {"function_call", "custom_tool_call"}:
                targets = []
                for tool, text, workdir in call_units(payload, cwd):
                    if tool == "apply_patch":
                        paths = re.findall(r"^\*\*\* (?:Add|Update) File: (.+?)\r?$", text, re.M)
                        paths += re.findall(r"^\*\*\* Move to: (.+?)\r?$", text, re.M)
                    else:
                        paths = shell_targets(text, workdir)
                    for path in paths:
                        if not path or any(char in path for char in "$`\n\r"):
                            continue
                        name = Path(path).name
                        if not (re.fullmatch(r".*HANDOFF.*\.md", name, re.I) or
                                session_id and name.lower() == f"{session_id.lower()}.md"):
                            continue
                        if not os.path.isabs(path) and not workdir:
                            continue
                        targets.append(os.path.abspath(os.path.join(workdir, path)))
                if targets and call_id:
                    pending[call_id] = (sequence, targets)
            elif kind in {"function_call_output", "custom_tool_call_output"}:
                candidate = pending.pop(call_id, None)
                if candidate and output_success(payload):
                    written_at, targets = candidate
                    for path in targets:
                        key = os.path.normcase(path)
                        if key not in seen or seen[key][0] < written_at:
                            seen[key] = (written_at, path)
    return [path for _, path in sorted(seen.values(), reverse=True) if os.path.isfile(path)]
