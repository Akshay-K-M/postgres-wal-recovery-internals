"""Drive crash recovery under gdb (single-user mode), one event per Next."""
import json
import os
import subprocess
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PG_SRC = os.environ.get("PG_SRC") or os.path.abspath(os.path.join(HERE, "..", "..", ".."))


class RedoUnderGdb:
    def __init__(self, cluster, rels, xids):
        self.cluster = cluster
        self.dir = os.path.join(cluster.lab, "redo-%d" % int(time.time() * 1000))
        os.makedirs(self.dir)
        self.events = os.path.join(self.dir, "events.jsonl")
        self.ctrl = os.path.join(self.dir, "ctrl")
        open(self.events, "w").close()
        os.mkfifo(self.ctrl)
        self.seen = 0
        env = dict(cluster.env,
                   VIZ_EVENTS=self.events, VIZ_CTRL=self.ctrl, VIZ_SRC=PG_SRC,
                   VIZ_RELS=json.dumps(rels), VIZ_XIDS=json.dumps(sorted(xids)),
                   VIZ_PGARGS="--single -D %s postgres" % cluster.data)
        self.log = open(os.path.join(self.dir, "gdb.log"), "w")
        self.proc = subprocess.Popen(
            ["gdb", "-q", "-batch", "-nx", "-x", os.path.join(HERE, "gdb_redo.py"), cluster.bin("postgres")],
            env=env, stdout=self.log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)

    def next_event(self, timeout=120):
        """Wait for the next JSON event written by gdb."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with open(self.events) as f:
                lines = f.read().splitlines()
            if len(lines) > self.seen:
                self.seen += 1
                return json.loads(lines[self.seen - 1])
            if self.proc.poll() is not None:
                with open(self.events) as f:
                    lines = f.read().splitlines()
                if len(lines) > self.seen:
                    continue
                return {"event": "exit", "code": self.proc.returncode, "unexpected": True,
                        "log": self.tail_log()}
            time.sleep(0.05)
        raise RuntimeError("gdb did not report back within %ds:\n%s" % (timeout, self.tail_log()))

    def resume(self):
        """Let the paused postgres continue to the next stop."""
        deadline = time.time() + 30
        while True:
            try:
                fd = os.open(self.ctrl, os.O_WRONLY | os.O_NONBLOCK)
                break
            except OSError:            # gdb not reading yet
                if self.proc.poll() is not None or time.time() > deadline:
                    return
                time.sleep(0.05)
        try:
            os.write(fd, b"next\n")
        finally:
            os.close(fd)

    def tail_log(self, n=15):
        try:
            with open(os.path.join(self.dir, "gdb.log")) as f:
                return "\n".join(f.read().splitlines()[-n:])
        except OSError:
            return ""

    def kill(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        # a killed single-user backend may leave its pid file behind
        pid = os.path.join(self.cluster.data, "postmaster.pid")
        if os.path.exists(pid):
            os.remove(pid)
