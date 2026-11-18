"""Parse the scenario the user types.

    T1: BEGIN;
    T1: INSERT INTO t VALUES (1, 'a');      -- runs in session T1
    CHECKPOINT;                             -- no label: one-off session
    crash                                   -- immediate shutdown

* A statement ends with ';' and may span several lines (only the first line
  carries the label).
* `crash` (any case, optional ';') crashes the server.
* A line starting with '--' is a caption shown with the next step.
"""
import re

LABEL_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_]{0,15})\s*:\s*(.*)$")
CRASH_RE = re.compile(r"^\s*crash\s*;?\s*$", re.I)


class ScenarioError(ValueError):
    pass


def _ends_statement(sql):
    """True when sql ends with ';' outside quotes and comments."""
    in_s = in_d = False
    i, last = 0, ""
    while i < len(sql):
        c = sql[i]
        if in_s:
            if c == "'":
                in_s = False
        elif in_d:
            if c == '"':
                in_d = False
        elif c == "'":
            in_s = True
        elif c == '"':
            in_d = True
        elif sql.startswith("--", i):
            nl = sql.find("\n", i)
            if nl < 0:
                break
            i = nl
            continue
        if not c.isspace():
            last = c
        i += 1
    return last == ";" and not in_s and not in_d


def parse(text):
    steps, caption, cur = [], [], None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        if cur is not None:                       # continuing a multi-line statement
            cur["sql"] += "\n" + line
            if _ends_statement(cur["sql"]):
                steps.append(cur)
                cur = None
            continue
        if not line.strip():
            continue
        if line.strip().startswith("--"):
            caption.append(line.strip()[2:].strip())
            continue
        if CRASH_RE.match(line):
            steps.append({"kind": "crash", "label": None, "sql": "crash", "line": lineno,
                          "caption": " ".join(caption)})
            caption = []
            continue
        m = LABEL_RE.match(line)
        label, sql = (m.group(1), m.group(2)) if m else (None, line.strip())
        if label and label.lower() == "crash":
            raise ScenarioError("line %d: 'crash' cannot be used as a session name" % lineno)
        if not sql.strip():
            raise ScenarioError("line %d: no SQL after '%s:'" % (lineno, label))
        cur = {"kind": "sql", "label": label, "sql": sql, "line": lineno, "caption": " ".join(caption)}
        caption = []
        if _ends_statement(sql):
            steps.append(cur)
            cur = None
    if cur is not None:
        raise ScenarioError("line %d: statement does not end with ';'" % cur["line"])
    if not steps:
        raise ScenarioError("the scenario is empty")
    return steps


EXAMPLE = """\
-- One table, five transactions, a checkpoint, a crash, then VACUUM.
CREATE TABLE accounts (id int, owner text);
CHECKPOINT;

-- T1 commits before the checkpoint
T1: BEGIN;
T1: INSERT INTO accounts VALUES (1, 'alice');
T1: COMMIT;

-- T2 stays open across the checkpoint and the crash
T2: BEGIN;
T2: INSERT INTO accounts VALUES (2, 'bob');

-- T3 rolls back on purpose
T3: BEGIN;
T3: INSERT INTO accounts VALUES (3, 'carol');
T3: ROLLBACK;

-- T4 fails with an error, which aborts it
T4: BEGIN;
T4: INSERT INTO accounts VALUES (4, 'dave');
T4: SELECT 1/0;
T4: ROLLBACK;

-- The checkpoint writes the page to disk, committed or not
CHECKPOINT;

T2: INSERT INTO accounts VALUES (5, 'erin');

-- T5 commits after the checkpoint (this also flushes T2's WAL)
T5: BEGIN;
T5: INSERT INTO accounts VALUES (6, 'frank');
T5: COMMIT;

crash

-- After recovery only committed rows are visible
SELECT * FROM accounts;

-- VACUUM removes rows of aborted / never-committed transactions
VACUUM accounts;
CHECKPOINT;
"""
