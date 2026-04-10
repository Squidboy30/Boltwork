import json, os, subprocess, sys, textwrap, time
from datetime import datetime
from pathlib import Path

MAX_REPAIR_ATTEMPTS = 3
REQUIRED_FILES = ["main.py", "requirements.txt", "tests/test_api.py", "tests/conftest.py"]


class C:
    GREEN = "\033[92m"; RED = "\033[91m"; YELLOW = "\033[93m"
    BLUE = "\033[94m"; BOLD = "\033[1m"; RESET = "\033[0m"


def ok(m):   print(f"  {C.GREEN}OK{C.RESET}    {m}")
def fail(m): print(f"  {C.RED}FAIL{C.RESET}  {m}")
def info(m): print(f"  {C.BLUE}--{C.RESET}    {m}")
def warn(m): print(f"  {C.YELLOW}!{C.RESET}     {m}")
def bold(m): print(f"\n{C.BOLD}{m}{C.RESET}")
def rule():  print("-" * 60)


def check_environment():
    bold("Step 1 - Checking environment")
    rule()
    all_good = True
    major, minor = sys.version_info.major, sys.version_info.minor
    if major == 3 and minor >= 10:
        ok(f"Python {major}.{minor}")
    else:
        fail(f"Python {major}.{minor} - need 3.10+")
        all_good = False
    for f in REQUIRED_FILES:
        if Path(f).exists():
            ok(f"Found {f}")
        else:
            fail(f"Missing {f}")
            all_good = False
    val = os.environ.get("ANTHROPIC_API_KEY", "")
    if val and val.startswith("sk-ant-") and len(val) > 20 and "test-key" not in val:
        ok("ANTHROPIC_API_KEY is set")
    else:
        fail("ANTHROPIC_API_KEY not set or looks wrong")
        info("Set it with: set ANTHROPIC_API_KEY=your-key-here")
        all_good = False
    try:
        import fastapi, anthropic, pdfplumber, httpx, pydantic, pytest
        ok("All packages installed")
    except ImportError as e:
        fail(f"Missing package: {e.name}")
        info("Run: pip install -r requirements.txt")
        all_good = False
    return all_good


def run_tests():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-v", "--tb=short", "--no-header"],
        capture_output=True, text=True,
    )
    return result.returncode == 0, result.stdout + result.stderr


def parse_results(output):
    summary = {"passed": 0, "failed": 0, "failures": []}
    for line in output.splitlines():
        if " passed" in line:
            try: summary["passed"] = int(line.strip().split()[0])
            except: pass
        if " failed" in line:
            try: summary["failed"] = int(line.strip().split()[0])
            except: pass
        if "FAILED" in line:
            summary["failures"].append(line.strip())
    return summary


def call_claude_repair(test_output, attempt):
    import anthropic as ant
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key or "test-key" in api_key:
        return {"explanation": "Cannot call Claude - API key not set.", "fixes": []}
    claude = ant.Anthropic(api_key=api_key)
    main_py = Path("main.py").read_text(encoding="utf-8")
    test_py = Path("tests/test_api.py").read_text(encoding="utf-8")
    prompt = f"""You are a Python debugging assistant. Fix main.py so all tests pass.
RULES:
- Only fix main.py. Never change test files.
- Return ONLY a valid JSON object, no markdown, no extra text.
- The content field must contain the COMPLETE fixed file.

CURRENT main.py:
```
{main_py}
```

TEST FILE (read only - do not change):
```
{test_py[:2000]}
```

FAILURES (attempt {attempt}/{MAX_REPAIR_ATTEMPTS}):
```
{test_output[-2000:]}
```

Return exactly this JSON:
{{"explanation": "plain English explanation for a non-technical person", "fixes": [{{"file": "main.py", "content": "<complete fixed main.py here>"}}]}}

If you cannot fix it, return fixes as an empty list and explain why in explanation."""
    try:
        msg = claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8096,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = "\n".join(l for l in raw.splitlines() if not l.startswith("```")).strip()
        return json.loads(raw)
    except Exception as e:
        return {"explanation": f"Could not reach Claude: {e}", "fixes": []}


def apply_fixes(repair):
    changed = []
    for fix in repair.get("fixes", []):
        if fix.get("file") == "main.py" and fix.get("content"):
            Path("main.py").write_text(fix["content"], encoding="utf-8")
            changed.append("main.py")
    return changed


class Report:
    def __init__(self):
        self.started = datetime.now()
        self.events = []

    def add(self, kind, msg, detail=""):
        self.events.append({
            "kind": kind, "msg": msg, "detail": detail,
            "time": datetime.now().strftime("%H:%M:%S"),
        })

    def print_final(self, success):
        rule()
        bold("Release Report")
        rule()
        print(f"  Started:  {self.started.strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"  Duration: {(datetime.now() - self.started).seconds}s\n")
        icons = {
            "pass":   f"{C.GREEN}OK{C.RESET}  ",
            "fail":   f"{C.RED}FAIL{C.RESET}",
            "repair": f"{C.YELLOW}FIX{C.RESET} ",
            "info":   f"{C.BLUE}--{C.RESET}  ",
        }
        for e in self.events:
            print(f"  {icons.get(e['kind'], '    ')}  [{e['time']}] {e['msg']}")
            if e["detail"]:
                for line in textwrap.wrap(e["detail"], 54):
                    print(f"               {line}")
        print()
        rule()
        if success:
            print(f"\n  {C.GREEN}{C.BOLD}ALL TESTS PASSED - server is starting.{C.RESET}\n")
        else:
            print(f"\n  {C.RED}{C.BOLD}RELEASE BLOCKED - see report above.{C.RESET}")
            print(f"  The server was NOT started.\n")
        rule()


def start_server():
    bold("Starting server")
    rule()
    info("Server running at http://localhost:8000")
    info("API docs at   http://localhost:8000/docs")
    info("Press Ctrl+C to stop\n")
    os.execv(sys.executable, [
        sys.executable, "-m", "uvicorn", "main:app",
        "--host", "0.0.0.0", "--port", "8000", "--reload",
    ])


def main():
    print("\n" + "=" * 42)
    print("  Summarisation API - Release Agent")
    print("=" * 42 + "\n")
    report = Report()
    if not check_environment():
        report.add("fail", "Environment check failed")
        report.print_final(False)
        sys.exit(1)
    report.add("pass", "Environment check passed")
    bold("\nStep 2 - Running tests")
    rule()
    passed = False
    for attempt in range(1, MAX_REPAIR_ATTEMPTS + 2):
        info(f"Running tests (attempt {attempt})...")
        test_passed, test_output = run_tests()
        summary = parse_results(test_output)
        if test_passed:
            ok(f"All {summary['passed']} tests passed")
            report.add("pass", f"All {summary['passed']} tests passed on attempt {attempt}")
            passed = True
            break
        fail(f"{summary['failed']} test(s) failed, {summary['passed']} passed")
        for f in summary["failures"][:3]:
            print(f"    {C.RED}{f}{C.RESET}")
        report.add("fail", f"Attempt {attempt}: {summary['failed']} failed",
                   ", ".join(summary["failures"][:2]))
        if attempt > MAX_REPAIR_ATTEMPTS:
            break
        bold(f"\nStep 2b - Auto-repair attempt {attempt}/{MAX_REPAIR_ATTEMPTS}")
        rule()
        info("Asking Claude to diagnose and fix...")
        repair = call_claude_repair(test_output, attempt)
        explanation = repair.get("explanation", "No explanation.")
        print(f"\n  Claude says:")
        for line in textwrap.wrap(explanation, 56):
            print(f"    {line}")
        print()
        if not repair.get("fixes"):
            warn("Claude could not produce a fix.")
            report.add("repair", f"Repair {attempt} - no fix", explanation)
            break
        changed = apply_fixes(repair)
        if changed:
            ok(f"Applied fix to: {', '.join(changed)}")
            report.add("repair", f"Repair {attempt} - fixed {', '.join(changed)}", explanation)
        else:
            warn("No files changed.")
            report.add("repair", f"Repair {attempt} - no changes", explanation)
            break
        time.sleep(1)
    report.print_final(passed)
    if passed:
        start_server()
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
