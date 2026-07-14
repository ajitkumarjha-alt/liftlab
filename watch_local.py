#!/usr/bin/env python3
"""Local operator harness for SOAK #1 — run the watch scheduler WITHOUT the cloud
enqueue/dashboard (deliberately NOT built until the loop is proven; avoids the
B1->B2->B3 ordering trap). Invokes watch_manager.run_watch directly with config
read from /etc/liftlab-agent.env. Runs under the AGENT (B3) python — watch_manager
is stdlib-only; it launches the CV child under B4 per watch_runtime.conf.

  <b3py> watch_local.py start 29     # FOREGROUND — hold under nohup/tmux. Its reader
                                     #   thread posts cycles to /api/gw/events (the
                                     #   existing analyze_local path). Token lives in
                                     #   THIS parent; stripped from the child.
  <b3py> watch_local.py confirm 29   # baseline eyeballed doors-shut -> SIGUSR1 child
  <b3py> watch_local.py stop 29      # SIGTERM child (graceful)
  <b3py> watch_local.py status 29    # print the child's latest status

confirm/stop/status signal/read the child by PID via the state file, so they work
from a separate short invocation while `start` holds the parent alive.
"""
import sys
import time

sys.path.insert(0, "/home/askjitk/liftlab-b3/pi-agent")
import watch_manager as wm  # noqa: E402  (stdlib-only)

ENV = "/etc/liftlab-agent.env"


def load_env(p):
    c = {}
    try:
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                c[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return c


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "status"
    channel = int(sys.argv[2]) if len(sys.argv) > 2 else 29
    c = load_env(ENV)
    token = c.get("GATEWAY_TOKEN", "")
    kw = dict(
        log=lambda m: print(m, flush=True),
        cloud=c.get("CLOUD_URL", ""), gw_id=c.get("GATEWAY_ID", "site-A"),
        headers=({"Authorization": f"Bearer {token}"} if token else {}),
        zones_path=c.get("ZONES_PATH", "/home/askjitk/liftlab-b4/camera_zones.json"),
        nvr=(c.get("NVR_HOST", ""), c.get("NVR_PORT", "80"), c.get("NVR_USER", ""), c.get("NVR_PASS", "")),
    )
    job = {"type": "watch_channel", "params": {"channel": channel, "action": action}}
    res = wm.run_watch(job, **kw)
    print(f"[watch_local] {action} ch{channel} -> {res}", flush=True)

    if action == "start" and res.get("status") == "starting":
        print(f"[watch_local] START ok (pid {res.get('pid')}). Holding parent alive so the reader "
              f"posts cycles to /api/gw/events. Run `watch_local.py stop {channel}` in another "
              f"shell, or Ctrl-C, to end.", flush=True)
        try:
            while wm._running(channel):
                time.sleep(5)
            print("[watch_local] child exited; parent done.", flush=True)
        except KeyboardInterrupt:
            print("[watch_local] Ctrl-C -> stopping child", flush=True)
            wm.run_watch({"type": "watch_channel", "params": {"channel": channel, "action": "stop"}}, **kw)


if __name__ == "__main__":
    main()
