"""Versioned projects, asset locks and per-host assembly instances."""
from pathlib import Path
from collections import defaultdict
from functools import lru_cache
import base64
import copy
import hashlib
import io
import json
import math
import os
import re
import threading
import time
import uuid
import zipfile

from PIL import Image, ImageDraw, ImageFont
from compute_runtime import np
from weapon_assets import explosion_layout

APP = Path(__file__).resolve().parent
INSTALL = APP.parent
VERSION = "1.7.3-beta.1"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)
FORMAT = "HD2WeaponPaintProject"
SCHEMA_VERSION = 1
LOCK = threading.RLock()


def json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def atomic_write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending = path.with_name(path.name + "." + uuid.uuid4().hex + ".pending")
    try:
        with pending.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        pending.unlink(missing_ok=True)


def data_root():
    value = os.environ.get("HD2_WEAPON_DATA_DIR")
    settings = INSTALL / "settings.json"
    if not value and settings.exists():
        value = json.loads(settings.read_text(encoding="utf-8-sig")).get("dataDirectory")
    path = Path(value or "data")
    path = (INSTALL / path).resolve() if not path.is_absolute() else path.resolve()
    if "steamapps/common/helldivers 2" in path.as_posix().lower():
        raise ValueError("请选择制作目录保存数据")
    path.mkdir(parents=True, exist_ok=True)
    return path


DATA = data_root()
BUILTIN = INSTALL / "data/assets/stock/1.5.2"
EXPORT_ROOT = INSTALL / "mods"
EXPORT_ROOT.mkdir(parents=True, exist_ok=True)


def accessory_index(root):
    path = root / "accessory_catalog.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"weapons": {}, "modelHashes": {}}


def read_catalog(root):
    catalog = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    choices = accessory_index(root)["weapons"]
    catalog["weapons"] = [dict(row, **choices.get(row["weaponId"], {})) for row in catalog["weapons"]]
    return catalog


CATALOG = {"categories": {}, "weapons": []}
BUILTIN_ACCESSORIES = {}
BUILTIN_FILES = {}
WEAPONS = {}
PACK_HASH = None


def load_builtin_pack():
    global CATALOG, BUILTIN_ACCESSORIES, BUILTIN_FILES, PACK_HASH
    if not (BUILTIN / "asset_manifest.json").is_file():
        return
    CATALOG = read_catalog(BUILTIN)
    BUILTIN_ACCESSORIES = accessory_index(BUILTIN)
    BUILTIN_FILES = json.loads((BUILTIN / "asset_manifest.json").read_bytes())["files"]
    WEAPONS.update({row["weaponId"]: row for row in CATALOG["weapons"]})
    WEAPONS.update({old: dict(WEAPONS[current], weaponId=old) for old, current in CATALOG.get("weaponAliases", {}).items()})
    PACK_HASH = file_hash(BUILTIN / "catalog.json")


load_builtin_pack()


@lru_cache(maxsize=16)
def casing_index(root):
    path = root / "casing_units.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {"weapons": {}, "modelHashes": {}}


def safe_name(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value)).strip(" .")[:90]
    if not name or name.upper().split(".")[0] in ("CON", "PRN", "AUX", "NUL", *("COM" + str(i) for i in range(1, 10)), *("LPT" + str(i) for i in range(1, 10))):
        raise ValueError("请输入有效的工程名称")
    return name


def inside(root, relative):
    path = (Path(root) / relative).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError("文件路径超出制作目录")
    return path


def new_project():
    first = next(row for row in CATALOG["weapons"] if "Reprimand" in row["englishName"] and row["status"] == "ready")
    return dict(format=FORMAT, schemaVersion=SCHEMA_VERSION, projectId=uuid.uuid4().hex, revision=0,
                name="我的武器涂装", activePackKey="builtin.1.5.2", assetLocks={"builtin.1.5.2": PACK_HASH},
                activeWeaponId=first["weaponId"], weapons={}, layers=[], colors=[], links=[],
                viewState=dict(exploded=False, distance=0, selected=[], selectedQuads={},
                               camera=None, environment="neutral", mode="select", selectionMode="component", paintScope="selection", hiddenParts=[], restoredParts=[]))


def paint_layers(project):
    return project.get("colors", []) + project["layers"]


def emission_masks(layer):
    settings = layer.get("emission", {})
    if settings.get("maskHash"):
        yield settings["maskHash"]
    for region in settings.get("regions", []):
        yield region["maskHash"]


def validate_emission(layer):
    settings = layer.get("emission", {})
    if not isinstance(settings, dict) or settings.get("mode", "static") not in ("static", "breathe", "flow"):
        raise ValueError("发光模式无效")
    for key in ("intensity", "minimum", "maximum"):
        value = settings.get(key, 0)
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError("发光强度应大于或等于零")
    for key in ("phase", "period", "speed", "width"):
        if key in settings and (not isinstance(settings[key], (int, float)) or not math.isfinite(settings[key])):
            raise ValueError("发光动画参数无效")
    if settings.get("mode") == "breathe" and (
            settings.get("period", 0) <= 0 or settings.get("minimum", 0) > settings.get("maximum", 0)):
        raise ValueError("呼吸周期或强度范围无效")
    if settings.get("mode") == "flow":
        extent = settings.get("range")
        if (not 0 < settings.get("speed", 0) <= 20 or not 0 < settings.get("width", 0) <= 1 or
                settings.get("direction") not in ("frontToBack", "backToFront") or
                not isinstance(extent, list) or len(extent) != 2 or
                not all(isinstance(value, (int, float)) and math.isfinite(value) for value in extent) or
                extent[0] >= extent[1]):
            raise ValueError("流光速度、宽度、方向或模型范围无效")
    if "color" in settings and not re.fullmatch(r"#[0-9a-fA-F]{6}", str(settings["color"])):
        raise ValueError("发光色应为六位十六进制")
    if "regions" in settings:
        regions = settings["regions"]
        if (not layer.get("imageHash") or settings.get("maskHash") or
                not isinstance(regions, list) or not 1 <= len(regions) <= 8):
            raise ValueError("每个图片图层可添加 1 到 8 个发光区域")
        for region in regions:
            if (not isinstance(region, dict) or
                    not re.fullmatch(r"[0-9a-f]{64}", str(region.get("maskHash", ""))) or
                    not re.fullmatch(r"#[0-9a-fA-F]{6}", str(region.get("color", ""))) or
                    not isinstance(region.get("useImageColor", False), bool) or
                    not isinstance(region.get("name", ""), str) or len(region.get("name", "")) > 80):
                raise ValueError("图片发光区域无效")


def validate_project(project, *, available_weapons=None, image_hashes=None):
    if not isinstance(project, dict) or project.get("format") != FORMAT:
        raise ValueError("请选择武器涂装工具的工程文件")
    if project.get("schemaVersion") != SCHEMA_VERSION:
        raise ValueError("此工程需要对应版本的武器涂装工具")
    if not isinstance(project.get("revision"), int) or project["revision"] < 0:
        raise ValueError("工程 revision 无效")
    for field, expected in (("weapons", dict), ("layers", list), ("assetLocks", dict), ("viewState", dict)):
        if not isinstance(project.get(field), expected):
            raise ValueError("工程字段无效 " + field)
    if available_weapons is None:
        available_weapons = project_weapons(project)
    colors = project.setdefault("colors", [])
    if not isinstance(colors, list):
        raise ValueError("部件配色数据无效")
    # Older solid fills become persistent color edits with their original order.
    colors.extend(layer for layer in project["layers"] if layer.get("kind") in ("fill", "brush"))
    project["layers"] = [layer for layer in project["layers"] if layer.get("kind") not in ("fill", "brush")]
    links = project.setdefault("links", [])
    if not isinstance(links, list) or any(not isinstance(pair, list) or len(pair) != 2 or any(identity not in available_weapons for identity in pair) for pair in links):
        raise ValueError("装备关联无效")
    if len(paint_layers(project)) > 2000:
        raise ValueError("工程图层超过 2000 层")
    ids = set()
    for layer in paint_layers(project):
        if not isinstance(layer, dict) or layer.get("layerId") in ids:
            raise ValueError("图层身份重复或无效")
        ids.add(layer.get("layerId"))
        if layer.get("kind") not in ("fill", "decal", "text", "emission", "brush", "stroke"):
            raise ValueError("未知图层类型")
        for key in ("opacity", "roughness", "metalness"):
            value = layer.get(key, 1 if key == "opacity" else .5)
            if not isinstance(value, (int, float)) or not 0 <= value <= 1:
                raise ValueError("图层参数应在 0 到 1 之间 " + key)
        if layer.get("imageHash"):
            projection = layer.get("projection", {})
            if any(not isinstance(projection.get(key), (int, float)) or projection[key] <= 0 for key in ("width", "height", "depth")):
                raise ValueError("图片投射的宽、高和深度应大于零")
            if projection.get("type", "plane") not in ("plane", "cylinder"):
                raise ValueError("未知的贴花投射方式")
            if projection.get("depthMode", "centered") not in ("centered", "inward"):
                raise ValueError("贴花覆盖深度模式无效")
            if projection.get("type") == "cylinder" and not (isinstance(projection.get("radius"), (int, float)) and math.isfinite(projection["radius"]) and projection["radius"] > 0):
                raise ValueError("曲面环绕半径应大于零")
            crop = layer.get("crop", [0, 0, 1, 1])
            if len(crop) != 4 or not all(0 <= value <= 1 for value in crop) or crop[0] >= crop[2] or crop[1] >= crop[3]:
                raise ValueError("图片裁剪范围无效")
        validate_emission(layer)
        if layer.get("kind") == "stroke" or layer.get("kind") == "emission" and "brush" in layer:
            brush = layer.get("brush", {})
            stamps = brush.get("stamps")
            if not isinstance(stamps, list) or len(stamps) > 4096:
                raise ValueError("画笔笔触数据无效")
            for stamp in stamps:
                if not isinstance(stamp.get("radius"), (int,float)) or not 0 < stamp["radius"] <= .2:
                    raise ValueError("画笔半径无效")
                for key in ("position", "normal"):
                    if len(stamp.get(key, [])) != 3 or any(not isinstance(value,(int,float)) or not math.isfinite(value) for value in stamp[key]):
                        raise ValueError("画笔坐标无效")
        if layer.get("weaponId") not in available_weapons:
            # Custom packs are registered before their projects are validated.
            raise ValueError("工程使用的武器资产尚未加载")
        for identity in (layer.get("imageHash"), *emission_masks(layer)):
            if identity and image_hashes is None:
                image_path(identity)
            elif identity and identity not in image_hashes:
                raise ValueError("工程缺少素材 " + str(identity)[:12])
    upgrade_magazine_liners(project)
    json_bytes(project)
    return project


def pack_for(project=None):
    project = project or {}
    expected = project.get("assetLocks", {}).get(project.get("activePackKey", "builtin.1"), PACK_HASH)
    if expected == PACK_HASH:
        return BUILTIN
    recovered = DATA / "assets/recovered" / expected
    if recovered.is_dir():
        return recovered
    raise ValueError("工程锁定的资产版本缺失，请打开带内嵌资产的完整工程")


def project_weapons(project):
    root = pack_for(project)
    catalog = CATALOG if root == BUILTIN else read_catalog(root)
    return {row["weaponId"]: row for row in catalog["weapons"]}


def model(unit_id, project=None):
    if not re.fullmatch(r"0x[0-9a-f]{16}", unit_id):
        raise ValueError("无效的 Unit ID")
    root = pack_for(project)
    if root == BUILTIN:
        return builtin_model(unit_id)
    return read_model(root, unit_id)


@lru_cache(maxsize=80)
def builtin_model(unit_id):
    return read_model(BUILTIN, unit_id)


def read_model(root, unit_id):
    path = root / "models" / (unit_id[2:] + ".json")
    catalog = CATALOG if root == BUILTIN else json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    hashes = catalog.get("modelHashes", {}).get(unit_id) or casing_index(root)["modelHashes"].get(unit_id, {})
    if not hashes:
        index = BUILTIN_ACCESSORIES if root == BUILTIN else accessory_index(root)
        hashes = index["modelHashes"].get(unit_id, {})
    expected = BUILTIN_FILES.get(path.relative_to(root).as_posix(), hashes.get("json")) if root == BUILTIN else hashes.get("json")
    if expected and file_hash(path) != expected:
        raise ValueError("模型映射哈希不匹配，请恢复对应资产版本")
    return json.loads(path.read_text(encoding="utf-8"))


def upgrade_magazine_liners(project):
    """Keep edits to the old PLAS-39 sample on its original inner-liner faces."""
    host = "0x30061f91af477f5e"
    unit = "0xdd8cbbac49f7c628"
    original = host + "/ammo/magazine"
    merged = host + "/magazine_" + unit[2:] + "/magazine"
    matching = [layer for layer in paint_layers(project)
                if original in layer.get("target", {}).get("componentInstanceIds", ())]
    if not matching or project.get("assetLocks", {}).get(project.get("activePackKey")) != PACK_HASH:
        return
    mapping = model(unit, project)["mergedAmmo"]
    for layer in matching:
        target = layer["target"]
        selected = target["componentInstanceIds"]
        target["componentInstanceIds"] = list(dict.fromkeys(merged if identity == original else identity for identity in selected))
        if "ammoHosts" in target:
            target["ammoHosts"] = [identity for identity in target["ammoHosts"] if identity != host]
        layer.pop("ammoSource", None)
        if layer.get("anchor", {}).get("instanceId") == original:
            layer["anchor"]["instanceId"] = merged
        for stamp in layer.get("brush", {}).get("stamps", ()):
            if stamp["partId"] == original and stamp["unitId"] == unit:
                stamp["partId"] = merged
        coverage = layer.setdefault("coverage", {})
        sources = layer.setdefault("coverageSources", {})
        sources.pop(original, None)
        old_indices = coverage.pop(original, None)
        if layer.get("coverageMode") == "quads":
            quads = [mapping["firstQuad"] + index for index in (old_indices or ())
                     if isinstance(index, int) and 0 <= index < mapping["originalQuadCount"]]
        elif selected == [original]:
            layer["coverageMode"] = "quads"
            quads = range(mapping["firstQuad"], mapping["lastQuad"])
        else:
            continue
        coverage[merged] = list(dict.fromkeys([*coverage.get(merged, ()), *quads]))
        sources[merged] = dict(unitId=unit)


def weapon_state(project, weapon):
    state = project["weapons"].setdefault(weapon["weaponId"], {})
    state.setdefault("magazine", weapon.get("defaultMagazine"))
    state.setdefault("installed", {item["variantId"]: item.get("default", True) for item in weapon["attachments"]})
    state.setdefault("includeRelated", False)
    if "accessories" not in state:
        state["accessories"] = {}
        for item in weapon["attachments"]:
            if state["installed"].get(item["variantId"], item.get("default", False)):
                state["accessories"].setdefault(item["role"], item["variantId"])
        if state.get("magazine"):
            state["accessories"]["magazine"] = state["magazine"]
    items = [item for item in weapon["attachments"] + weapon["magazines"] if not item.get("unavailable")]
    for role in {item["role"] for item in items}:
        allowed = {item["variantId"] for item in items if item["role"] == role}
        chosen = state["accessories"].get(role)
        if chosen not in allowed:
            canonical = weapon.get("attachmentAliases", {}).get(chosen)
            if canonical in allowed:
                chosen = state["accessories"][role] = canonical
        if weapon["category"] == "support":
            chosen = next((item["variantId"] for item in items if item["role"] == role and item.get("default")), None)
            state["accessories"][role] = chosen
        if chosen not in allowed and (chosen or "attachmentAliases" in weapon):
            default = next((item["variantId"] for item in items if item["role"] == role and item.get("default")), None)
            state["accessories"][role] = default
            if chosen:
                project.setdefault("compatibilityAdjustments", []).append(dict(weaponId=weapon["weaponId"], role=role,
                                                                               previous=chosen, replacement=default))
        if role == "magazine":
            state["magazine"] = state["accessories"].get(role)
    return state


def transformed_bounds(bounds, matrix):
    minimum, maximum = np.asarray(bounds)
    corners = np.array([[x, y, z, 1] for x in (minimum[0], maximum[0]) for y in (minimum[1], maximum[1]) for z in (minimum[2], maximum[2])])
    points = (corners @ matrix.T)[:, :3]
    return [points.min(0).tolist(), points.max(0).tolist()]


def linked_weapon_ids(project, weapon_id):
    catalog = project_weapons(project)
    links = project.setdefault("links", [])
    # Upgrade the older one-way toggle into the symmetric project relation.
    for identity, state in list(project["weapons"].items()):
        if state.get("includeRelated") and identity in catalog:
            related = catalog[identity].get("relatedWeaponIds", [])
            if related and not any(identity in pair for pair in links):
                links.append([identity, related[0]])
            state["includeRelated"] = False
    occupied = set()
    current = []
    for pair in reversed(links):
        if occupied.intersection(pair):
            continue
        current.append(pair)
        occupied.update(pair)
    links = list(reversed(current))
    project["links"] = links
    linked = [weapon_id]
    for pair in links:
        if weapon_id in pair:
            linked.extend(identity for identity in pair if identity != weapon_id)
    return list(dict.fromkeys(linked))


def assembly(project, weapon_id=None, include_inactive=False):
    weapon_id = weapon_id or project["activeWeaponId"]
    weapon = project_weapons(project).get(weapon_id)
    if not weapon or weapon["status"] != "ready":
        raise ValueError(weapon.get("reason", "没有可用的武器资产") if weapon else "未知武器")
    catalog = project_weapons(project)
    casings = casing_index(pack_for(project))["weapons"]
    group = linked_weapon_ids(project, weapon_id)
    definitions = []
    removed = []
    for identity in group:
        host = catalog[identity]
        state = weapon_state(project, host)
        body = model(host["unitId"], project)
        host_matrix = np.eye(4)
        if len(group) > 1 and host["category"] == "backpack":
            host_matrix[:3, 3] = [-.9, 0, .05]
        definitions.append((identity, "body", host["unitId"], host_matrix, True, None))
        for item in host["magazines"] + host["attachments"]:
            if item.get("unavailable"):
                continue
            active = state["accessories"].get(item["role"]) == item["variantId"]
            if not include_inactive and not active:
                continue
            if item.get("empty"):
                if active and item.get("exportUnitId"):
                    removed.append(dict(hostEntityId=identity, role=item["role"], label=item["label"],
                                        exportUnitId=item["exportUnitId"], templateUnitId=item["unitId"]))
                continue
            target = model(item["unitId"], project)
            matrix = np.asarray(item.get("mountTransform") or body["sockets"][item["socket"]], dtype=float)
            if item["role"] == "optic" and "attach_offset" in target["sockets"]:
                matrix = matrix @ np.linalg.inv(np.asarray(target["sockets"]["attach_offset"]))
            definitions.append((identity, item["variantId"], item["unitId"], host_matrix @ matrix, active, item))
            if secondary := item.get("secondary"):
                paired = dict(item, unitId=secondary["unitId"], exportUnitId=secondary["unitId"],
                              socket=secondary["socket"], label="第二" + item["label"], paired=True)
                if paired["unitId"] == item["unitId"]:
                    paired["paintSourceVariant"] = item["variantId"]
                paired_matrix = np.asarray(body["sockets"][secondary["socket"]], dtype=float)
                definitions.append((identity, item["variantId"] + "_secondary", paired["unitId"],
                                    host_matrix @ paired_matrix, active, paired))
        casing_units = dict.fromkeys(row["unitId"] for row in casings.get(identity, ()))
        definitions.extend((identity, "casing_" + unit_id[2:], unit_id, host_matrix, True, None) for unit_id in casing_units)
    parts = []
    for identity, variant, unit_id, matrix, active, attachment in definitions:
        target = model(unit_id, project)
        for component in target["parts"]:
            if component["role"] == "lens":
                continue
            instance_id = "/".join((identity, variant, component["componentId"]))
            if component["role"] == "ammo" and attachment and attachment.get("role") == "magazine":
                instance_id = identity + ("/ammo/magazine_secondary" if attachment.get("paired") else "/ammo/magazine")
            part = dict(component)
            part.update(instanceId=instance_id, hostEntityId=identity, hostLabel=catalog[identity]["label"], unitId=unit_id,
                        exportUnitId=attachment.get("exportUnitId", unit_id) if attachment else unit_id,
                        variantId=variant, assembledTransform=matrix.tolist(), active=active,
                        bounds=transformed_bounds(component["bounds"], matrix), localBounds=copy.deepcopy(component["bounds"]), attachment=attachment)
            if variant.startswith("casing_"):
                part["ejectedCasing"] = True
            if attachment and attachment.get("paintSourceVariant"):
                part["paintSourceId"] = (identity + "/ammo/magazine" if component["role"] == "ammo" else
                                         "/".join((identity, attachment["paintSourceVariant"], component["componentId"])))
            if attachment and component["componentId"] == attachment["role"]:
                part["label"] = attachment["label"].replace("（原装）", "")
            parts.append(part)
    # The workbench shows one detached ammunition sample. Native geometry and
    # assembled transforms remain intact for UV baking and synchronized export.
    for identity in group:
        magazine_ammo = [part for part in parts if part["active"] and part["hostEntityId"] == identity and part["role"] == "ammo"]
        exterior = [part for part in parts if part["active"] and part["hostEntityId"] == identity and part["role"] != "ammo"]
        if magazine_ammo and exterior:
            lower = np.min([part["bounds"][0] for part in exterior], axis=0)
            upper = np.max([part["bounds"][1] for part in exterior], axis=0)
            sample = max(magazine_ammo, key=lambda part: (not part.get("ejectedCasing"),
                         (part.get("attachment") or {}).get("role") == "magazine", part["count"]))
            for part in magazine_ammo:
                part["ammoSyncSourceId"] = sample["instanceId"]
                part["previewOmitted"] = part is not sample
                part["displayAmmoSample"] = part is sample
                minimum, maximum = np.asarray(part["bounds"])
                center = (minimum + maximum) / 2
                part["workspaceOffset"] = [float((lower[0] + upper[0]) / 2 - center[0]), 0., float(lower[2] - .045 - maximum[2])]
                part["label"] = "弹药（同步）"
                part["detachedAmmoSample"] = True
    active_parts = [part for part in parts if part["active"] and not part.get("previewOmitted")]
    layout = explosion_layout(active_parts)
    for part in parts:
        part["explosionDelta"] = layout.get(part["instanceId"], [0, 0, 0])
    return dict(weapon=weapon, parts=parts, assetRevision=project.get("activePackKey", "builtin.1"),
                state=weapon_state(project, weapon), linkedWeaponIds=group, links=project["links"],
                states={identity: weapon_state(project, catalog[identity]) for identity in group}, removedAttachments=removed)


def image_path(identity):
    if not re.fullmatch(r"[0-9a-f]{64}", str(identity)):
        raise ValueError("素材哈希无效")
    path = DATA / "images" / (identity + ".png")
    if not path.is_file():
        raise ValueError("工程缺少素材 " + identity[:12])
    return path


def import_image(data):
    raw = base64.b64decode(data["data"].split(",")[-1], validate=True)
    if len(raw) > 64 * 1024 * 1024:
        raise ValueError("单张图片请控制在 64 MB 内")
    image = Image.open(io.BytesIO(raw))
    image.load()
    if image.width * image.height > 48_000_000:
        raise ValueError("图片像素超过 4800 万")
    image = image.convert("RGBA")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    normalized = buffer.getvalue()
    identity = hashlib.sha256(normalized).hexdigest()
    original = DATA / "originals" / hashlib.sha256(raw).hexdigest()
    if not original.exists():
        atomic_write(original, raw)
    path = DATA / "images" / (identity + ".png")
    if not path.exists():
        atomic_write(path, normalized)
    metadata = dict(hash=identity, name=str(data.get("name", "图片"))[:120], width=image.width, height=image.height,
                    purpose="mask" if data.get("purpose") == "mask" else "image",
                    originalHash=hashlib.sha256(raw).hexdigest(), importedAt=time.time())
    meta_path = path.with_suffix(".json")
    with LOCK:
        previous = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else None
        if previous and data.get("purpose") == "mask":
            metadata = previous
        atomic_write(meta_path, json_bytes(metadata))
    return metadata


def remove_library_image(identity):
    path = image_path(identity).with_suffix(".json")
    with LOCK:
        metadata = json.loads(path.read_text(encoding="utf-8"))
        metadata["hidden"] = True
        atomic_write(path, json_bytes(metadata))
    return library()


def library():
    metadata = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((DATA / "images").glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)]
    masks, artwork = set(), set()
    for path in (DATA / "recovery").glob("*.json"):
        project = json.loads(path.read_text(encoding="utf-8"))
        for layer in project.get("layers", []):
            if layer.get("imageHash"):
                artwork.add(layer["imageHash"])
            masks.update(emission_masks(layer))
    return [item for item in metadata if not item.get("hidden") and item.get("purpose") != "mask" and item["hash"] not in masks - artwork]


def text_image(data):
    text = str(data.get("text", "自由"))[:200]
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/msyh.ttc"
    font = ImageFont.truetype(str(font_path), 120) if font_path.is_file() else ImageFont.load_default(size=100)
    bounds = font.getbbox(text or " ")
    image = Image.new("RGBA", (max(8, bounds[2] - bounds[0] + 32), max(8, bounds[3] - bounds[1] + 32)))
    ImageDraw.Draw(image).text((16 - bounds[0], 16 - bounds[1]), text, font=font, fill=data.get("color", "#ffffff"))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return import_image(dict(data=base64.b64encode(buffer.getvalue()).decode(), name=text))


def project_image_hashes(project):
    return {value for layer in paint_layers(project) for value in (layer.get("imageHash"), *emission_masks(layer)) if value}


def selected_pack_files(project):
    root = pack_for(project)
    catalog_weapons = project_weapons(project)
    units = set()
    empty = root / "native/unit/0040c16bd48573e2.unit.main"
    if empty.exists():
        units.add("0x0040c16bd48573e2")
    for weapon_id in set(project["weapons"]) | {project["activeWeaponId"]}:
        if weapon_id in catalog_weapons and catalog_weapons[weapon_id]["status"] == "ready":
            units.update(part["unitId"] for part in assembly(project, weapon_id, True)["parts"])
    files = [root / "catalog.json", root / "asset_manifest.json"]
    for name in ("casing_units.json", "accessory_catalog.json"):
        if (root / name).is_file():
            files.append(root / name)
    for identity in units:
        files.extend((root / "models").glob(identity[2:] + ".*"))
        files.extend((root / "native/unit").glob(identity[2:] + ".*"))
    for folder in ("native/material", "native/texture", "materials"):
        files.extend((root / folder).glob("*"))
    preview = root / "preview/materials.json"
    if preview.is_file():
        descriptions = json.loads(preview.read_text(encoding="utf-8"))
        files.append(preview)
        for identity in units:
            descriptor = root / "models" / (identity[2:] + ".json")
            if not descriptor.is_file():
                continue
            for part in json.loads(descriptor.read_text(encoding="utf-8"))["parts"]:
                if part["role"]=="lens":
                    continue
                for group in part.get("materialGroups", []):
                    key = group.get("previewMaterial", group["material"])
                    files.extend(root / "preview" / name for name in descriptions[key]["files"].values())
    return root, sorted(set(files))


def project_bytes(project):
    validate_project(project)
    portable = copy.deepcopy(project)
    migration_info = portable.get("migration", {})
    migration_source = migration_info.pop("sourceArchiveHash", None)
    if migration_source:
        if not re.fullmatch(r"[0-9a-f]{64}", str(migration_source)):
            raise ValueError("迁移来源工程哈希无效")
        source = DATA / "migration_sources" / (migration_source + ".hd2project")
        if file_hash(source) != migration_source:
            raise ValueError("迁移来源工程哈希不匹配")
        migration_info["sourceArchiveReference"] = migration_source
    buffer = io.BytesIO()
    inventory = {}
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
        def write(name, raw):
            # 固定成员时间戳，工程快照才可逐字节复现。
            info = zipfile.ZipInfo(name, ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info._compresslevel = 3
            archive.writestr(info, raw)

        raw = json_bytes(portable)
        write("project.json", raw)
        inventory["project.json"] = hashlib.sha256(raw).hexdigest()
        for identity in sorted(project_image_hashes(project)):
            raw = image_path(identity).read_bytes()
            write("images/" + identity + ".png", raw)
            inventory["images/" + identity + ".png"] = hashlib.sha256(raw).hexdigest()
            metadata = json.loads(image_path(identity).with_suffix(".json").read_text(encoding="utf-8"))
            name = "images/" + identity + ".json"
            raw = json_bytes(metadata)
            write(name, raw)
            inventory[name] = hashlib.sha256(raw).hexdigest()
            original = DATA / "originals" / metadata["originalHash"]
            if original.exists():
                name = "originals/" + metadata["originalHash"]
                if name not in inventory:
                    raw = original.read_bytes()
                    write(name, raw)
                    inventory[name] = hashlib.sha256(raw).hexdigest()
        root, files = selected_pack_files(project)
        for path in files:
            name = "assets/" + path.relative_to(root).as_posix()
            raw = path.read_bytes()
            write(name, raw)
            inventory[name] = hashlib.sha256(raw).hexdigest()
        write("inventory.json", json_bytes(inventory))
    return buffer.getvalue()


def reserve_version(folder, prefix="1."):
    folder.mkdir(parents=True, exist_ok=True)
    minor = 1
    while True:
        version = folder / (prefix + str(minor))
        try:
            version.mkdir()
            return version
        except FileExistsError:
            minor += 1


def save_project(project, purpose="projects"):
    with LOCK:
        validate_project(project)
        folder = DATA / purpose / safe_name(project["name"])
        version = reserve_version(folder)
        path = version / "project.hd2project"
        try:
            atomic_write(path, project_bytes(project))
        except Exception:
            version.rmdir()
            raise
        return dict(path=str(path), relativePath=path.relative_to(DATA).as_posix(), revision=project["revision"])


def inspect_project_archive(archive):
    """Validate the portable project using its own catalog and images."""
    members = archive.infolist()
    if sum(member.file_size for member in members) > 2 * 1024 ** 3:
        raise ValueError("工程解压后超过 2 GB")
    names = [member.filename for member in members]
    if len(names) != len(set(names)):
        raise ValueError("工程内存在重复文件")
    inventory = json.loads(archive.read("inventory.json"))
    if not isinstance(inventory, dict) or set(names) != set(inventory) | {"inventory.json"}:
        raise ValueError("工程文件清单与内容不一致")
    image_hashes = set()
    for name, expected in inventory.items():
        if inside(DATA / "tmp/import", name).relative_to((DATA / "tmp/import").resolve()).as_posix() != name:
            raise ValueError("工程成员路径无效 " + name)
        with archive.open(name) as member:
            if hashlib.file_digest(member, "sha256").hexdigest() != expected:
                raise ValueError("工程内文件校验失败 " + name)
        if name.startswith("images/") and name.endswith(".png"):
            if name != f"images/{expected}.png":
                raise ValueError("素材身份与内容不一致")
            image_hashes.add(expected)
        elif name.startswith("images/") and name.endswith(".json"):
            metadata = json.loads(archive.read(name))
            identity = metadata.get("hash")
            if (not re.fullmatch(r"[0-9a-f]{64}", str(identity)) or name != f"images/{identity}.json" or
                    not re.fullmatch(r"[0-9a-f]{64}", str(metadata.get("originalHash")))):
                raise ValueError("素材元数据身份无效")
        elif name.startswith("originals/") and name != f"originals/{expected}":
            raise ValueError("原图来源哈希无效")
    project = json.loads(archive.read("project.json"))
    pack_hash = project.get("assetLocks", {}).get(project.get("activePackKey", "builtin.1"))
    if not re.fullmatch(r"[0-9a-f]{64}", str(pack_hash)) or inventory.get("assets/catalog.json") != pack_hash:
        raise ValueError("工程资产目录哈希不匹配")
    catalog = json.loads(archive.read("assets/catalog.json"))
    available = {row["weaponId"]: row for row in catalog["weapons"]}
    validate_project(project, available_weapons=available, image_hashes=image_hashes)
    return project, pack_hash, available, inventory


def open_project(raw, restore_to_workbench=True):
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        project, pack_hash, available, inventory = inspect_project_archive(archive)
        if not restore_to_workbench:
            return project
        recovered = DATA / "assets/recovered" / pack_hash
        # A built-in archive uses the bundled catalog even if its portable
        # snapshot predates the dependency additions under the same lock.
        if pack_hash != PACK_HASH:
            for name, expected in inventory.items():
                if name.startswith("assets/"):
                    target = inside(recovered, name[7:])
                    if target.exists() and file_hash(target) != expected:
                        raise ValueError("已恢复资产内容冲突 " + name[7:])
        for name in inventory:
            if name.startswith("images/") and name.endswith(".png"):
                identity = Path(name).stem
                target = inside(DATA, name)
                if not target.exists():
                    atomic_write(target, archive.read(name))
                    if target.with_suffix(".json").relative_to(DATA).as_posix() not in inventory:
                        with Image.open(target) as image:
                            atomic_write(target.with_suffix(".json"), json_bytes(dict(
                                hash=identity, name="工程素材", width=image.width, height=image.height, originalHash=identity)))
            elif name.startswith("images/") and name.endswith(".json"):
                atomic_write(inside(DATA, name), archive.read(name))
            elif name.startswith("originals/"):
                target = inside(DATA, name)
                if not target.exists():
                    atomic_write(target, archive.read(name))
            elif name.startswith("assets/") and pack_hash != PACK_HASH:
                target = inside(recovered, name[7:])
                if not target.exists():
                    atomic_write(target, archive.read(name))
            elif name == "migration_source.hd2project":
                atomic_write(DATA / "migration_sources" / (inventory[name] + ".hd2project"), archive.read(name))
        # Register archived catalog entries, preserving their locked model pack.
        for identity, weapon in available.items():
            WEAPONS.setdefault(identity, weapon)
        reference = project.get("migration", {}).get("sourceArchiveReference")
        if reference and re.fullmatch(r"[0-9a-f]{64}", str(reference)):
            local_source = DATA / "migration_sources" / (reference + ".hd2project")
            if local_source.is_file() and file_hash(local_source) == reference:
                project["migration"]["sourceArchiveHash"] = reference
        validate_project(project)
        autosave(project, force=True)
        return project


def autosave(project, force=False):
    validate_project(project)
    with LOCK:
        path = DATA / "recovery/autosave.json"
        previous = DATA / "recovery/previous.json"
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8"))
            if not force and current.get("projectId") == project["projectId"] and current.get("revision", 0) > project["revision"]:
                return dict(saved=False, revision=current["revision"], reason="newer_revision_already_saved")
            atomic_write(previous, path.read_bytes())
        atomic_write(path, json_bytes(project))
    return dict(saved=True, revision=project["revision"])


def restore():
    for name in ("autosave.json", "previous.json"):
        try:
            project = json.loads((DATA / "recovery" / name).read_text(encoding="utf-8"))
            return validate_project(project)
        except (OSError, ValueError, KeyError):
            continue
    return new_project()


def shared_impact(unit_ids):
    units = set(unit_ids)
    users = []
    casings = casing_index(BUILTIN)["weapons"]
    for weapon in WEAPONS.values():
        if weapon["weaponId"] in CATALOG.get("weaponAliases", {}):
            continue
        refs = weapon.get("configuration", {}).get("effective_references", {})
        if (weapon["unitId"] in units or any(value.get("hash") in units for value in refs.values()) or
                any(item.get("exportUnitId") in units or (item.get("secondary") or {}).get("unitId") in units
                    for item in weapon["attachments"] + weapon["magazines"]) or
                any(row["unitId"] in units for row in casings.get(weapon["weaponId"], ()))):
            users.append(dict(weaponId=weapon["weaponId"], label=weapon["label"]))
    return users


def upgrade_projection_depth(layer, parts):
    """Anchor a legacy plane at the outer surface while retaining its inward reach."""
    projection = layer.get("projection", {})
    if not layer.get("imageHash") or projection.get("type", "plane") != "plane" or projection.get("depthMode") == "inward":
        return
    from weapon_baker import layer_applies
    targets = [part for part in parts if part["active"] and layer_applies({**layer, "enabled": True}, part)]
    if not targets:
        return
    origin = np.asarray(projection["origin"], dtype=float)
    normal = np.asarray(projection["normal"], dtype=float)
    normal /= max(float(np.linalg.norm(normal)), 1e-12)
    anchor = layer.get("anchor", {})
    anchored = next((part for part in parts if part["instanceId"] == anchor.get("instanceId")), None)
    delta = np.eye(4)
    if anchored and anchor.get("assembledTransform"):
        delta = np.asarray(anchored["assembledTransform"]) @ np.linalg.inv(np.asarray(anchor["assembledTransform"]))
    world_origin = (delta @ np.array([*origin, 1]))[:3]
    world_normal = delta[:3, :3] @ normal
    normal_scale = max(float(np.linalg.norm(world_normal)), 1e-12)
    world_normal /= normal_scale
    front = max(float(np.dot(np.where(world_normal >= 0, part["bounds"][1], part["bounds"][0]) - world_origin, world_normal)) for part in targets)
    shift = (front + .00002) / normal_scale
    projection["origin"] = (origin + normal * shift).tolist()
    projection["normal"] = normal.tolist()
    projection["depth"] = max(.0001, projection["depth"] + shift)
    projection["depthMode"] = "inward"


def migration(project, target_pack, component_remap):
    """Preserve unmapped layers in a new project revision with explicit reports."""
    target = BUILTIN if target_pack == "builtin" else inside(DATA / "assets", target_pack)
    catalog_path = target / "catalog.json"
    if not catalog_path.is_file():
        raise ValueError("请选择已导入的资产包")
    updated = copy.deepcopy(project)
    source_archive = project_bytes(project)
    source_archive_hash = hashlib.sha256(source_archive).hexdigest()
    atomic_write(DATA / "migration_sources" / (source_archive_hash + ".hd2project"), source_archive)
    manifest = json.loads((target / "asset_manifest.json").read_text(encoding="utf-8"))
    key = manifest["packId"] + "." + manifest["version"]
    updated["activePackKey"] = key
    updated["assetLocks"] = {key: file_hash(catalog_path)}
    recovered = DATA / "assets/recovered" / file_hash(catalog_path)
    if target != BUILTIN and not recovered.exists():
        import shutil
        shutil.copytree(target, recovered)
    new_parts = {}
    aliases = CATALOG.get("weaponAliases", {}) if target == BUILTIN else {}
    if aliases:
        remap_retired_weapons(updated, aliases)
    retired_content = dict(weapons=[], layers=[], colors=[], links=[])
    if target == BUILTIN:
        allowed = set(project_weapons(updated))
        retired_content["weapons"] = [identity for identity in updated["weapons"] if identity not in allowed]
        updated["weapons"] = {identity:value for identity,value in updated["weapons"].items() if identity in allowed}
        for collection in ("layers","colors"):
            retired_content[collection] = [layer["layerId"] for layer in updated.get(collection,[]) if layer["weaponId"] not in allowed]
            updated[collection] = [layer for layer in updated.get(collection,[]) if layer["weaponId"] in allowed]
        retired_content["links"] = updated.get("links",[])
        updated["links"] = []
        if updated["activeWeaponId"] not in allowed:
            updated["activeWeaponId"] = new_project()["activeWeaponId"]
        view = updated["viewState"]
        view["selected"] = [identity for identity in view.get("selected",[]) if identity.split('/')[0] in allowed]
        view["hiddenParts"] = [identity for identity in view.get("hiddenParts",[]) if identity.split('/')[0] in allowed]
        view["restoredParts"] = [identity for identity in view.get("restoredParts",[]) if identity.split('/')[0] in allowed]
        view["selectedQuads"] = {}
        view["selectionMode"] = "component"
        view["mode"] = "select"
    for weapon_id in updated["weapons"]:
        if weapon_id in project_weapons(updated):
            new_parts.update({part["instanceId"]: part for part in assembly(updated, weapon_id, True)["parts"]})
    old_parts = {}
    for weapon_id in project["weapons"]:
        old_parts.update({part["instanceId"]: part for part in assembly(project, weapon_id, True)["parts"]})
    if aliases:
        old_parts = {aliases.get(key.split("/", 1)[0], key.split("/", 1)[0]) + "/" + key.split("/", 1)[1]: part
                     for key, part in old_parts.items()}
    legacy = {}
    omitted_optical = {}
    if target == BUILTIN:
        for identity, part in old_parts.items():
            candidates = {}
            for candidate in new_parts.values():
                if candidate["hostEntityId"] != part["hostEntityId"] or candidate["variantId"] != part["variantId"]:
                    continue
                descriptor = model(candidate["unitId"], updated)
                if candidate["unitId"] != part["unitId"] and descriptor.get("sourceUnitId") != part["unitId"]:
                    continue
                source = descriptor.get("legacyQuadSources", {}).get(part["componentId"])
                mapping = descriptor.get("legacyQuadMappings", {}).get(part["componentId"], {}) if source == part["quads"] else {}
                omitted_optical.setdefault(identity,set()).update(int(key) for key in mapping.get("lens",{}))
                role = candidate["componentId"]
                if mapping.get(role):
                    candidates[candidate["instanceId"]] = {int(key): value for key, value in mapping[role].items()}
                elif candidate["instanceId"] == identity and candidate["unitId"] != part["unitId"] and candidate["quads"] == part["quads"]:
                    candidates[identity] = {index: index for index in range(len(part["quads"]))}
            if candidates:
                legacy[identity] = candidates
    report = []
    for layer in paint_layers(updated):
        original = layer.get("target", {}).get("componentInstanceIds", [])
        remapped_coverage = set()
        legacy_pending = []
        for identity, candidates in legacy.items():
            if identity in component_remap:
                continue
            if identity in original:
                component_remap_for_layer = list(candidates)
                layer.setdefault("target", {})["componentInstanceIds"] = [new for old in layer["target"]["componentInstanceIds"] for new in (component_remap_for_layer if old == identity else [old])]
            excluded = layer.get("target", {}).get("excludedComponentInstanceIds", [])
            if identity in excluded:
                layer["target"]["excludedComponentInstanceIds"] = list(dict.fromkeys(new for old in excluded for new in (candidates if old == identity else [old])))
            if layer.get("coverageMode") == "quads" and identity in layer.get("coverage", {}):
                selection = layer["coverage"].pop(identity)
                source = layer.get("coverageSources", {}).pop(identity, {"unitId": old_parts[identity]["unitId"]})
                matched = set()
                for candidate, mapping in candidates.items():
                    selected = [mapping[index] for index in selection if index in mapping]
                    matched.update(index for index in selection if index in mapping)
                    if selected:
                        layer["coverage"][candidate] = selected
                        layer.setdefault("coverageSources", {})[candidate] = {**source, "unitId": new_parts[candidate]["unitId"]}
                        remapped_coverage.add(candidate)
                if set(selection) - matched - omitted_optical.get(identity,set()):
                    layer["coverage"][identity] = selection
                    layer.setdefault("coverageSources", {})[identity] = source
                    legacy_pending.append(identity)
            anchor = layer.get("anchor", {})
            if anchor.get("instanceId") == identity:
                point = np.asarray(anchor.get("localPosition", [0, 0, 0]))
                candidate = min(candidates, key=lambda key: float(np.linalg.norm((np.asarray(new_parts[key]["bounds"]).mean(0)) -
                    (np.asarray(old_parts[identity]["assembledTransform"]) @ np.array([*point, 1]))[:3])))
                anchor["instanceId"] = candidate
                anchor.pop("triangle", None)
        original = layer.get("target", {}).get("componentInstanceIds", [])
        mapped = []
        pending = []
        for identity in original:
            match = component_remap.get(identity)
            if match is None:
                mapped.append(identity)
                if identity not in new_parts:
                    pending.append(identity)
            else:
                mapped.extend(match if isinstance(match, list) else [match])
        layer.setdefault("target", {})["componentInstanceIds"] = mapped
        ammo_parts = [new_parts[identity] for identity in mapped if identity in new_parts and new_parts[identity]["role"]=="ammo"]
        if ammo_parts:
            layer["target"]["ammoHosts"] = sorted({part["hostEntityId"] for part in ammo_parts})
            sample = next((part for part in new_parts.values() if part.get("displayAmmoSample") and part["hostEntityId"]==ammo_parts[0]["hostEntityId"]),ammo_parts[0])
            component = next(row for row in model(sample["unitId"],updated)["parts"] if row["componentId"]==sample["componentId"])
            layer.setdefault("ammoSource",dict(unitId=sample["unitId"],componentId=sample["componentId"],instanceId=sample["instanceId"],
                                              bounds=component["bounds"],assembledTransform=sample["assembledTransform"]))
            layer["target"]["componentInstanceIds"] = list(dict.fromkeys(sample["instanceId"] if identity in new_parts and new_parts[identity]["role"]=="ammo" else identity for identity in mapped))
        if target == BUILTIN and layer.get("imageHash") and layer.get("projection",{}).get("type","plane")=="plane":
            projection = layer["projection"]
            if not all(axis in projection for axis in ("u", "v", "normal")):
                side = projection.get("side", "front")
                projection.update(side=side, u=[1 if side == "front" else -1, 0, 0],
                                  v=[0, 0, 1], normal=[0, -1 if side == "front" else 1, 0], cutoff=.02)
            upgrade_projection_depth(layer, assembly(updated, layer["weaponId"])["parts"])
        coverage_pending = list(legacy_pending)
        if layer.get("coverageMode") == "quads":
            for identity, selection in layer.get("coverage", {}).items():
                old, new = old_parts.get(identity), new_parts.get(identity)
                if identity not in remapped_coverage and (not old or not new or old["quads"] != new["quads"]):
                    coverage_pending.append(identity)
        if pending or coverage_pending:
            layer["migrationPending"] = dict(targets=pending, coverage=coverage_pending)
        else:
            layer.pop("migrationPending", None)
        report.append(dict(layerId=layer["layerId"], matched=len(original) - len(pending), pending=pending,
                           matchRatio=(len(original) - len(pending)) / max(len(original), 1), coverageReview=coverage_pending))
    updated["revision"] += 1
    updated["projectId"] = uuid.uuid4().hex
    updated["migration"] = dict(sourceProjectId=project["projectId"], sourceArchiveHash=source_archive_hash, layers=report,
                                retiredContent=retired_content, projectionConvention="outer_surface_inward_depth")
    return dict(project=updated, report=report)


def remap_retired_weapons(project, aliases):
    """Retain user artwork while correcting retired catalog identities."""
    def identity(value):
        if not isinstance(value, str):
            return value
        host, separator, tail = value.partition("/")
        return aliases.get(host, host) + separator + tail

    project["activeWeaponId"] = identity(project["activeWeaponId"])
    for old, current in aliases.items():
        if old in project["weapons"]:
            state = project["weapons"].pop(old)
            project["weapons"].setdefault(current, state)
    project["links"] = [[identity(value) for value in pair] for pair in project.get("links", [])]
    view = project["viewState"]
    for key in ("selected", "hiddenParts", "restoredParts"):
        view[key] = [identity(value) for value in view.get(key, [])]
    view["selectedQuads"] = {identity(key): value for key, value in view.get("selectedQuads", {}).items()}
    for layer in paint_layers(project):
        layer["weaponId"] = identity(layer["weaponId"])
        target = layer.get("target", {})
        for key in ("componentInstanceIds", "assemblyWeaponIds", "excludedComponentInstanceIds"):
            if key in target:
                target[key] = [identity(value) for value in target[key]]
        for key in ("coverage", "coverageSources"):
            if key in layer:
                layer[key] = {identity(name): value for name, value in layer[key].items()}
        if layer.get("anchor", {}).get("instanceId"):
            layer["anchor"]["instanceId"] = identity(layer["anchor"]["instanceId"])
