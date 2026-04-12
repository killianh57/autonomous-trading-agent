#!/usr/bin/env python3
"""
auto_fix.py - Service technique autonome
Detecte et corrige les erreurs Python a chaque push.
Envoie une alerte Telegram si fix applique.
"""
import ast, re, os, sys, json, base64, subprocess, requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT   = os.getenv("TELEGRAM_CHAT_ID", "")
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

# ─── SCAN: undefined CAPS constants ───
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
    undef = []
    lines = code.split("\n")
    for name in sorted(used - defined - skip):
        lns = [i+1 for i, l in enumerate(lines) if re.search(r"\b"+name+r"\b", l) and not l.strip().startswith("#")]
        if lns:
            undef.append((name, lns))
    return undef, None

# ─── NON-ASCII SCAN ───
def find_non_ascii(code):
    bad = [(i+1, repr(c)) for i, c in enumerate(code) if ord(c) > 127]
    return bad

# ─── SYNTAX CHECK ───
def check_syntax(code, path):
    try:
        ast.parse(code)
        return None
    except SyntaxError as e:
        return f"{path} - SyntaxError line {e.lineno}: {e.msg}"

# ─── COLLECT ALL PY FILES ───
def get_py_files():
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD~1", "HEAD"],
        capture_output=True, text=True
    )
    changed = [f for f in result.stdout.strip().split("\n") if f.endswith(".py") and os.path.exists(f)]
    if not changed:
        # fallback: all py files
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

    all_errors = []
    fixes_applied = []

    for fpath in py_files:
        if not os.path.exists(fpath):
            continue
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            code = f.read()

        lines = code.split("\n")

        # 1. Syntax
        syntax_err = check_syntax(code, fpath)
        if syntax_err:
            all_errors.append(("SYNTAX", fpath, syntax_err, None))
            continue

        # 2. Non-ASCII
        bad_chars = find_non_ascii(code)
        if bad_chars:
            # Auto-fix: remove non-ascii
            fixed = "".join(c if ord(c) <= 127 else " " for c in code)
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(fixed)
            fixes_applied.append(f"{fpath}: removed {len(bad_chars)} non-ASCII chars")
            code = fixed
            lines = code.split("\n")

        # 3. Undefined constants - report only (cant auto-fix without knowing value)
        undef, err = find_undefined_constants(code)
        if undef:
            for name, lns in undef:
                all_errors.append(("UNDEF", fpath, name, lns))

    # ─── REPORT ───
    if not all_errors and not fixes_applied:
        print("All clean.")
        telegram(f"<b>OK {REPO}</b>\nAudit Python : aucune erreur detectee.")
        return

    # Build report
    lines_msg = [f"<b>Service Technique - {REPO}</b>"]

    if fixes_applied:
        lines_msg.append("\n<b>Auto-fixes appliques :</b>")
        for fix in fixes_applied:
            lines_msg.append(f"  - {fix}")

    if all_errors:
        lines_msg.append("\n<b>Erreurs detectees :</b>")
        for kind, fpath, name, lns in all_errors:
            if kind == "SYNTAX":
                lines_msg.append(f"  SYNTAX {fpath}: {name}")
            elif kind == "UNDEF":
                lines_msg.append(f"  UNDEF {fpath}: {name} (L{lns})")

    lines_msg.append("\nAction requise : corriger et push.")

    msg = "\n".join(lines_msg)
    print(msg)
    telegram(msg)

    # Commit non-ascii fixes if any
    if fixes_applied:
        subprocess.run(["git", "config", "user.email", "bot@autofix.com"])
        subprocess.run(["git", "config", "user.name", "AutoFix Bot"])
        subprocess.run(["git", "add", "-A"])
        subprocess.run(["git", "commit", "-m", "fix: auto-remove non-ASCII characters"])
        subprocess.run(["git", "push"])

    # Exit 1 if unresolved errors (makes GH Action fail = visible)
    if all_errors:
        sys.exit(1)

if __name__ == "__main__":
    main()
