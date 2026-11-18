"""HTTP API. One scenario run at a time; the whole app is meant for one presenter."""
import atexit
import json
import os
import threading
import traceback

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_POST

from .engine import runner, scenario

LOCK = threading.Lock()
STATE = {"run": None, "cursor": 0}


def _close():
    if STATE["run"]:
        STATE["run"].close()
        STATE["run"] = None


atexit.register(_close)


def _payload():
    run, i = STATE["run"], STATE["cursor"]
    return {
        "view": run.history[i], "index": i, "count": len(run.history),
        "can_next": i < len(run.history) - 1 or run.has_next(),
        "script": run.text, "lines": [s["line"] for s in run.steps],
    }


def _error(msg, status=400):
    return JsonResponse({"error": msg}, status=status)


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "viz")


def _asset_version():
    """Changes whenever app.js or app.css is edited, so browsers never run a stale copy."""
    return max(int(os.path.getmtime(os.path.join(STATIC_DIR, f))) for f in ("app.js", "app.css"))


@ensure_csrf_cookie
def index(request):
    return render(request, "viz/index.html", {"example": scenario.EXAMPLE, "v": _asset_version()})


@require_POST
def start(request):
    text = json.loads(request.body or "{}").get("script", "")
    try:
        scenario.parse(text)
    except scenario.ScenarioError as e:
        return _error(str(e))
    with LOCK:
        _close()
        run = runner.Run(text)
        try:
            run.start()
        except Exception as e:
            run.close()
            return _error("Could not start PostgreSQL: %s" % e, 500)
        STATE.update(run=run, cursor=0)
        return JsonResponse(_payload())


@require_POST
def step(request):
    direction = json.loads(request.body or "{}").get("dir", "next")
    with LOCK:
        run = STATE["run"]
        if run is None:
            return _error("No scenario is running.")
        if direction == "prev":
            STATE["cursor"] = max(0, STATE["cursor"] - 1)
        elif direction == "next":
            if STATE["cursor"] < len(run.history) - 1:
                STATE["cursor"] += 1
            elif run.has_next():
                try:
                    run.next()
                except Exception as e:
                    traceback.print_exc()
                    return _error("This step failed: %s" % e, 500)
                STATE["cursor"] = len(run.history) - 1
        return JsonResponse(_payload())


@require_POST
def stop(request):
    with LOCK:
        _close()
    return JsonResponse({"ok": True})
