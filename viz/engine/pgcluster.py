"""A throw-away PostgreSQL cluster plus long-lived psql sessions."""
import json
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time


def find_bindir():
    cands = [os.environ.get("PGBIN"), os.path.expanduser("~/pgsql/bin"), "/usr/local/pgsql/bin"]
    for c in cands:
        if c and os.path.exists(os.path.join(c, "postgres")):
            return c
    raise RuntimeError("PostgreSQL binaries not found. Set PGBIN or install into ~/pgsql "
                       "(see recovery_app/setup.sh).")


# Settings that make the demo easy to follow. Every one is explained in the UI.
SERVER_CONF = """
# --- recovery visualizer ---
listen_addresses = ''
checkpoint_timeout = 1h          # no surprise checkpoints
synchronous_commit = on          # COMMIT waits for its WAL flush
autovacuum = off                 # only an explicit VACUUM removes rows
bgwriter_lru_maxpages = 0        # pages reach the table file only via checkpoints
shared_buffers = 4MB             # small, so gdb can scan the buffer pool quickly
max_connections = 30
log_checkpoints = on
"""


class Cluster:
    def __init__(self):
        self.bindir = find_bindir()
        self.lab = tempfile.mkdtemp(prefix="pg-recovery-viz.")
        self.data = os.path.join(self.lab, "data")
        self.sock = self.lab
        self.port = 55400 + os.getpid() % 500
        self.log = os.path.join(self.lab, "server.log")
        self.env = dict(os.environ, PGHOST=self.sock, PGPORT=str(self.port), PGUSER="postgres",
                        PGDATABASE="postgres", LC_ALL="C", PGAPPNAME="viz-inspector")

    def bin(self, name):
        return os.path.join(self.bindir, name)

    # ---------------------------------------------------------------- lifecycle
    def init(self):
        subprocess.run([self.bin("initdb"), "-A", "trust", "-U", "postgres", "-D", self.data,
                        "--no-data-checksums"], env=self.env, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        with open(os.path.join(self.data, "postgresql.conf"), "a") as f:
            f.write(SERVER_CONF)
            f.write("unix_socket_directories = '%s'\nport = %d\n" % (self.sock, self.port))

    def start(self):
        subprocess.run([self.bin("pg_ctl"), "-D", self.data, "-l", self.log, "-w", "start"],
                       env=self.env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def crash(self):
        subprocess.run([self.bin("pg_ctl"), "-D", self.data, "-m", "immediate", "stop"],
                       env=self.env, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def stop(self):
        subprocess.run([self.bin("pg_ctl"), "-D", self.data, "-m", "fast", "stop"],
                       env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def destroy(self):
        if os.path.exists(os.path.join(self.data, "postmaster.pid")):
            self.stop()
        shutil.rmtree(self.lab, ignore_errors=True)

    # ---------------------------------------------------------------- queries
    def query(self, sql, variables=None):
        """Run SQL in a fresh connection, return stdout (unaligned, tuples only)."""
        args = [self.bin("psql"), "-X", "-A", "-t", "-q", "-v", "ON_ERROR_STOP=1"]
        for k, v in (variables or {}).items():
            args += ["-v", "%s=%s" % (k, v)]
        p = subprocess.run(args, input=sql, env=self.env, capture_output=True, text=True)
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or "psql failed")
        return p.stdout.strip()

    def query_json(self, sql, variables=None):
        out = self.query(sql, variables)
        return json.loads(out) if out else None

    def controldata(self):
        p = subprocess.run([self.bin("pg_controldata"), "-D", self.data], env=self.env,
                           capture_output=True, text=True)
        info = {}
        for line in p.stdout.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                info[k.strip()] = v.strip()
        return info


class Session:
    """One psql process = one connection, labelled via application_name."""

    WAIT = 3.0      # seconds to wait for a statement before calling it "blocked"

    def __init__(self, cluster, label):
        self.label = label
        self.dead = False
        env = dict(cluster.env, PGAPPNAME="viz:" + label)
        cmd = [cluster.bin("psql"), "-X", "-P", "pager=off", "-v", "ON_ERROR_STOP=0"]
        if shutil.which("stdbuf"):
            cmd = ["stdbuf", "-oL", "-eL"] + cmd       # line-buffer psql's output
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, bufsize=1, env=env)
        self.lines = queue.Queue()
        self.n = 0
        self.waiting_marker = None
        self.late_output = []
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stdout:
            self.lines.put(line.rstrip("\n"))
        self.lines.put(None)

    def _collect(self, marker, timeout):
        out, deadline = [], time.time() + timeout
        while True:
            try:
                line = self.lines.get(timeout=max(0.01, deadline - time.time()))
            except queue.Empty:
                return out, False
            if line is None:
                self.dead = True
                return out, True
            if line == marker:
                return out, True
            out.append(line)

    def drain_late(self):
        """Output of an earlier statement that was blocked and has now finished."""
        if not self.waiting_marker:
            return None
        out, done = self._collect(self.waiting_marker, 0.2)
        self.late_output += out
        if done:
            self.waiting_marker = None
            res, self.late_output = self.late_output, []
            return res
        return None

    def run(self, sql):
        """Returns (output_lines, finished)."""
        if self.waiting_marker:
            return ["(still waiting for the previous statement in %s)" % self.label], False
        self.n += 1
        marker = "__viz_done_%d__" % self.n
        try:
            self.proc.stdin.write(sql.rstrip() + "\n\\echo " + marker + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError):
            self.dead = True
            return ["(connection is gone)"], True
        out, done = self._collect(marker, self.WAIT)
        if not done:
            self.waiting_marker = marker
            self.late_output = out
        return out, done

    def close(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def kill(self):
        self.dead = True
        try:
            self.proc.kill()
        except OSError:
            pass
