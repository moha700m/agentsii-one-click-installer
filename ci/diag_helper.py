import json, os, subprocess, sys, time, tempfile
from pathlib import Path
exe = Path("dist/agentsii-auth.exe").resolve()
la = Path(os.environ["LOCALAPPDATA"]) / "Agentsii"
la.mkdir(parents=True, exist_ok=True)
(la / "bridge.json").write_text(json.dumps({"port": 17821, "providers": {"diag": {"upstream": "http://127.0.0.1:9/v1"}}}))
import keyring
keyring.set_password("agentsii-codex:diag", "api_key", "sk-diag-123")
def ps():
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq agentsii-auth.exe"], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if "agentsii" in l.lower()]
print("A) stdout to file")
t = time.time()
with open("o.txt", "wb") as f:
    p = subprocess.run([str(exe), "--provider", "diag"], stdout=f, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, timeout=60)
print(" rc", p.returncode, "t=%.1f" % (time.time() - t), "out=", open("o.txt").read(), "err=", p.stderr[:300])
print(" procs:", ps())
time.sleep(3)
print(" procs after 3s:", ps())
print("B) stdout pipe")
t = time.time()
pp = subprocess.Popen([str(exe), "--provider", "diag"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
try:
    o, e = pp.communicate(timeout=40)
    print(" rc", pp.returncode, "t=%.1f" % (time.time() - t), o, e[:300])
except subprocess.TimeoutExpired:
    print(" TIMEOUT; process exited?", pp.poll(), "t=%.1f" % (time.time() - t))
    print(" procs:", ps())
for l in ps():
    pass
subprocess.run(["taskkill", "/F", "/IM", "agentsii-auth.exe"], capture_output=True)
print("C) bridge direct, 5s")
pb = subprocess.Popen([str(exe), "--bridge"])
time.sleep(5)
import urllib.request
try:
    print(" health:", urllib.request.urlopen("http://127.0.0.1:17821/agentsii-bridge/health", timeout=5).read())
except Exception as ex:
    print(" health failed", ex)
print(" poll", pb.poll())
