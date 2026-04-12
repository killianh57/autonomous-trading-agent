#!/usr/bin/env python3
"""
auto_fix.py v2 - Service technique autonome avec Claude
Detecte et corrige les erreurs Python a chaque push.
"""
import ast, re, os, sys, json, base64, subprocess, requests

TELEGRAM_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_KEY   = os.getenv("ANTHROPIC_API_KEY", "")
REPO            = os.getenv("REPO", "")

# ─── TELEGRAM ───
def telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        print("Telegram non configure")
        return
    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "HTML"},
        timeout=10
    )

# ─── CLAUDE GUESS VALUE ───
def claude_guess_value(name, context_lines, full_code):
    if not ANTHROPIC_KEY:
        return None
    prompt = (
        f"Tu es un expert Python. Dans ce code de trading bot, la constante '{name}' "
        f"est utilisee mais jamais definie.\n\n"
        f"Contexte (lignes autour de l'usage):\n{context_lines}\n\n"
        f"Debut du fichier (constantes existantes):\n{full_code[:3000]}\n\n"
        f"Reponds UNIQUEMENT avec la valeur Python a assigner. "
        f"Exemples valides: 20 | -0.10 | 0.005 | True | 'BTC-USD'\n"
        f"Pas d'explication, pas de commentaire, juste la valeur."
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 50,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=15
        )
        val = r.json()["content"][0]["text"].strip()
        # Validate: must be a valid Python literal
        ast.literal_eval(val)
        return val
    except Exception as e:
        print(f"Claude error for {name}: {e}")
        return None

# ─── INJECT CONSTANT ───
def inject_constant(code, name, value, comment=""):
    lines = code.split("\n")
    # Find last constant definition block (CAPS = value pattern)
    last_const_line = 0
    for i, line in enumerate(lines):
        if re.match(r"^[A-Z][A-Z_0-9]+\s*=", line.strip()):
            last_const_line = i
    insert_line = last_const_line + 1
    new_line = f"{name:<22} = {value}"
    if comment:
        new_line += f"  # {comment}"
    lines.insert(insert_line, new_line)
    return "\n".join(lines)

# ─── SYNTAX CHECK ───
def check_syntax(code, path):
    try:
        ast.parse(code)
        return None
    except SyntaxError as e:
        return f"SyntaxError line {e.lineno}: {e.msg}"

# ─── UNDEFINED CONSTANTS ───
def find_undefined_constants(code):
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return [], f"SyntaxError line {e.lineno}: {e.msg}"

    defined = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    defined.add(t.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Import):
            for a in node.names:
                defined.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                defined.add(a.asname or a.name)

    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if re.match(r"^[A-Z][A-Z_0-9]{2,}$", node.id):
                used.add(node.id)

    skip = {"True","False","None","NONE","GET","POST","PUT","DELETE","OK","EOF"}
    lines = code.split("\n")
    undef = []
    for name in sorted(used - defined - skip):
        lns = [i+1 for i, l in enumerate(lines) if re.search(r"\b"+name+r"\b", l) and not l.strip().startswith("#")]
        if lns:
            undef.append((name, lns))
    return undef, None

# ─── NON-ASCII ───
def find_non_ascii(code):
    return [(i+1, repr(c)) for i, c in enumerate(code) if ord(c) > 127]

# ─── GET PY FILES ───
def get_py_files():
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
        capture_output=True, text=True
    )
    changed = [f for f in result.stdout.strip().split("\n") if f.endswith(".py") and os.path.exists(f)]
    if not changed:
        changed = []
        for root, _, files in os.walk("."):
            if ".git" in root or "__pycache__" in root:
                continue
            for f in files:
                if f.endswith(".py"):
                    changed.append(os.path.join(root, f).lstrip("./"))
    return changed

# ─── MAIN ───
def main():
    py_files = get_py_files()
    if not py_files:
        print("No Python files to check.")
        return

    print(f"Checking {len(py_files)} file(s): {py_files}")

    fixes_applied = []
    errors_remaining = []
    files_modified = {}

    for fpath in py_files:
        if not os.path.exists(fpath):
            continue
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()

        lines = code.split("\n")

        # 1. Syntax check
        syntax_err = check_syntax(code, fpath)
        if syntax_err:
            errors_remaining.append(("SYNTAX", fpath, syntax_err))
            continue

        modified = False

        # 2. Non-ASCII auto-fix
        bad_chars = find_non_ascii(code)
        if bad_chars:
            code = "".join(c if ord(c) <= 127 else " " for c in code)
            fixes_applied.append(f"{fpath}: removed {len(bad_chars)} non-ASCII chars")
            modified = True

        # 3. Undefined constants - ask Claude
        undef, _ = find_undefined_constants(code)
        for name, lns in undef:
            context = "\n".join(lines[max(0, lns[0]-5):lns[0]+5])
            value = claude_guess_value(name, context, code)
            if value is not None:
                code = inject_constant(code, name, value, f"auto-fix by Claude")
                fixes_applied.append(f"{fpath}: injected {name} = {value}")
                modified = True
                print(f"Claude fixed: {name} = {value}")
            else:
                errors_remaining.append(("UNDEF", fpath, name, lns))

        if modified:
            files_modified[fpath] = code

    # Write modified files
    for fpath, new_code in files_modified.items():
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(new_code)

    # Commit if any fixes
    if files_modified:
        subprocess.run(["git", "config", "user.email", "autofix@bot.com"])
        subprocess.run(["git", "config", "user.name", "AutoFix Bot"])
        subprocess.run(["git", "add", "-A"])
        msg = "fix(auto): " + " | ".join(fixes_applied[:3])
        subprocess.run(["git", "commit", "-m", msg])
        subprocess.run(["git", "push"])

    # Telegram report
    if not fixes_applied and not errors_remaining:
        telegram(f"<b>OK {REPO}</b>\nAudit Python : aucune erreur detectee.")
        print("All clean.")
        return

    lines_msg = [f"<b>Service Technique - {REPO}</b>"]

    if fixes_applied:
        lines_msg.append("\n<b>Auto-fixes Claude :</b>")
        for fix in fixes_applied:
            lines_msg.append(f"  - {fix}")

    if errors_remaining:
        lines_msg.append("\n<b>Erreurs non resolues :</b>")
        for item in errors_remaining:
            if item[0] == "SYNTAX":
                lines_msg.append(f"  SYNTAX {item[1]}: {item[2]}")
            else:
                lines_msg.append(f"  UNDEF {item[1]}: {item[2]} (L{item[3]})")
        lines_msg.append("\nAction requise : corriger et push.")

    telegram("\n".join(lines_msg))
    print("\n".join(lines_msg))

    if errors_remaining:
        sys.exit(1)

if __name__ == "__main__":
    main()
