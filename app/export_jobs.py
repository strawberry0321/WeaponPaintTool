"""Isolated, cancellable export workers with persistent frozen inputs."""
from pathlib import Path
import copy
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid

import studio_core as core
from weapon_export import export_plan, package_name

LOCK = threading.RLock()
JOBS = {}


def staging_path(identity):
    return core.DATA / "tmp" / ("export-" + identity)


def job_state(identity):
    try:
        return json.loads((core.DATA / "jobs" / identity / "status.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def discard_work(state):
    """Drop the staging copy and the empty reserved version directory of an aborted job."""
    work, output = state.get("work"), state.get("output")
    if work:
        shutil.rmtree(Path(work), ignore_errors=True)
    if output:
        for folder in (Path(output), Path(output).parent):
            try:
                folder.rmdir()
            except OSError:
                break


def purge_staging():
    """Remove staging leftovers; a job still marked running may belong to another instance."""
    root = core.DATA / "tmp"
    if not root.is_dir():
        return
    for path in root.glob("export-*"):
        identity = path.name[len("export-"):]
        if identity in JOBS:
            continue
        state = job_state(identity)
        if state.get("state") == "running" and time.time() - state.get("updatedAt", 0) < 3600:
            continue
        shutil.rmtree(path, ignore_errors=True)


def start(data):
    project = copy.deepcopy(data["project"])
    core.validate_project(project)
    plan = export_plan(project, data.get("scope", "current"))
    if not plan["canExport"]:
        raise ValueError("\n".join(row["message"] for row in plan["issues"]) or "请选择导出目标")
    package_name(data.get("packageName"))
    core.autosave(project)
    backup = core.save_project(project, "backups")
    purge_staging()
    with LOCK:
        folder = core.EXPORT_ROOT / core.safe_name(project["name"])
        root = core.reserve_version(folder, prefix="V")
        identity = uuid.uuid4().hex
        job = core.DATA / "jobs" / identity
        job.mkdir(parents=True)
        staging = staging_path(identity)
        staging.mkdir(parents=True)
        # The worker builds every intermediate file in `staging`; only the finished archive reaches `root`.
        request = dict(data, project=project, output=str(staging), destination=str(root),
                       jobId=identity, snapshot=backup["path"])
        core.atomic_write(job / "request.json", core.json_bytes(request))
        state = dict(jobId=identity, state="running", stage="queued", message="工程已备份，正在准备导出", progress=0,
                     startedAt=time.time(), projectRevision=project["revision"], backup=backup, output=str(root),
                     work=str(staging))
        core.atomic_write(job / "status.json", core.json_bytes(state))
        reports = core.DATA / "reports" / identity
        reports.mkdir(parents=True, exist_ok=True)
        log = (reports / "worker.log").open("wb")
        environment = dict(os.environ, HD2_WEAPON_DATA_DIR=str(core.DATA), OPENBLAS_NUM_THREADS="1", GOTO_NUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
        process = subprocess.Popen([sys.executable, "-B", "-X", "utf8", str(core.APP / "export_worker.py"), str(job)],
                                   env=environment, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        JOBS[identity] = (process, log)
        core.atomic_write(core.DATA / "jobs/latest.json", core.json_bytes(dict(jobId=identity)))
    return state


def snapshot(identity=None):
    if identity is None:
        try:
            identity = json.loads((core.DATA / "jobs/latest.json").read_text(encoding="utf-8"))["jobId"]
        except (OSError, ValueError):
            return None
    if not isinstance(identity, str) or len(identity) != 32 or any(character not in "0123456789abcdef" for character in identity):
        raise ValueError("导出作业 ID 无效")
    path = core.DATA / "jobs" / identity / "status.json"
    if not path.is_file():
        raise ValueError("导出作业不存在")
    for attempt in range(5):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            break
        except (PermissionError,FileNotFoundError):
            if attempt==4:
                raise
            time.sleep(.05)
    with LOCK:
        owned = JOBS.get(identity)
        if state["state"] == "running" and (owned is None or owned[0].poll() is not None):
            state.update(state="interrupted", message="导出进程已中断，工程备份已保留，可重新导出", finishedAt=time.time())
            core.atomic_write(path, core.json_bytes(state))
            discard_work(state)
        if owned and owned[0].poll() is not None:
            owned[1].close()
            JOBS.pop(identity, None)
    state["elapsedSeconds"] = (state.get("finishedAt") or time.time()) - state["startedAt"]
    return state


def cancel(identity):
    state = snapshot(identity)
    if state["state"] == "running":
        core.atomic_write(core.DATA / "jobs" / identity / "cancel", b"cancel")
    return state


def shutdown():
    with LOCK:
        current = list(JOBS.items())
    for identity, (process, log) in current:
        if process.poll() is None:
            core.atomic_write(core.DATA / "jobs" / identity / "cancel", b"cancel")
    deadline = time.monotonic() + 4
    for identity, (process, log) in current:
        try:
            process.wait(timeout=max(.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait()
        log.close()
        state = job_state(identity)
        if state.get("state") != "complete":
            discard_work(state)

