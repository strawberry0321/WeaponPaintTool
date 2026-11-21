"""Native geometry-preserving output and verified manager-compatible packages."""
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import copy
import gzip
import hashlib
import io
import json
import math
import os
import shutil
import struct
import subprocess
import time
import zipfile

from PIL import Image
from compute_runtime import np
from archive_layout import validate_archive
from block_encoder import encode_mip, prepare_mip, pack_range, block_total
from weapon_assets import hash64, thin, bones, read_group
import native_writer as native
import native_motion as motion
import studio_core as core
import weapon_baker as baker

UNIT = 0xe0a48d0be9a7453f
MATERIAL = 0xeac0b497876adedf
TEXTURE = 0xcd4238c6a0c69e32
PATCH = "9ba626afa44a3aa3.patch_0"

WORKERS = max(1, int(os.environ.get("HD2_EXPORT_WORKERS", "0")) or min(16, os.cpu_count() or 4))
CHUNK_BLOCKS = 16384
POOL = None


def worker_pool():
    global POOL
    if POOL is None:
        POOL = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="texture")
    return POOL


def array_digest(array):
    """按缓冲协议直接哈希，避免 tobytes 复制整张贴图。"""
    return hashlib.sha256(np.ascontiguousarray(array, dtype="<f4")).hexdigest()


def channel_planes(source):
    """通道拆成连续平面；PIL 对非连续视图会整块复制，先拆开可省下每个 MIP 的重复拷贝。"""
    return [np.ascontiguousarray(source[:, :, channel]) for channel in range(4)]


def build_mip(planes, size):
    return np.clip(np.stack([np.asarray(Image.fromarray(plane).resize(size, Image.Resampling.LANCZOS)) for plane in planes],
                            axis=2), 0, 1)


def mip_layout(width, height, count, fmt):
    block_bytes = 8 if fmt in (71, 72, 80) else 16
    dimensions = [(max(1, width >> mip), max(1, height >> mip)) for mip in range(count)]
    return [(w, h, ((w + 3) // 4) * ((h + 3) // 4) * block_bytes) for w, h in dimensions]


def export_plan(project, scope="current"):
    core.validate_project(project)
    if scope != "current":
        raise ValueError("导出范围为当前武器")
    weapon_id = project["activeWeaponId"]
    weapons = core.project_weapons(project)
    assembly = core.assembly(project, weapon_id)
    changed = [part for part in assembly["parts"] if part["active"] and not part.get("paintSourceId") and
               (not part.get("ejectedCasing") or baker.layers_for(part, project))]
    removed = assembly["removedAttachments"]
    issues = []
    issues.extend(dict(code="migration_pending", layerId=layer["layerId"], message="此图层有待复核的资产迁移映射，请在选面后重新绑定图层范围")
                  for layer in core.paint_layers(project) if layer.get("migrationPending") and any(baker.layer_applies(layer, part) for part in changed))
    output_units = {part["exportUnitId"] for part in changed} | {item["exportUnitId"] for item in removed}
    casing_units = {part["exportUnitId"] for part in changed if part.get("ejectedCasing")}
    casing_effects = [dict(hostWeaponId=identity, **row) for identity in assembly["linkedWeaponIds"]
                      for row in core.casing_index(core.pack_for(project))["weapons"].get(identity, ())
                      if row["unitId"] in casing_units]
    selected_attachments = []
    for identity in assembly["linkedWeaponIds"]:
        host = weapons[identity]
        selected = assembly["states"][identity]["accessories"]
        for item in host["attachments"] + host["magazines"]:
            if item["variantId"] == selected.get(item["role"]) and not item.get("unavailable"):
                selected_attachments.append(dict(hostWeaponId=identity, role=item["role"], label=item["label"],
                                                 nativeSettingId=item.get("nativeSettingId"),
                                                 unitId=item.get("exportUnitId")))
    impacts = core.shared_impact(output_units)
    animated = {}
    for part in changed:
        for layer in baker.layers_for(part, project):
            source = layer.get("coverageSources", {}).get(part["instanceId"])
            if source and source["unitId"] != part["unitId"]:
                issues.append(dict(code="ammo_surface_mapping", layerId=layer["layerId"], message="此弹药设计的选面来自另一原生模板，请复核并重新绑定当前选面"))
            emission = layer.get("emission", {})
            if not emission.get("enabled") and layer["kind"] != "emission":
                continue
            mode = emission.get("mode")
            if mode in motion.ANIMATED:
                animated[layer["layerId"]] = mode
    if animated:
        folder = core.pack_for(project) / "native/material"
        if not all((folder / f"{motion.SURFACE_PARENT:016x}.material.{part}").is_file() for part in ("main", "gpu")):
            issues.append(dict(code="native_animation_asset", message="此工程的资产包缺少可用的原生动画发光材质"))
    signature = hashlib.sha256(core.json_bytes(dict(project=project, scope=scope))).hexdigest()
    return dict(scope=scope, projectRevision=project["revision"], signature=signature,
                parts=[{key: part[key] for key in ("instanceId", "unitId", "exportUnitId", "label", "role", "variantId")} for part in changed],
                weapons=[dict(weaponId=identity, label=weapons[identity]["label"]) for identity in assembly["linkedWeaponIds"]],
                outputDirectory=str(core.EXPORT_ROOT),
                nativeUnits=sorted(output_units), removedAttachments=removed, sharedImpact=impacts, ejectedCasings=casing_effects,
                selectedAttachments=selected_attachments,
                issues=issues, canExport=bool(changed) and not issues, animatedLayers=animated, gameValidated=False,
                previewSource="离线工作台预览", assetLocks=project["assetLocks"])


def package_name(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("请输入安装包名称")
    name = value.strip()
    if name.lower().endswith(".zip"):
        name = name[:-4]
    return core.safe_name(name)


def patch_material(raw, bindings, fields):
    raw = bytearray(raw)
    nt, nv = struct.unpack_from("<I", raw, 64)[0], struct.unpack_from("<I", raw, 104)[0]
    for index in range(nt):
        key = struct.unpack_from("<I", raw, 136 + index * 4)[0]
        if key in bindings:
            struct.pack_into("<Q", raw, 136 + nt * 4 + index * 8, bindings[key])
    written = set()
    for index in range(nv):
        kind, count, key, offset, stride = struct.unpack_from("<5I", raw, 136 + nt * 12 + index * 20)
        if key in fields:
            values = fields[key]
            if len(values) != kind + 1:
                raise ValueError("原生材质参数尺寸不匹配")
            struct.pack_into("<" + "f" * len(values), raw, 136 + nt * 12 + nv * 20 + offset, *values)
            written.add(key)
    if written != set(fields):
        raise ValueError("原生材质缺少所需参数")
    return bytes(raw)


class NativeOutput:
    def __init__(self, root, project, progress, cancel):
        self.root, self.project = root, project
        self.pack = core.pack_for(project)
        self.progress, self.cancel = progress, cancel
        self.resources = {}
        self.profiles = []
        self.texture_reports = []
        self.unit_reports = []
        self.source_units = []
        self.hashes = []
        self.source_resources = {}
        self.surface_by_part = {}
        self.material_cache = {}
        self.texture_cache = {}
        self.motion_parents = {}
        self.motion_layers = set()

    def source_path(self, identity, kind):
        return self.pack / "native" / kind / f"{identity:016x}.{kind}.main"

    def source(self, identity, kind):
        key = (identity, kind)
        if key in self.source_resources:
            return self.source_resources[key]
        path = self.source_path(identity, kind)
        if not path.is_file():
            raise ValueError(f"资产包缺少原生{kind}依赖 {identity:016x}")
        result = []
        for suffix in ("main", "gpu", "stream"):
            source = path.with_suffix("." + suffix)
            exists = source.is_file()
            raw = source.read_bytes() if exists else b""
            result.append(raw)
            if exists:
                target = self.root / "source/extracted/Raw" / f"{identity:016x}.{kind}.{suffix}"
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    if target.read_bytes() != raw:
                        raise ValueError(f"已有的原生来源副本发生变化 {target.name}")
                else:
                    with target.open("xb") as stream:
                        stream.write(raw)
                self.hashes.append(dict(source=str(source), copy=str(target.relative_to(self.root)), sha256=hashlib.sha256(raw).hexdigest()))
        self.source_resources[key] = tuple(result)
        return self.source_resources[key]

    def add(self, identity, kind, main, gpu=b"", stream=b""):
        key = (identity, kind)
        resource = dict(id=identity, type=kind, main=bytes(main), gpu=bytes(gpu), stream=bytes(stream))
        if key in self.resources and self.resources[key] != resource:
            raise ValueError(f"同一原生资源包含不同内容 {identity:016x}")
        self.resources[key] = resource
        return identity

    def texture(self, pixels, template_id, label):
        self.cancel()
        solid = pixels if pixels.ndim == 1 else pixels[0, 0] if pixels.strides[:2] == (0, 0) else None
        memory_key = (template_id, pixels.shape, array_digest(pixels if solid is None else solid))
        if memory_key in self.texture_cache:
            return self.texture_cache[memory_key]
        main, original_gpu, original_stream = self.source(template_id, "texture")
        if len(main) != 340 or main[192:196] != b"DDS ":
            raise ValueError("纹理模板头部无效")
        height, width = struct.unpack_from("<II", main, 204)
        count = struct.unpack_from("<I", main, 220)[0]
        fmt = struct.unpack_from("<I", main, 320)[0]
        streamed_mips = struct.unpack_from("<I", main, 8)[0] if original_stream else 0
        resident_mips = count - streamed_mips
        dims = (width, height) if solid is not None else tuple(
            min(4096, 1 << (length - 1).bit_length()) for length in (pixels.shape[1], pixels.shape[0]))
        if dims != (width, height):
            width, height = dims
            count = max(dims).bit_length()
            if count < resident_mips:
                raise ValueError("无法保留原生常驻 MIP 的数量")
            streamed_mips = count - resident_mips if original_stream else 0
            layout = mip_layout(width, height, count, fmt)
            header = bytearray(main)
            struct.pack_into("<II", header, 204, height, width)
            struct.pack_into("<I", header, 212, layout[0][2])
            struct.pack_into("<I", header, 220, count)
            header[12:192] = bytes(180)
            offset, remaining = 0, sum(length for _, _, length in layout)
            for mip, (w, h, length) in enumerate(layout):
                struct.pack_into("<IIHH", header, 12 + mip * 12, offset, remaining, w, h)
                offset += length
                remaining -= length
            if original_stream:
                struct.pack_into("<I", header, 8, streamed_mips)
            main = bytes(header)
        else:
            layout = mip_layout(width, height, count, fmt)
        if solid is not None:
            if len(solid) == 3:
                solid = np.append(solid, 1.)
            pixels = np.clip(solid.astype(np.float32), 0, 1)
        else:
            if pixels.shape[2] == 3:
                pixels = np.concatenate((pixels, np.ones((*pixels.shape[:2], 1))), axis=2)
            source = pixels.astype(np.float32, copy=False)
            if source.shape[:2] == (height, width):
                pixels = np.clip(source, 0, 1)
            else:
                resized = list(worker_pool().map(lambda channel: np.asarray(
                    Image.fromarray(source[:, :, channel]).resize((width, height), Image.Resampling.BILINEAR)), range(4)))
                pixels = np.clip(np.stack(resized, axis=2), 0, 1)
        digest = hashlib.sha256(main)
        digest.update(memoryview(np.ascontiguousarray(pixels, dtype="<f4")))
        digest.update(b"weapon-encoder-1" if solid is None else b"weapon-encoder-1-solid")
        cache_key = digest.hexdigest()
        cache = core.DATA / "cache/dds" / (cache_key + ".dds")
        meta = cache.with_suffix(".json")
        cached = False
        dds = None
        if cache.is_file() and meta.is_file():
            try:
                candidate = cache.read_bytes()
                if hashlib.sha256(candidate).hexdigest() == json.loads(meta.read_text(encoding="utf-8")).get("sha256"):
                    dds = candidate
                    cached = True
            except (OSError, ValueError, TypeError):
                cached = False
        if not cached:
            self.cancel()
            if solid is not None:
                block = encode_mip(np.broadcast_to(pixels, (4, 4, 4)), fmt)
                buffers = [block * (length // len(block)) for _, _, length in layout]
            else:
                pool = worker_pool()
                planes = channel_planes(pixels)
                mips = [pixels] + list(pool.map(lambda level: build_mip(planes, level[:2]), layout[1:]))
                prepared = [prepare_mip(values, fmt) for values in mips]
                tasks = []
                for mip in range(count):
                    total = block_total(mips[mip])
                    split = max(1, min(WORKERS, -(-total // CHUNK_BLOCKS)))
                    step = -(-total // split)
                    for start in range(0, total, step):
                        tasks.append((mip, start, min(start + step, total)))
                parts = list(pool.map(lambda task: pack_range(prepared[task[0]], fmt, task[1], task[2]), tasks))
                joined = [[] for _ in range(count)]
                for task, part in zip(tasks, parts):
                    joined[task[0]].append(part)
                buffers = [b"".join(chunks) for chunks in joined]
            dds = main[192:340] + b"".join(buffers)
            core.atomic_write(cache, dds)
            core.atomic_write(meta, core.json_bytes(dict(sha256=hashlib.sha256(dds).hexdigest())))
        self.cancel()
        if 148 + sum(length for _, _, length in layout) != len(dds):
            raise ValueError("DDS MIP 长度错误")
        dds_hash = hashlib.sha256(dds).hexdigest()
        identity = hash64("content/mods/weaponpaint/texture/" + dds_hash)
        body = dds[148:]
        resident_bytes = sum(length for _, _, length in layout[streamed_mips:])
        gpu = (body[-resident_bytes:] if original_stream else body) if original_gpu else b""
        stream = body if original_stream else b""
        if not stream and len(gpu) != len(body):
            raise ValueError("纹理模板的常驻 MIP 长度不匹配")
        self.add(identity, TEXTURE, main, gpu, stream)
        self.texture_reports.append(dict(id=f"0x{identity:016x}", label=label, mipCount=count, format=fmt,
                                         width=width, height=height, decodedAllMips=False, cacheHit=cached, sha256=dds_hash))
        self.texture_cache[memory_key] = identity
        return identity

    def original_texture(self, identity):
        if (identity, TEXTURE) in self.resources:
            return
        main, gpu, stream = self.source(identity, "texture")
        self.add(identity, TEXTURE, main, gpu, stream)

    def original_material(self, identity):
        if not identity or (identity, MATERIAL) in self.resources:
            return
        main, gpu, stream = self.source(identity, "material")
        self.add(identity, MATERIAL, main, gpu, stream)
        parent = struct.unpack_from("<Q", main, 24)[0]
        self.original_material(parent)
        count = struct.unpack_from("<I", main, 64)[0]
        for texture in struct.unpack_from("<" + "Q" * count, main, 136 + count * 4):
            if texture:
                self.original_texture(texture)

    def empty_unit(self, output_id, template_id):
        main, gpu, stream = self.source(int(template_id, 16), "unit")
        parsed = native.parse(main, gpu)
        if parsed["meshes"] or gpu:
            raise ValueError("空配件模板包含可绘制几何")
        self.add(int(output_id, 16), UNIT, main, gpu, stream)
        self.source_units.append(dict(id=output_id, role="removed_attachment", path=template_id[2:]))
        self.unit_reports.append(dict(unitId=output_id, sourceUnitId=template_id, emptyAttachment=True,
                                      originalGpuAbsent=True, originalHeaderPreserved=True, meshes=[]))

    def material(self, surface, source_material, emission):
        base = self.texture(surface["base"], 0xad342ea1a6c63069, "base_color_linear")
        normal = self.texture(np.array([.5, .5, 1., 1.]), 0x3b0e8a63c4d525ee, "clean_normal")
        mra = self.texture(surface["parameters"], 0x151f34f6d35f3ee6, "material_data")
        bindings = {thin("albedo"): base, thin("normal"): normal, thin("mra"): mra}
        emission_texture = None
        animated = surface.get("animated")
        animated_parent = None
        if animated or surface.get("texturedEmission", False):
            template, _, _ = self.source(motion.EMISSION_TEMPLATE, "material")
            parent = struct.unpack_from("<Q", template, 24)[0]
            self.original_material(parent)
            if animated:
                atlas, strength, recipe = motion.atlas_for(surface)
                animated_parent = self.animated_parent(parent, recipe)
                template = bytearray(template)
                struct.pack_into("<Q", template, 24, animated_parent)
                emission_texture = self.texture(atlas, 0x151f34f6d35f3ee6, "animated_emission_rgba")
                self.motion_layers.update(identity for identity, _, _ in animated)
            else:
                strength = float(surface["emission"].max())
                pixels = surface["emission"] / strength
                emission_texture = self.texture(pixels, 0xad342ea1a6c63069, "emission_linear")
            bindings[thin("emissive_color")] = emission_texture
            fields = {thin("emissive_power"): [strength], thin("basecoloremissive_intensity"): [1.],
                      thin("emissive_color_contrast"): [1.], thin("emissive_color_power"): [1.],
                      thin("resolution_setting"): [3.]}
        else:
            template, _, _ = self.source(0x84a638f2e5acf1a6, "material")
            strength = float(np.max(emission))
            tint = (np.asarray(emission) / max(strength, 1e-10)).tolist()
            fields = {thin("emissive_color"): tint, thin("emissive_intensity"): [strength],
                      thin("emissive_enabled"): [float(strength > 0)], thin("emissive_amount"): [float(strength > 0)]}
        raw = patch_material(template, bindings, fields)
        identity = hash64("content/mods/weaponpaint/material/" + hashlib.sha256(raw).hexdigest())
        self.add(identity, MATERIAL, raw)
        if identity not in self.material_cache:
            self.profiles.append(dict(id=f"0x{identity:016x}", fields={str(key): values for key, values in fields.items()},
                                      surfaceRole=surface["surfaceRole"], sourceMaterial=f"0x{source_material:016x}",
                                      emissionMode="texture" if emission_texture else "uniform", emissionScale=strength,
                                      emissionTexture=f"0x{emission_texture:016x}" if emission_texture else None,
                                      animatedParent=f"0x{animated_parent:016x}" if animated_parent else None,
                                      animatedLayers=[row[0] for row in animated] if animated else [],
                                      surfaceKey=surface.get("key")))
            self.material_cache[identity] = raw
        return identity

    def animated_parent(self, parent, recipe):
        key = parent, recipe
        if key not in self.motion_parents:
            original, gpu, stream = self.source(parent, "material")
            if stream or parent != motion.SURFACE_PARENT:
                raise ValueError("原生动画父材质与内置适配不符")
            identity, main, code, profile = motion.specialize_parent(original, gpu, recipe)
            self.add(identity, MATERIAL, main, code)
            self.profiles.append(dict(id=f"0x{identity:016x}", fields={}, shaderProfile=profile,
                                      shaderSha256=hashlib.sha256(code).hexdigest()))
            self.motion_parents[key] = identity
        return self.motion_parents[key]

    def reproject_lods(self, parsed, skeleton, raw_coverage, parts, layers, assignments):
        """Bake projected light against the geometry and UVs of each drawn LOD."""
        active = {role: part for role, part in parts.items() if role != "ammo" and
                  any(baker.has_stamps(layer) or layer.get("imageHash") for layer in layers.get(role, ())) and
                  any(layer["kind"] == "emission" or layer.get("emission", {}).get("enabled") for layer in layers.get(role, ())) and
                  all(layer.get("coverageMode") != "quads" for layer in layers.get(role, ()))}
        geometry = defaultdict(list)
        for (mesh_id, group_id), coverage in raw_coverage.items():
            roles = set(coverage["roles"]) & active.keys()
            selected = {role: [i for i, value in enumerate(coverage["roles"])
                               if value == role and (mesh_id, group_id, i) not in assignments] for role in roles}
            if not any(selected.values()):
                continue
            mesh = parsed["meshes"][mesh_id]
            positions, uvs, _ = read_group(parsed, mesh, group_id, skeleton, skin_names=False)
            for role, indices in selected.items():
                if indices:
                    refs = np.column_stack((np.full(len(indices), mesh_id), np.full(len(indices), group_id), indices))
                    geometry[(mesh_id, role)].append((positions[indices], uvs[indices], refs, coverage["material"]))
        for (mesh_id, role), rows in geometry.items():
            self.cancel()
            part = active[role]
            positions, uvs, refs = (np.concatenate([row[axis] for row in rows]) for axis in range(3))
            original_materials = {group["material"]: group.get("previewMaterial", group["material"])
                                  for group in part.get("materialGroups", ())}
            groups, start = [], 0
            for points, _, _, material in rows:
                count = len(points) * 3
                groups.append(dict(start=start, count=count, material=material,
                                   previewMaterial=original_materials.get(material, material)))
                start += count
            fitted = dict(part, materialGroups=groups)
            baked = baker.surfaces(fitted, self.project, 512, self.cancel, native_motion=True,
                                   geometry=(positions, uvs, refs))
            for ref in refs:
                assignments[tuple(map(int, ref))] = None
            for surface in baked:
                pixels = surface["emission"]
                surface["texturedEmission"] = pixels.strides[:2] != (0, 0) and bool(np.any(np.ptp(pixels, axis=(0, 1)) > 1e-6))
                surface["key"] += f"/lod-{mesh_id}"
                ref = refs[surface["triangles"][0]]
                material = int(raw_coverage[(int(ref[0]), int(ref[1]))]["material"], 16)
                uniform = pixels[0, 0] if not surface["texturedEmission"] else np.zeros(3)
                identity = self.material(surface, material, uniform)
                for triangle in surface["triangles"]:
                    assignments[tuple(map(int, refs[triangle]))] = identity
        return len(geometry)

    def build_unit(self, unit_id, parts, output_id=None):
        self.cancel()
        original_main, original_gpu, original_stream = self.source(int(unit_id, 16), "unit")
        parsed = native.parse(original_main, original_gpu)
        descriptor = core.model(unit_id, self.project)
        if (hashlib.sha256(original_main).hexdigest() != descriptor["sourceHashes"]["main"] or
                original_gpu and hashlib.sha256(original_gpu).hexdigest() != descriptor["sourceHashes"]["gpu"]):
            raise ValueError("原生资源与资产版本哈希不匹配")
        if parsed["geometry"]:
            raise ValueError("共享 geometry_group 需要独立适配")
        optical_materials = sorted({int(row["material"], 16) for row in descriptor["coverage"] if "lens" in row["roles"]})
        for material in optical_materials:
            self.original_material(material)
        unique = {}
        for part in parts:
            previous = unique.get(part["componentId"])
            if previous and previous["instanceId"] != part["instanceId"]:
                signatures = []
                for candidate in (previous, part):
                    signature = hashlib.sha256()
                    for surface in baker.surfaces(candidate, self.project, 512, self.cancel, native_motion=True):
                        for channel in ("base", "emission", "parameters"):
                            signature.update(surface[channel].tobytes())
                        for _, settings, pixels in surface.get("animated", ()):
                            signature.update(core.json_bytes(settings))
                            signature.update(pixels.tobytes())
                    signatures.append(signature.hexdigest())
                if signatures[0] != signatures[1]:
                    raise ValueError("同一原生 Unit 的部件实例具有不同涂装，请分别导出当前装配。冲突实例为 " + previous["instanceId"] + " 与 " + part["instanceId"])
            unique[part["componentId"]] = part
        unit_role = "ejected_casing" if any(part.get("ejectedCasing") for part in parts) else "weapon"
        native_coverage = {(row["mesh"], row["group"]): row for row in descriptor["coverage"]}
        high_rows = []
        high_refs = {}
        effective_layers = {}
        solid_materials = {}
        no_emission = np.zeros(3)
        for part in unique.values():
            layers = baker.layers_for(part, self.project)
            if not layers:
                continue
            effective_layers[part["componentId"]] = layers
            self.progress("baking", "烘焙 " + part["label"], None)
            fills = all(layer["kind"] in ("fill", "brush") and layer.get("coverageMode") != "quads" and
                        not layer.get("imageHash") and not layer.get("emission", {}).get("enabled") for layer in layers)
            if fills and any(layer.get("opacity", 1) == 1 for layer in layers):
                surface = next(baker.surfaces(part, self.project, 512, self.cancel))
                mesh, group, _ = surface["refs"][0]
                source_material = int(native_coverage[(mesh, group)]["material"], 16)
                solid_materials[part["componentId"]] = self.material(surface, source_material, no_emission)
                continue
            positions, uvs, refs = baker.part_arrays(part, self.project)
            matrix = np.asarray(part["assembledTransform"])
            local_centers = positions.mean(1)
            uv_centers = uvs.mean(1)
            assembled_centers = local_centers @ matrix[:3, :3].T + matrix[:3, 3]
            world_normals = np.cross(positions[:, 1] - positions[:, 0], positions[:, 2] - positions[:, 0]) @ np.linalg.inv(matrix[:3, :3])
            world_normals /= np.maximum(np.linalg.norm(world_normals, axis=1, keepdims=True), 1e-20)
            masks = baker.paint_masks(part, positions, layers)
            full_wrap = any(layer.get("imageHash") and layer["projection"].get("wrap", True) for layer in layers)
            affected = np.logical_or.reduce(list(masks.values())) if masks else np.zeros(len(positions),dtype=bool)
            if not np.any(affected):
                continue
            for triangle in np.flatnonzero(~affected):
                high_refs[tuple(int(value) for value in refs[triangle])] = None
                high_rows.append(dict(center=local_centers[triangle], uv=uv_centers[triangle],
                                      role=part["componentId"], material=None, normal=world_normals[triangle], layer_ids=frozenset()))
            baked = baker.surfaces(part, self.project, 512, self.cancel, masks=masks, native_motion=True)
            images = baker.source_images(layers)
            emissive = any(layer["kind"] == "emission" or layer.get("emission", {}).get("enabled") for layer in layers)
            brush_data = {layer["layerId"]: baker.prepare_brush(layer, part) for layer in layers if baker.has_stamps(layer)}
            motion_slots = {layer["layerId"]: i for i, layer in enumerate(filter(baker.is_animated, layers), 1)}
            for surface in baked:
                emission_pixels = surface["emission"]
                surface["texturedEmission"] = emissive and emission_pixels.strides[:2] != (0, 0) and bool(
                    np.any(np.ptp(emission_pixels, axis=(0, 1)) > 1e-6))
                material_ids = {}
                surface_layer_ids=frozenset(layer["layerId"] for layer in layers if np.any(masks[layer["layerId"]][surface["triangles"]]))
                for triangle in surface["triangles"]:
                    normal = world_normals[triangle]
                    if surface.get("animated") or surface["texturedEmission"]:
                        emission_key = ("texture",)
                        emission = no_emission
                    elif emissive:
                        _, evaluated, _ = baker.evaluate(assembled_centers[triangle:triangle + 1], normal[None],
                                                        np.array([[1 / 3] * 3]), triangle, part, layers, images,
                                                        animated_slots=motion_slots, prepared_brushes=brush_data)
                        emission = evaluated[0, 0] if motion_slots else evaluated[0]
                        emission_key = tuple(np.round(emission, 6))
                    else:
                        emission_key = ()
                        emission = no_emission
                    if emission_key not in material_ids:
                        ref = refs[triangle]
                        source_material = native_coverage[(int(ref[0]), int(ref[1]))]["material"]
                        material_ids[emission_key] = self.material(surface, int(source_material, 16), emission)
                    material_id = material_ids[emission_key]
                    high_refs[tuple(int(value) for value in refs[triangle])] = material_id
                    triangle_layer_ids = surface_layer_ids
                    if full_wrap:
                        # A baked UV partition can contain both side-facing and front-facing triangles.
                        triangle_layer_ids = frozenset(layer["layerId"] for layer in layers if masks[layer["layerId"]][triangle])
                    high_rows.append(dict(center=local_centers[triangle], uv=uv_centers[triangle], role=part["componentId"],
                                          material=material_id, normal=normal, layer_ids=triangle_layer_ids))
        if not high_rows and not solid_materials:
            output_id = output_id or unit_id
            self.add(int(output_id, 16), UNIT, original_main, original_gpu, original_stream)
            self.source_units.append(dict(id=output_id, role=unit_role, path=unit_id[2:]))
            self.unit_reports.append(dict(unitId=output_id, sourceUnitId=unit_id, originalGpuPreserved=True,
                                          originalHeaderPreserved=True, originalTrianglesExactlyOnce=True,
                                          originalUVsPreserved=True, unchangedMaterialsPreserved=True, meshes=[],
                                          protectedOpticalMaterials=[f"0x{value:016x}" for value in optical_materials]))
            return
        bone_list = bones(original_main, {})
        reprojected = self.reproject_lods(parsed, bone_list, native_coverage, unique, effective_layers, high_refs)
        centers = np.asarray([row["center"] for row in high_rows])
        high_uvs = np.asarray([row["uv"] for row in high_rows])
        high_roles = np.asarray([row["role"] for row in high_rows])
        high_indices = {role: np.flatnonzero(high_roles == role) for role in unique}
        high_normals = np.asarray([row["normal"] for row in high_rows])
        layer_signatures=list(dict.fromkeys(row["layer_ids"] for row in high_rows))
        signature_indices={signature:index for index,signature in enumerate(layer_signatures)}
        high_signatures=np.asarray([signature_indices[row["layer_ids"]] for row in high_rows])
        facing_rules = {}
        for role,part in unique.items():
            normal_matrix = np.linalg.inv(np.asarray(part["assembledTransform"])[:3,:3])
            rules=[]
            for layer in effective_layers.get(role, ()):
                directions,cutoffs = [],[]
                whole_surface = False
                if layer.get("imageHash"):
                    projection = layer["projection"]
                    if projection.get("type")=="cylinder" or projection.get("wrap", True):
                        whole_surface = True
                    else:
                        directions.append(projection["normal"]); cutoffs.append(projection.get("cutoff",.001))
                elif layer["kind"]=="stroke":
                    for stamp in layer["brush"]["stamps"]:
                        if stamp["partId"]==part["instanceId"] and stamp["unitId"]==part["unitId"]:
                            normal = np.asarray(stamp["normal"])@normal_matrix
                            normal /= max(float(np.linalg.norm(normal)),1e-20)
                            directions.append(normal); cutoffs.append(.05)
                else:
                    whole_surface = True
                rules.append((layer["layerId"],whole_surface,np.asarray(directions).reshape(-1,3),np.asarray(cutoffs)))
            facing_rules[role]=(normal_matrix,rules)
        main, gpu = bytearray(original_main), bytearray(original_gpu)
        layouts = copy.deepcopy(parsed["layouts"])
        meshes = copy.deepcopy(parsed["meshes"])
        skeletons = copy.deepcopy(parsed["skeletons"])
        binding_map = dict(parsed["materials"])
        slot_cache = {}
        proof = []
        for mesh in meshes:
            if mesh["layout"] < 0 or not mesh["groups"]:
                continue
            layout = layouts[mesh["layout"]]
            old_skeleton = parsed["skeletons"][mesh["skeleton"]] if mesh["skeleton"] >= 0 else None
            new_groups, new_slots, new_maps, new_indices = [], [], [], []
            modified = False
            for group_index, group in enumerate(mesh["groups"]):
                source_slot = mesh["slots"][group[0]]
                coverage = native_coverage.get((mesh["index"], group_index))
                index_bytes = 2 if layout["indexType"] == 0 else 4
                triangles = np.frombuffer(original_gpu, dtype="<u2" if index_bytes == 2 else "<u4",
                                           count=group[4], offset=layout["iOff"] + group[3] * index_bytes).reshape(-1, 3)
                partitions = defaultdict(list)
                if coverage and any(len(high_indices[role]) for role in set(coverage["roles"]) & high_indices.keys()):
                    positions, uvs, _ = read_group(parsed, parsed["meshes"][mesh["index"]], group_index, bone_list)
                for index in range(len(triangles)):
                    if index % 256 == 0:
                        self.cancel()
                    reference = (mesh["index"], group_index, index)
                    identity = high_refs.get(reference)
                    role = coverage["roles"][index] if coverage else None
                    if role in solid_materials:
                        identity = solid_materials[role]
                    elif reference not in high_refs and role in high_indices and len(high_indices[role]):
                        eligible = high_indices[role]
                        if role != "ammo":
                            normal_matrix,rules = facing_rules[role]
                            normal = np.cross(positions[index,1]-positions[index,0],positions[index,2]-positions[index,0])@normal_matrix
                            normal /= max(float(np.linalg.norm(normal)),1e-20)
                            eligible = eligible[high_normals[eligible]@normal > .1]
                            allowed_layers={identity for identity,whole_surface,directions,cutoffs in rules
                                            if whole_surface or bool(np.any(directions@normal > cutoffs))}
                            allowed_signatures=np.asarray([signature.issubset(allowed_layers) for signature in layer_signatures])
                            eligible=eligible[allowed_signatures[high_signatures[eligible]]]
                        distance = np.linalg.norm((high_uvs[eligible] - uvs[index].mean(0)) if role == "ammo" else (centers[eligible] - positions[index].mean(0)), axis=1)
                        if len(eligible):
                            identity = high_rows[int(eligible[int(np.argmin(distance))])]["material"]
                    slot = source_slot
                    if identity is not None:
                        if identity not in slot_cache:
                            candidate = thin("weaponpaint_slot_" + f"{identity:016x}")
                            while candidate in binding_map and binding_map[candidate] != identity:
                                candidate = (candidate + 1) & 0xffffffff
                            slot_cache[identity] = candidate
                            binding_map[candidate] = identity
                        slot = slot_cache[identity]
                        modified = True
                    partitions[slot].append(index)
                for slot, selection in partitions.items():
                    if slot not in new_slots:
                        new_slots.append(slot)
                    remap_index = len(new_groups) if old_skeleton else group[5]
                    selected = triangles[selection].reshape(-1).tolist()
                    new_groups.append([new_slots.index(slot), group[1], group[2], len(new_indices), len(selected), remap_index])
                    new_indices.extend(selected)
                    if old_skeleton:
                        new_maps.append(old_skeleton["maps"][group[5]])
            if not modified:
                continue
            if len(new_indices) != sum(group[4] for group in mesh["groups"]):
                raise ValueError("原生三角形覆盖发生变化")
            native.pad(gpu, 64)
            index_offset = len(gpu)
            encoded = np.asarray(new_indices, dtype="<u2" if layout["indexType"] == 0 else "<u4").tobytes()
            gpu.extend(encoded)
            new_layout = copy.deepcopy(layout)
            new_layout["raw"] = bytearray(layout["raw"])
            struct.pack_into("<I", new_layout["raw"], 392, len(new_indices))
            struct.pack_into("<II", new_layout["raw"], 424, index_offset, len(encoded))
            layouts.append(new_layout)
            mesh["layout"] = len(layouts) - 1
            mesh["slots"], mesh["groups"] = new_slots, new_groups
            if old_skeleton:
                new_skeleton = copy.deepcopy(old_skeleton)
                new_skeleton["maps"] = new_maps
                skeletons.append(new_skeleton)
                mesh["skeleton"] = len(skeletons) - 1
            proof.append(dict(mesh=mesh["index"], triangles=len(new_indices) // 3, partitions=len(new_groups)))
        for offset, items in ((88, [native.skeleton_bytes(item) for item in skeletons]), (92, [item["raw"] for item in layouts])):
            native.pad(main)
            struct.pack_into("<I", main, offset, len(main))
            main.extend(native.table(items))
        native.pad(main)
        struct.pack_into("<I", main, 100, len(main))
        table = bytearray(struct.pack("<I", len(meshes)) + bytes(len(meshes) * 8))
        for index, (mesh, bone) in enumerate(zip(meshes, parsed["bones"])):
            struct.pack_into("<I", table, 4 + index * 4, len(table))
            struct.pack_into("<I", table, 4 + len(meshes) * 4 + index * 4, bone)
            table.extend(native.mesh_bytes(mesh))
        main.extend(table)
        native.pad(main)
        struct.pack_into("<I", main, 112, len(main))
        slots = sorted(binding_map)
        main.extend(struct.pack("<I", len(slots)) + struct.pack("<" + "I" * len(slots), *slots) +
                    struct.pack("<" + "Q" * len(slots), *(binding_map[slot] for slot in slots)))
        if bytes(gpu[:len(original_gpu)]) != original_gpu:
            raise ValueError("原始顶点与索引数据发生变化")
        allowed = set(range(88, 96)) | set(range(100, 104)) | set(range(112, 116))
        if any(a != b and index not in allowed for index, (a, b) in enumerate(zip(original_main, main))):
            raise ValueError("原始 Unit 的保护字段发生变化")
        output_id = output_id or unit_id
        self.add(int(output_id, 16), UNIT, main, gpu, original_stream)
        self.source_units.append(dict(id=output_id, role=unit_role, path=unit_id[2:]))
        self.unit_reports.append(dict(unitId=output_id, sourceUnitId=unit_id, originalGpuPreserved=True, originalHeaderPreserved=True,
                                      reprojectedLodParts=reprojected,
                                      originalTrianglesExactlyOnce=True, originalUVsPreserved=True, meshes=proof,
                                      protectedOpticalMaterials=[f"0x{value:016x}" for value in optical_materials]))

    def archive(self):
        resources = sorted(self.resources.values(), key=lambda item: (item["type"], item["id"]))
        types = sorted({item["type"] for item in resources})
        counts = Counter(item["type"] for item in resources)
        start = 72 + len(types) * 32
        pm, ps, pg, rm, rg = native.align(start + len(resources) * 80), 0, 0, 0, 0
        rows = []
        for index, item in enumerate(resources):
            pm, ps, pg = native.align(pm), native.align(ps, 64), native.align(pg, 64)
            row = [item["id"], item["type"], pm, ps, pg, rm, rg, len(item["main"]), len(item["stream"]), len(item["gpu"]), 16, 64, index + 1]
            rows.append(row)
            pm, ps, pg = pm + row[7], ps + row[8], pg + row[9]
            rm, rg = rm + native.align(row[7], 256), rg + native.align(row[9], 256)
        main = bytearray(native.align(pm))
        main[:72] = gzip.decompress((core.APP / "engine/boot_header.gz").read_bytes())
        struct.pack_into("<II", main, 4, len(types), len(resources))
        struct.pack_into("<QQ", main, 32, rm, rg)
        for index, kind in enumerate(types):
            block = bytearray(gzip.decompress((core.APP / "engine/texture_type.gz").read_bytes()))
            struct.pack_into("<Q", block, 8, kind)
            struct.pack_into("<I", block, 16, counts[kind])
            main[72 + index * 32:104 + index * 32] = block
        for index, (resource, row) in enumerate(zip(resources, rows)):
            struct.pack_into("<7Q6I", main, start + index * 80, *row)
            main[row[2]:row[2] + len(resource["main"])] = resource["main"]
        package = self.root / "package"
        package.mkdir(exist_ok=True)
        core.atomic_write(package / PATCH, main)
        for key, column, suffix in (("stream", 3, ".stream"), ("gpu", 4, ".gpu_resources")):
            with (package / (PATCH + suffix)).open("wb") as target:
                for resource, row in zip(resources, rows, strict=True):
                    self.cancel()
                    target.write(bytes(row[column] - target.tell()))
                    target.write(resource[key])
        report = validate_archive(main, (package / (PATCH + ".stream")).stat().st_size,
                                  (package / (PATCH + ".gpu_resources")).stat().st_size)
        core.atomic_write(self.root / "reports/archive_layout.json", core.json_bytes(report))
        return package


def mirror_reports(folder, identity):
    """Keep the small JSON reports and the worker log for later diagnosis; only bulky artifacts stay temporary."""
    if not isinstance(identity, str) or len(identity) != 32 or any(character not in "0123456789abcdef" for character in identity):
        return
    try:
        for path in sorted(Path(folder).glob("*.json")):
            core.atomic_write(core.DATA / "reports" / identity / path.name, path.read_bytes())
    except OSError:
        pass


def execute(request, root, progress, cancel):
    project = request["project"]
    plan = export_plan(project, request.get("scope", "current"))
    if not plan["canExport"]:
        raise ValueError("\n".join(issue["message"] for issue in plan["issues"]) or "请选择导出部件")
    root = Path(root)
    # root holds every intermediate file; the reserved version directory only receives the finished archive.
    destination = Path(request.get("destination") or root)
    version = destination.name
    reports = root / "reports"
    try:
        return build(request, root, destination, version, plan, progress, cancel)
    finally:
        mirror_reports(reports, request.get("jobId"))
        if destination != root:
            for folder in (destination, destination.parent):
                try:
                    # An aborted run leaves the reserved version directory and possibly an empty MOD folder.
                    folder.rmdir()
                except OSError:
                    break
            shutil.rmtree(root, ignore_errors=True)


def build(request, root, destination, version, plan, progress, cancel, *, output_factory=NativeOutput):
    project = request["project"]
    for directory in ("source", "reports", "work", "textures", "package"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    progress("inputs", "核对工程版本与图片素材", .05)
    for identity in core.project_image_hashes(project):
        cancel()
        if core.file_hash(core.image_path(identity)) != identity:
            raise ValueError("工程引用的图片哈希不一致，请重新导入该素材")
    core.atomic_write(root / "reports/export_plan.json", core.json_bytes(plan))
    preview = request.get("preview") or ""
    import base64
    if not preview or request.get("previewRevision") != project["revision"]:
        raise ValueError("请从当前工程重新生成装配预览")
    preview = base64.b64decode(preview.split(",")[-1], validate=True)
    with Image.open(io.BytesIO(preview)) as image:
        image.load()
        if image.format != "PNG" or image.width < 320 or image.height < 200:
            raise ValueError("请从当前工程重新生成装配预览")
    target_ids = {part["instanceId"] for part in plan["parts"]}
    native_output = output_factory(root, project, progress, cancel)
    for item in plan["removedAttachments"]:
        native_output.empty_unit(item["exportUnitId"], item["templateUnitId"])
    units = defaultdict(list)
    for part in core.assembly(project)["parts"]:
        if part["instanceId"] in target_ids:
            units[part["exportUnitId"]].append(part)
    for index, (output_id, parts) in enumerate(units.items()):
        cancel()
        sources = {part["unitId"] for part in parts}
        if len(sources) != 1:
            raise ValueError("关联装备的共享配件款式冲突，请为两者选择相同款式")
        unit_id = next(iter(sources))
        progress("baking", "处理原生 Unit " + unit_id, .08 + .67 * index / max(len(units), 1))
        native_output.build_unit(unit_id, parts, output_id)
    progress("native", "写入原生补丁", .79)
    native_output.archive()
    plan["exportedAnimations"] = [dict(layerId=identity, mode=mode) for identity, mode in plan["animatedLayers"].items()
                                  if identity in native_output.motion_layers]
    plan["uncoveredAnimations"] = sorted(set(plan["animatedLayers"]) - native_output.motion_layers)
    for name, data in (("source_units", native_output.source_units), ("material_profiles", native_output.profiles),
                       ("texture_encoding", native_output.texture_reports), ("unit_replacement", native_output.unit_reports),
                       ("source_manifest", native_output.hashes)):
        core.atomic_write(root / "reports" / (name + ".json"), core.json_bytes(data))
    native_output.resources.clear()
    cancel()
    progress("readback", "独立解析原生补丁与全部 MIP", .86)
    verifier = core.APP / "engine/verify_weapon.exe"
    process = subprocess.Popen([str(verifier), str(root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        while process.poll() is None:
            cancel()
            time.sleep(.1)
        stdout, stderr = process.communicate()
    except Exception:
        process.kill()
        process.communicate()
        raise
    if process.returncode:
        raise ValueError("原生独立回读失败 " + stderr.decode("utf-8", "replace")[-3000:])
    for item in native_output.texture_reports:
        item["decodedAllMips"] = True
    core.atomic_write(root / "reports/texture_encoding.json", core.json_bytes(native_output.texture_reports))
    cancel()
    progress("packaging", "校验预览、来源和安装包", .95)
    return package_verified_export(request, root, destination, version, plan, preview, native_output.hashes, cancel)


def package_verified_export(request, root, destination, version, plan, preview, source_hashes, cancel):
    project = request["project"]
    package = root / "package"
    patches = (PATCH, PATCH + ".stream", PATCH + ".gpu_resources")
    patch_hashes = {name: core.file_hash(package / name) for name in patches}
    core.atomic_write(package / "preview.png", preview)
    manifest = dict(format="HD2WeaponPaintMod", softwareVersion=core.VERSION, projectId=project["projectId"],
                    revision=project["revision"], assetLocks=project["assetLocks"], scope=plan["scope"],
                    nativeUnits=plan["nativeUnits"], sharedImpact=plan["sharedImpact"], ejectedCasings=plan["ejectedCasings"],
                    selectedAttachments=plan["selectedAttachments"], patchHashes=patch_hashes,
                    previewSource="离线工作台预览", validation="independent_native_readback", gameTested=False,
                    editableProjectIncluded=False)
    if plan.get("nativeTrial"):
        manifest["nativeTrial"] = plan["nativeTrial"]
    if plan["animatedLayers"]:
        manifest["lightingExport"] = dict(mode="native", gameplayClock="global_viewport.time", armoryPreview="lit",
                                          animationLayers=plan["exportedAnimations"], uncoveredLayers=plan["uncoveredAnimations"])
    core.atomic_write(package / "mod_manifest.json", core.json_bytes(manifest))
    instructions = "武器涂装工具生成的 MOD\n\n使用 MOD 管理器导入本压缩包。\n预览来源为离线工作台。\n验证已完成原生结构、几何与全部 MIP 回读。\n游戏实测状态见 mod_manifest.json。\n原生依赖及共享影响见 mod_manifest.json。\n"
    if plan["selectedAttachments"]:
        instructions += "在游戏中选用与工作台一致的配件。请查看 mod_manifest.json 中的 selectedAttachments。\n"
    if plan.get("nativeTrial"):
        instructions += "此包用于 SMG-32 训斥的后握把发光测试。对照包与呼吸包应轮流启用。预览展示亮灯时的部件与颜色。\n"
    if plan["exportedAnimations"]:
        instructions += "在任务中观察原生流光与呼吸灯。军械库和包内 PNG 展示亮灯外观。\n"
    core.atomic_write(package / "使用说明.txt", instructions.encode("utf-8-sig"))
    custom_name = package_name(request.get("packageName"))
    archive_name = (custom_name or core.safe_name(project["name"]) + "_" + version) + ".zip"
    pending = root / (archive_name + ".pending")
    members = (*patches, "preview.png", "mod_manifest.json", "使用说明.txt")
    with zipfile.ZipFile(pending, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as archive:
        for name in sorted(members):
            cancel()
            path = package / name
            info = zipfile.ZipInfo(name, core.ZIP_TIME)
            info.file_size = path.stat().st_size
            info.compress_type = zipfile.ZIP_DEFLATED
            info._compresslevel = 3
            info.external_attr = 0o644 << 16
            with archive.open(info, "w", force_zip64=info.file_size > zipfile.ZIP64_LIMIT) as target, path.open("rb") as source:
                for chunk in iter(lambda: source.read(1 << 22), b""):
                    cancel()
                    target.write(chunk)
    with zipfile.ZipFile(pending) as archive:
        if sorted(archive.namelist()) != sorted(members):
            raise ValueError("安装包文件清单不一致")
        with Image.open(io.BytesIO(archive.read("preview.png"))) as preview_image:
            preview_image.load()
            if preview_image.format != "PNG":
                raise ValueError("安装包预览格式不一致")
        for member in members:
            cancel()
            if member in patch_hashes:
                with archive.open(member) as handle:
                    if hashlib.file_digest(handle, "sha256").hexdigest() != patch_hashes[member]:
                        raise ValueError("打包后补丁哈希不一致")
            elif member != "preview.png":
                archive.read(member)
    for row in source_hashes:
        if core.file_hash(row["source"]) != row["sha256"] or core.file_hash(root / row["copy"]) != row["sha256"]:
            raise ValueError("制作来源哈希发生变化")
    cancel()
    final = destination / archive_name
    # The version directory is reserved exclusively before this worker starts.
    if final.exists():
        raise ValueError("最终安装包已存在")
    # Only the verified archive crosses into the version directory; every intermediate file dies with root.
    shutil.move(str(pending), str(final))
    report = dict(status="passed", zipPath=str(final), pngDecoded=True, patchHashesPreserved=True,
                  projectIncluded=False, sourceHashesUnchanged=True, gameTested=False,
                  patchBytes=sum((package / member).stat().st_size for member in patches), zipBytes=final.stat().st_size)
    core.atomic_write(root / "reports/delivery_audit.json", core.json_bytes(report))
    return dict(path=str(final), relativePath=final.relative_to(core.INSTALL).as_posix(),
                revision=project["revision"], audit=report)
