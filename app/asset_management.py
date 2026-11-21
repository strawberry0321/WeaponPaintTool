"""Import immutable asset versions and audit mappings before registration."""
from pathlib import Path
import base64
import hashlib
import io
import json
import re
import shutil
import uuid
import zipfile

import studio_core as core
from compute_runtime import np
from weapon_assets import validate_quads


def list_packs():
    results = [dict(id="builtin/1", manifest=json.loads((core.BUILTIN / "asset_manifest.json").read_text(encoding="utf-8")))] if core.PACK_HASH else []
    for path in (core.DATA / "assets/installed").glob("*/*/asset_manifest.json"):
        manifest = json.loads(path.read_text(encoding="utf-8"))
        results.append(dict(id=path.parent.relative_to(core.DATA / "assets").as_posix(), manifest=manifest))
    return results


def load_installed():
    for path in (core.DATA / "assets/installed").glob("*/*/catalog.json"):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        for weapon in catalog["weapons"]:
            core.WEAPONS.setdefault(weapon["weaponId"], weapon)


def import_pack(data):
    raw = base64.b64decode(data["data"].split(",")[-1], validate=True)
    source_hash = hashlib.sha256(raw).hexdigest()
    protected_source = core.DATA / "assets/source_packages" / (source_hash + ".hd2assets")
    if not protected_source.exists():
        core.atomic_write(protected_source, raw)
    stage = core.DATA / "tmp/asset-import" / uuid.uuid4().hex
    stage.mkdir(parents=True)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or sum(item.file_size for item in archive.infolist()) > 3 * 1024 ** 3:
                raise ValueError("资产包文件数或解压大小无效")
            manifest = json.loads(archive.read("asset_manifest.json"))
            if manifest.get("schemaVersion") != 1:
                raise ValueError("资产包格式版本需要适配")
            pack_id, version = str(manifest["packId"]), str(manifest["version"])
            if not all(re.fullmatch(r"[A-Za-z0-9_.-]{1,70}", value) and value not in (".", "..") for value in (pack_id, version)):
                raise ValueError("资产包名称或版本无效")
            inventory = manifest.get("files")
            if not isinstance(inventory, dict) or set(names) != set(inventory) | {"asset_manifest.json"}:
                raise ValueError("资产包需要完整的逐文件 SHA-256 清单")
            for name, expected in inventory.items():
                target = core.inside(stage, name)
                raw_file = archive.read(name)
                if hashlib.sha256(raw_file).hexdigest() != expected:
                    raise ValueError("资产校验失败 " + name)
                core.atomic_write(target, raw_file)
            core.atomic_write(stage / "asset_manifest.json", core.json_bytes(manifest))
        catalog = json.loads((stage / "catalog.json").read_text(encoding="utf-8"))
        if core.file_hash(stage / "catalog.json") != manifest["catalogHash"]:
            raise ValueError("目录哈希不一致")
        count = 0
        for weapon in catalog["weapons"]:
            if weapon.get("status") != "ready":
                continue
            model_path = core.inside(stage / "models", weapon["unitId"][2:] + ".json")
            descriptor = json.loads(model_path.read_text(encoding="utf-8"))
            blob = model_path.with_suffix(".bin").read_bytes()
            for part in descriptor["parts"]:
                start, length = part["offset"], part["count"] * 32
                if start < 0 or start + length > len(blob) or part["count"] % 3:
                    raise ValueError("模型顶点缓冲或三角形声明无效")
                points = np.frombuffer(blob, dtype="<f4", count=part["count"] * 8, offset=start).reshape(-1, 8)[:, :3].reshape(-1, 3, 3)
                if not np.all(np.isfinite(points)):
                    raise ValueError("模型坐标包含无效值")
                validate_quads(points, part["quads"])
                count += 1
            native_path = stage / "native/unit" / (weapon["unitId"][2:] + ".unit.main")
            if not native_path.is_file() or core.file_hash(native_path) != descriptor["sourceHashes"]["main"]:
                raise ValueError("原生 Unit 来源缺失或哈希错误")
        destination = core.DATA / "assets/installed" / pack_id / version
        if destination.exists():
            raise ValueError("此资产版本已经存在，请递增版本号")
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage.rename(destination)
        core.atomic_write(core.DATA / "assets/import_reports" / (source_hash + ".json"), core.json_bytes(dict(
            sourceCopy=str(protected_source), sourceSHA256=source_hash,
            destination=str(destination), checkedFiles=len(inventory), auditedComponents=count)))
        # Recoverable, content-addressed versions are used by the project lock.
        recovered = core.DATA / "assets/recovered" / manifest["catalogHash"]
        if not recovered.exists():
            shutil.copytree(destination, recovered)
        load_installed()
        return dict(id=destination.relative_to(core.DATA / "assets").as_posix(), packId=pack_id,
                    version=version, auditedComponents=count, weapons=len(catalog["weapons"]),
                    sourceSHA256=source_hash)
    finally:
        if stage.exists():
            # stage is a UUID child created by this import in the data directory.
            if not stage.resolve().is_relative_to((core.DATA / "tmp/asset-import").resolve()):
                raise ValueError("资产导入临时路径无效")
            shutil.rmtree(stage)
