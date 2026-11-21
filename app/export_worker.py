"""Entry point for one immutable export job."""
from pathlib import Path
import json
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).resolve().parent))
import studio_core as core
import weapon_export


def main():
    folder = Path(sys.argv[1]).resolve()
    request = json.loads((folder / "request.json").read_text(encoding="utf-8"))
    state = json.loads((folder / "status.json").read_text(encoding="utf-8"))

    def cancelled():
        if (folder / "cancel").exists():
            raise InterruptedError("导出已取消，工程备份和有效缓存已保留")

    def progress(stage, message, fraction):
        state.update(stage=stage, message=message, updatedAt=time.time())
        if fraction is not None:
            state["progress"] = fraction
        core.atomic_write(folder / "status.json", core.json_bytes(state))

    try:
        cancelled()
        result = weapon_export.execute(request, Path(request["output"]), progress, cancelled)
        state.update(state="complete", stage="complete", message="MOD 安装包已生成并通过独立回读", progress=1, result=result)
    except InterruptedError as error:
        state.update(state="cancelled", message=str(error))
    except Exception as error:
        traceback.print_exc()
        state.update(state="failed", message=str(error), errorType=type(error).__name__)
    finally:
        state["finishedAt"] = time.time()
        core.atomic_write(folder / "status.json", core.json_bytes(state))


if __name__ == "__main__":
    main()
