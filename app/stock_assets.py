"""Build the editor asset pack locally from the game's installed resources."""

import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import threading
import zipfile

from compute_runtime import np
import native_writer
import studio_core as core
from weapon_assets import DISPLAY_BASIS, bones, read_group, read_group_normals


SETUP_LOCK = threading.Lock()


def write_previews(root):
    preview = root / "preview"
    with zipfile.ZipFile(core.APP / "assets/stock_previews.zip") as archive:
        manifest = json.loads(archive.read("materials.json"))
        needed = {name for value in manifest.values() for name in value["files"].values()}
        if set(archive.namelist()) != needed | {"materials.json"}:
            raise ValueError("原装预览材质清单不完整")
        for name in sorted(needed):
            core.atomic_write(core.inside(preview, name), archive.read(name))
        core.atomic_write(preview / "materials.json", archive.read("materials.json"))
    return needed | {"materials.json"}


def restore_existing_previews():
    if not core.PACK_HASH:
        return
    index = core.BUILTIN / "preview/materials.json"
    entries = json.loads(index.read_bytes()) if index.is_file() else {}
    if entries and all((index.parent / name).is_file() for value in entries.values() for name in value["files"].values()):
        return
    names = write_previews(core.BUILTIN)
    path = core.BUILTIN / "asset_manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["files"].update({f"preview/{name}": core.file_hash(index.parent / name) for name in names})
    manifest["validation"] = "本机资源校验与原装材质预览"
    core.atomic_write(path, core.json_bytes(manifest))
    core.load_builtin_pack()


def fit_local_sight(stage, model):
    """Translate the stock front and rear sight feet to the recorded mount positions."""
    source_id = model["sourceUnitId"][2:]
    folder = stage / "native/unit"
    source = folder / (source_id + ".unit.main")
    raw = source.read_bytes()
    original = source.with_suffix(".gpu").read_bytes()
    gpu = bytearray(original)
    unit = native_writer.parse(raw, original)
    transforms = bones(raw, {})
    canonical = json.loads((stage / "models" / (source_id + ".json")).read_bytes())
    coverage = {(record["mesh"], record["group"]): record["roles"] for record in canonical["coverage"]}
    moved = {}
    for mesh in unit["meshes"]:
        if mesh["layout"] < 0:
            continue
        layout = unit["layouts"][mesh["layout"]]
        position = next(item for item in layout["items"] if item["type"] == 0)
        bone = struct.unpack_from("<I", mesh["raw"], 48)[0]
        basis = transforms[bone]["matrix"][:3, :3] if bone < len(transforms) else np.eye(3)
        for group_id, group in enumerate(mesh["groups"]):
            roles = coverage.get((mesh["index"], group_id))
            if roles is None:
                continue
            index_stride = 2 if layout["indexType"] == 0 else 4
            indices = np.frombuffer(original, dtype="<u2" if index_stride == 2 else "<u4", count=group[4],
                                    offset=layout["iOff"] + group[3] * index_stride).reshape(-1, 3)
            for role, amount in model["mountCorrection"].items():
                selected = np.asarray(roles) == role
                delta = np.linalg.solve(DISPLAY_BASIS @ basis, [amount, 0, 0])
                for vertex in np.unique(indices[selected]):
                    offset = layout["vOff"] + (group[1] + int(vertex)) * layout["stride"] + position["offset"]
                    if offset in moved:
                        if not np.allclose(moved[offset], delta):
                            raise ValueError("本机瞄具的安装面不匹配")
                        continue
                    value = np.asarray(struct.unpack_from("<3f", original, offset))
                    struct.pack_into("<3f", gpu, offset, *(value + delta))
                    moved[offset] = delta
    target = folder / (model["unitId"][2:] + ".unit.main")
    target.write_bytes(raw)
    target.with_suffix(".gpu").write_bytes(gpu)
    if core.file_hash(target) != model["sourceHashes"]["main"] or core.file_hash(target.with_suffix(".gpu")) != model["sourceHashes"]["gpu"]:
        raise ValueError("本机生成的瞄具安装位置与支持版本不一致")


def rebuild_model(stage, descriptor_path, recipe):
    description = json.loads(descriptor_path.read_bytes())
    unit_id = description["unitId"][2:]
    path = stage / "native/unit" / (unit_id + ".unit.main")
    if description.get("sourceUnitId") and description.get("mountCorrection"):
        fit_local_sight(stage, description)
    raw = path.read_bytes()
    gpu = path.with_suffix(".gpu")
    unit = native_writer.parse(raw, gpu.read_bytes() if gpu.is_file() else b"")
    transforms = bones(raw, {})
    groups = {}
    arrays = {}
    preview = bytearray(max((part["offset"] + part["count"] * 32 for part in description["parts"]), default=0))
    with np.load(io.BytesIO(recipe.read("indices/" + unit_id + ".npz")), allow_pickle=False) as mapping:
        for part in description["parts"]:
            key = part["componentId"]
            refs = mapping[key + "_refs"]
            representative = mapping[key + "_representative"]
            positions = np.empty((len(refs), 3, 3), dtype=np.float32)
            normals = np.empty_like(positions)
            uv = np.empty((len(refs), 3, 2), dtype=np.float32)
            for mesh, group in np.unique(refs[:, :2], axis=0):
                identity = int(mesh), int(group)
                if identity not in groups:
                    data = unit["meshes"][mesh]
                    vertices, coordinates, _ = read_group(unit, data, group, transforms, skin_names=False)
                    groups[identity] = vertices, coordinates, read_group_normals(unit, data, group, transforms)
                vertices, coordinates, directions = groups[identity]
                using = (refs[:, 0] == mesh) & (refs[:, 1] == group)
                triangles = refs[using, 2]
                positions[using] = vertices[triangles]
                normals[using] = directions[triangles]
                uv[using] = coordinates[triangles]
            arrays.update({
                key + "_positions": positions,
                key + "_uvs": uv,
                key + "_refs": refs,
                key + "_representative": representative,
            })
            render_vertices = np.concatenate((positions[representative], normals[representative], uv[representative]), axis=2)
            data = render_vertices.astype("<f4", copy=False).tobytes()
            if len(data) != part["count"] * 32:
                raise ValueError("本机资源与支持版本的模型索引不一致")
            start = part["offset"]
            preview[start:start + len(data)] = data
    descriptor_path.with_suffix(".bin").write_bytes(preview)
    np.savez_compressed(descriptor_path.with_suffix(".npz"), **arrays)


def prepare(game_directory):
    if not isinstance(game_directory, str) or not game_directory.strip():
        raise ValueError("请选择 Helldivers 2 安装目录")
    game = Path(game_directory).resolve()
    if not (game / "data").is_dir():
        raise ValueError("请选择包含 data 文件夹的 Helldivers 2 安装目录")
    with SETUP_LOCK:
        if core.PACK_HASH:
            return dict(ready=True)
        destination = core.BUILTIN
        if destination.exists():
            raise ValueError("当前资源目录不完整，请移出残留目录后重试")
        temp = (core.DATA / "tmp").resolve()
        temp.mkdir(parents=True, exist_ok=True)
        folder = Path(tempfile.mkdtemp(dir=temp, prefix="stock-"))
        stage = folder / "pack"
        stage.mkdir()
        try:
            with zipfile.ZipFile(core.APP / "assets/stock_recipe.zip") as recipe:
                for item in recipe.infolist():
                    if item.filename.startswith("indices/"):
                        continue
                    target = core.inside(stage, item.filename)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(recipe.read(item))
                executable = core.APP / "engine/stock_extract.exe"
                result = subprocess.run([str(executable), str(game), str(stage / "native_sources.json"), str(stage / "native")],
                                        cwd=folder, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=300,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if result.returncode:
                    missing = sum(line.startswith(b"missing compatible") for line in result.stderr.splitlines())
                    if missing:
                        raise ValueError(f"本机游戏资源与测试版不匹配，缺少 {missing} 项兼容资源")
                    raise ValueError("无法读取本机游戏资源")
                for descriptor in sorted((stage / "models").glob("*.json")):
                    rebuild_model(stage, descriptor, recipe)
            info = json.loads((stage / "asset_info.json").read_bytes())
            (stage / "asset_info.json").unlink()
            (stage / "native_sources.json").unlink()
            write_previews(stage)
            files = {p.relative_to(stage).as_posix(): core.file_hash(p) for p in stage.rglob("*") if p.is_file()}
            info.update(catalogHash=files["catalog.json"], files=files, units=len(list((stage / "models").glob("*.json"))),
                        validation="本机资源校验与原装材质预览")
            core.atomic_write(stage / "asset_manifest.json", core.json_bytes(info))
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage.rename(destination)
            core.load_builtin_pack()
            return dict(ready=True, units=info["units"])
        finally:
            if folder.parent == temp and folder.resolve().is_relative_to(temp):
                shutil.rmtree(folder, ignore_errors=True)
