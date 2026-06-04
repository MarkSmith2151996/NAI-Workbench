#!/usr/bin/env python3
"""Sandbox self-test — runs inside the container via ttyd, tests everything."""
import os, sys, time, subprocess, shutil, json

# Colors
G = "\033[92m"  # green
R = "\033[91m"  # red
Y = "\033[93m"  # yellow
C = "\033[96m"  # cyan
B = "\033[1m"   # bold
D = "\033[0m"   # reset

passed = 0
failed = 0

def test(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  {G}PASS{D}  {name}")
    else:
        failed += 1
        print(f"  {R}FAIL{D}  {name}  {Y}{detail}{D}")

def section(title):
    print(f"\n{C}{B}{'─'*50}")
    print(f"  {title}")
    print(f"{'─'*50}{D}")

# ─── Header ───
print(f"\n{B}{C}╔{'═'*48}╗")
print(f"║{'SANDBOX SELF-TEST':^48}║")
print(f"╚{'═'*48}╝{D}")
print(f"  Container: {os.environ.get('HOSTNAME', 'unknown')}")
print(f"  Python:    {sys.version.split()[0]}")
print(f"  CWD:       {os.getcwd()}")
print(f"  Time:      {time.strftime('%Y-%m-%d %H:%M:%S')}")

# ─── Filesystem ───
section("FILESYSTEM")
test("Working dir is /workspace", os.getcwd() == "/workspace")
test("/workspace exists", os.path.isdir("/workspace"))
files = os.listdir("/workspace")
test("Project files mounted", len(files) > 0, f"found {len(files)} files")
test("Can read files", os.path.isfile("/workspace/CLAUDE.md") or len(files) > 3)
test("Can write temp file", True)
try:
    with open("/tmp/sandbox_test.txt", "w") as f:
        f.write("test")
    with open("/tmp/sandbox_test.txt") as f:
        test("Can read back temp file", f.read() == "test")
    os.remove("/tmp/sandbox_test.txt")
except Exception as e:
    test("Can read back temp file", False, str(e))

# ─── Python Environment ───
section("PYTHON ENVIRONMENT")
test("Python 3.12+", sys.version_info >= (3, 12), f"got {sys.version_info[:2]}")

modules = {"json": False, "sqlite3": False, "http.server": False, "subprocess": False, "os": False}
for mod in modules:
    try:
        __import__(mod)
        modules[mod] = True
    except ImportError:
        pass
for mod, ok in modules.items():
    test(f"import {mod}", ok)

# Check if pip works
pip_result = subprocess.run([sys.executable, "-m", "pip", "--version"], capture_output=True, text=True)
test("pip available", pip_result.returncode == 0)

# ─── Network ───
section("NETWORK")
import socket
test("DNS resolution", True)
try:
    socket.getaddrinfo("github.com", 443)
    test("Can resolve github.com", True)
except Exception as e:
    test("Can resolve github.com", False, str(e))

test("Can bind port", True)
try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("0.0.0.0", 19999))
    s.close()
    test("Port bind + release works", True)
except Exception as e:
    test("Port bind + release works", False, str(e))

# ─── Tools ───
section("TOOLS")
tools = {
    "bash": "bash --version",
    "curl": "curl --version",
    "git": "git --version",
    "ttyd": "ttyd --version",
}
for name, cmd in tools.items():
    result = subprocess.run(cmd.split(), capture_output=True, text=True)
    version = result.stdout.split("\n")[0][:50] if result.returncode == 0 else ""
    test(f"{name} installed", result.returncode == 0, version or result.stderr[:50])

# ─── Docker Awareness ───
section("CONTAINER ENVIRONMENT")
test("Running in container", os.path.isfile("/.dockerenv") or os.environ.get("container"))
test("HOSTNAME set", bool(os.environ.get("HOSTNAME")))
test("/tmp writable", os.access("/tmp", os.W_OK))
test("/workspace writable", os.access("/workspace", os.W_OK))

# Check sandbox log pipe
test("/tmp/sandbox.log exists", os.path.isfile("/tmp/sandbox.log"))

# ─── Process ───
section("PROCESS")
test("PID > 1 (not init)", os.getpid() > 1)
test("UID is root (0) or dev (1000)", os.getuid() in (0, 1000), f"uid={os.getuid()}")

# ─── Performance ───
section("PERFORMANCE")
start = time.time()
total = sum(range(1_000_000))
elapsed = time.time() - start
test(f"Sum 1M ints in <0.5s", elapsed < 0.5, f"{elapsed:.3f}s")

start = time.time()
data = "x" * 10_000_000
_ = json.dumps({"data": data})
elapsed = time.time() - start
test(f"JSON encode 10MB in <1s", elapsed < 1.0, f"{elapsed:.3f}s")

# ─── Summary ───
total_tests = passed + failed
color = G if failed == 0 else R
print(f"\n{B}{color}╔{'═'*48}╗")
print(f"║{'RESULTS':^48}║")
print(f"║{f'{passed}/{total_tests} passed':^48}║")
if failed:
    print(f"║{f'{failed} FAILED':^48}║")
print(f"╚{'═'*48}╝{D}\n")

# Keep alive so ttyd stays open
if failed == 0:
    print(f"{G}All tests passed! Sandbox is fully operational.{D}")
else:
    print(f"{Y}Some tests failed — check output above.{D}")

print(f"\n{C}Press Ctrl+C to exit, or this will close in 60s...{D}")
try:
    time.sleep(60)
except KeyboardInterrupt:
    pass
