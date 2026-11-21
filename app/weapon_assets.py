"""Native Unit geometry, semantic components and exact quad coverage."""
from pathlib import Path
from collections import Counter, defaultdict
import math
import re
import struct

from compute_runtime import np
import native_writer as native
from hashing import murmur64_hash

HIDDEN_MATERIALS = {0x75f87ad2ae08e9c2, 0x5990e5efbca8ae21, 0x84b327056baf1c6d}
DISPLAY_BASIS = np.array([[0, 1, 0], [-1, 0, 0], [0, 0, 1]], dtype=float)
LABELS = {
    "receiver": "枪身", "stock": "枪托", "pistol_grip": "后握把", "handguard": "护木",
    "rail_top": "上导轨", "rail_bottom": "下导轨", "barrel": "枪管", "bolt": "枪机",
    "bolt_handle": "拉机柄", "trigger": "扳机", "fireselector": "快慢机", "mag_release": "弹匣释放钮",
    "magazine": "弹匣", "ammo": "弹药", "optic": "瞄具外壳", "lens": "镜片与分划",
    "foregrip": "前握把", "muzzle": "枪口", "underbarrel": "下挂配件", "backpack": "背包",
    "shell": "外壳", "pin": "保险与拉环", "slide": "套筒", "cylinder": "转轮",
}
DIRECTIONS = {
    "stock": [-1, 0, -.08], "pistol_grip": [-.7, 0, -1], "handguard": [1, 0, .03],
    "rail_top": [-.03, 0, 1], "rail_bottom": [1, 0, -.4], "barrel": [1, 0, .62],
    "bolt": [.15, 0, 1], "bolt_handle": [.75, 0, 1], "trigger": [-.45, 0, -1],
    "fireselector": [-1, 0, -.25], "mag_release": [-.1, 0, -1], "magazine": [.35, 0, -1],
    "ammo": [.35, 0, -1], "optic": [-.55, 0, 1], "lens": [-.55, 0, 1],
    "foregrip": [.9, 0, -1], "muzzle": [1, 0, .1], "underbarrel": [.8, 0, -1],
    "backpack": [-1, 0, .7], "shell": [0, 0, 0], "pin": [.5, 0, 1],
    "slide": [0, 0, 1], "cylinder": [1, 0, .5],
}


def hash64(text):
    return murmur64_hash(text.encode("utf-8"))


def thin(text):
    return hash64(text) >> 32


def bones(main, names):
    offset = struct.unpack_from("<I", main, 52)[0]
    if not offset:
        return []
    count = struct.unpack_from("<I", main, offset)[0]
    matrices = np.frombuffer(main, dtype="<f4", count=count * 16,
                             offset=offset + 16 + count * 64).reshape(count, 4, 4).transpose(0, 2, 1)
    hashes = struct.unpack_from("<" + "I" * count, main, offset + 16 + count * 132)
    parents = [struct.unpack_from("<HH", main, offset + 16 + count * 128 + index * 4)[1] for index in range(count)]
    return [dict(name=names.get(value, f"{value:08x}"), matrix=matrix.copy(), parent=parent)
            for value, matrix, parent in zip(hashes, matrices, parents)]


def attribute_array(parsed, mesh, group_index, item, dtype, count):
    layout = parsed["layouts"][mesh["layout"]]
    group = mesh["groups"][group_index]
    return np.ndarray((group[2], count), dtype=dtype, buffer=parsed["gpu"],
                      offset=layout["vOff"] + group[1] * layout["stride"] + item["offset"],
                      strides=(layout["stride"], np.dtype(dtype).itemsize))


def read_group_normals(parsed, mesh, group_index, bone_list):
    layout = parsed["layouts"][mesh["layout"]]
    group = mesh["groups"][group_index]
    item = next(item for item in layout["items"] if item["type"] == 1)
    if item["format"] == 30:
        packed = attribute_array(parsed, mesh, group_index, item, "<u4", 1)[:, 0]
        x = (packed & 1023).astype(np.float64) * (2 / 1023) - 1
        y = ((packed >> 10) & 1023).astype(np.float64) * (2 / 1023) - 1
        z = 1 - np.abs(x) - np.abs(y)
        folded = z < 0
        original_x = x.copy()
        x[folded] = (1 - np.abs(y[folded])) * np.where(original_x[folded] >= 0, 1, -1)
        y[folded] = (1 - np.abs(original_x[folded])) * np.where(y[folded] >= 0, 1, -1)
        normals = np.stack((x, y, z), axis=1)
    elif item["format"] == 35:
        normals = attribute_array(parsed, mesh, group_index, item, "<f2", 4)[:, :3].astype(np.float64)
    elif item["format"] in (2, 3):
        normals = attribute_array(parsed, mesh, group_index, item, "<f4", 3).astype(np.float64)
    else:
        raise ValueError("此模型的原生法线编码需要适配")
    transform_index = struct.unpack_from("<I", mesh["raw"], 48)[0]
    transform = bone_list[transform_index]["matrix"][:3, :3] if transform_index < len(bone_list) else np.eye(3)
    normals = normals @ np.linalg.inv(transform)
    normals = normals @ DISPLAY_BASIS.T
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-20)
    step = 2 if layout["indexType"] == 0 else 4
    indices = np.frombuffer(parsed["gpu"], dtype="<u2" if step == 2 else "<u4", count=group[4],
                            offset=layout["iOff"] + group[3] * step).reshape(-1, 3)
    return normals[indices].astype(np.float32)


def is_render_mesh(mesh, bone_list):
    index = struct.unpack_from("<I", mesh["raw"], 48)[0]
    visited = set()
    while index < len(bone_list) and index not in visited:
        visited.add(index)
        name = bone_list[index]["name"].lower()
        if name in ("collision", "shadow_mesh") or name.startswith(("c_collision", "c_culling", "collision_")):
            return False
        index = bone_list[index].get("parent", index)
    return True


def is_ammunition_bone(name):
    name = name.lower()
    return bool(re.fullmatch(r"(?:bullet|round|cartridge|shell|missile|ammo|rocket|projectile)s?(?:_(?:\d+|left|right|l|r))?(?:_(?:scale|fired|spent))*", name)) or name in {
        "shells_drum", "feed_bullet", "bullet_feed", "feed_round", "round_feed", "feed_shell", "shell_feed", "ammo_casing"
    } or bool(re.fullmatch(r"drum_shell_\d+(?:_breach)?", name))


def component_role(default_role, bone, material_name, center, source_label=None):
    if is_ammunition_bone(bone):
        return "ammo"
    if "lens" in material_name or "reticle" in material_name:
        return "lens"
    if default_role in ("optic", "muzzle", "foregrip", "underbarrel", "backpack"):
        return default_role
    if default_role == "magazine":
        return "magazine"
    if source_label:
        if source_label == "rail":
            return "rail_top" if center[2] > .07 else "rail_bottom"
        if source_label == "bolt" and bone == "bolt_handle":
            return "bolt_handle"
        if source_label == "controls":
            return bone if bone in ("fireselector", "mag_release") else "fireselector"
        return source_label
    for role, aliases in (
        ("bolt_handle", ("bolt_handle", "charging_handle")), ("bolt", ("bolt", "breech")),
        ("trigger", ("trigger",)), ("fireselector", ("fireselector", "fire_selector")),
        ("mag_release", ("mag_release", "magazine_release")), ("slide", ("slide",)),
        ("cylinder", ("cylinder", "revolver")), ("pin", ("pin", "spoon", "ring", "fuse")),
    ):
        if any(bone == alias or bone.startswith(alias + "_") for alias in aliases):
            return role
    return default_role


def read_group(parsed, mesh, group_index, bone_list, *, skin_names=True):
    layout = parsed["layouts"][mesh["layout"]]
    group = mesh["groups"][group_index]
    item = next(item for item in layout["items"] if item["type"] == 0)
    if item["format"] != 2:
        raise ValueError("该资源的位置编码需要新增适配")
    positions = np.ndarray((group[2], 3), dtype="<f4", buffer=parsed["gpu"],
                           offset=layout["vOff"] + group[1] * layout["stride"] + item["offset"],
                           strides=(layout["stride"], 4)).copy()
    uv_item = next((item for item in layout["items"] if item["type"] == 4 and item["layer"] == 0), None)
    uvs = np.zeros((len(positions), 2), dtype=np.float32)
    if uv_item:
        if uv_item["format"] not in (1, 33):
            raise ValueError("该资源的 UV 编码需要新增适配")
        step = 2 if uv_item["format"] == 33 else 4
        uvs = np.ndarray((group[2], 2), dtype="<f2" if step == 2 else "<f4", buffer=parsed["gpu"],
                         offset=layout["vOff"] + group[1] * layout["stride"] + uv_item["offset"],
                         strides=(layout["stride"], step)).astype(np.float32)
    indices = np.frombuffer(parsed["gpu"], dtype="<u2" if layout["indexType"] == 0 else "<u4",
                            count=group[4], offset=layout["iOff"] + group[3] * (2 if layout["indexType"] == 0 else 4)).reshape(-1, 3)
    if indices.size and int(indices.max()) >= len(positions):
        raise ValueError("原生索引超出顶点范围")
    transform_index = struct.unpack_from("<I", mesh["raw"], 48)[0]
    transform = bone_list[transform_index]["matrix"] if transform_index < len(bone_list) else np.eye(4)
    positions = positions @ transform[:3, :3].T + transform[:3, 3]
    positions = positions @ DISPLAY_BASIS.T
    bone_names = [bone_list[transform_index]["name"] if transform_index < len(bone_list) else ""] * len(indices)
    item = next((item for item in layout["items"] if item["type"] == 6 and item["layer"] == 0), None)
    if skin_names and mesh["skeleton"] >= 0 and item:
        skeleton = parsed["skeletons"][mesh["skeleton"]]
        remap = np.frombuffer(skeleton["maps"][group[5]], dtype="<u4")
        joints = np.frombuffer(skeleton["indices"], dtype="<u4")
        local = np.ndarray((group[2], 4), dtype="u1", buffer=parsed["gpu"],
                           offset=layout["vOff"] + group[1] * layout["stride"] + item["offset"],
                           strides=(layout["stride"], 1))
        weights_item = next((value for value in layout["items"] if value["type"] == 7), None)
        weights = np.zeros((group[2], 4), dtype=float)
        weights[:, 0] = 1
        if weights_item:
            if weights_item["format"] == 35:
                weights = attribute_array(parsed, mesh, group_index, weights_item, "<f2", 4).astype(float)
            elif weights_item["format"] == 33:
                weights[:, :2] = attribute_array(parsed, mesh, group_index, weights_item, "<f2", 2)
            elif weights_item["format"] == 29:
                packed = attribute_array(parsed, mesh, group_index, weights_item, "<u4", 1)[:, 0]
                weights[:, :3] = np.stack([(packed >> shift) & 1023 for shift in (0, 10, 20)], axis=1) / 1023
            elif weights_item["format"] == 0:
                weights[:, 0] = attribute_array(parsed, mesh, group_index, weights_item, "<f4", 1)[:, 0]
        for triangle_index, triangle in enumerate(indices):
            contributions = defaultdict(float)
            for joint, weight in zip(local[triangle].reshape(-1), weights[triangle].reshape(-1)):
                contributions[int(joint)] += float(weight)
            joint = max(contributions, key=contributions.get)
            if joint < len(remap) and remap[joint] < len(joints) and joints[remap[joint]] < len(bone_list):
                bone_names[triangle_index] = bone_list[joints[remap[joint]]]["name"]
    return positions[indices].astype(np.float32), uvs[indices], bone_names


def make_quads(positions):
    """Pair convex neighbors and subdivide every unpaired valid triangle exactly."""
    if len(positions) == 0:
        return [], [], dict(triangles=0, quads=0, degenerate=0, coverageAreaError=0)
    rounded = np.rint(positions * 1000000).astype(np.int64)
    vertices, ids = np.unique(rounded.reshape(-1, 3), axis=0, return_inverse=True)
    ids = ids.reshape(-1, 3)
    cross = np.cross(positions[:, 1] - positions[:, 0], positions[:, 2] - positions[:, 0])
    lengths = np.linalg.norm(cross, axis=1)
    normals = cross / np.maximum(lengths[:, None], 1e-20)
    edges = defaultdict(list)
    for triangle, vertices in enumerate(ids):
        for a, b in ((0, 1), (1, 2), (2, 0)):
            edges[tuple(sorted((int(vertices[a]), int(vertices[b]))))].append(triangle)
    # Track a shared midpoint at a native T junction as two edge segments.
    vertex_points = np.unique(rounded.reshape(-1, 3), axis=0)
    point_ids = {tuple(point): index for index, point in enumerate(vertex_points)}
    topology_edges = {edge: list(owners) for edge, owners in edges.items()}
    for (a, b), owners in list(edges.items()):
        total = vertex_points[a] + vertex_points[b]
        if np.any(total % 2):
            continue
        midpoint = point_ids.get(tuple(total // 2))
        if midpoint is None or midpoint in (a, b):
            continue
        for first, second in ((a, midpoint), (midpoint, b)):
            key = tuple(sorted((first, second)))
            topology_edges.setdefault(key, []).extend(owners)
    neighbors = [[] for _ in positions]
    candidates = []
    for edge, owners in topology_edges.items():
        owners = list(dict.fromkeys(owners))
        if len(owners) != 2:
            continue
        a, b = owners
        angle = float(normals[a] @ normals[b])
        if angle < math.cos(math.radians(28)):
            continue
        neighbors[a].append(b)
        neighbors[b].append(a)
        union = set(ids[a]) | set(ids[b])
        if len(union) != 4 or min(lengths[a], lengths[b]) < 1e-12:
            continue
        shared = set(edge)
        outer_a = next(index for index, value in enumerate(ids[a]) if value not in shared)
        outer_b = next(index for index, value in enumerate(ids[b]) if value not in shared)
        shared_points = np.linalg.norm(positions[a, :, None] - positions[b, None, :], axis=2) < 1e-6
        if shared_points.sum() != 2 or shared_points[outer_a].any() or shared_points[:, outer_b].any():
            continue
        order = [positions[a, outer_a], positions[a, (outer_a + 1) % 3], positions[b, outer_b], positions[a, (outer_a + 2) % 3]]
        polygon = np.asarray(order)
        turns = np.cross(np.roll(polygon, -1, axis=0) - polygon, np.roll(polygon, -2, axis=0) - np.roll(polygon, -1, axis=0)) @ normals[a]
        if np.min(turns) <= 1e-13:
            continue
        sides = np.linalg.norm(np.roll(polygon, -1, axis=0) - polygon, axis=1)
        quality = angle * min(lengths[a], lengths[b]) / max(lengths[a], lengths[b]) * sides.min() / sides.max()
        candidates.append((quality, a, b, outer_a, outer_b))
    matching = {}
    choices = {}
    candidate_neighbors = defaultdict(list)
    for quality, a, b, outer_a, outer_b in sorted(candidates, reverse=True):
        choices[(a, b)] = [a, b, outer_a, outer_b]
        choices[(b, a)] = [a, b, outer_a, outer_b]
        candidate_neighbors[a].append(b)
        candidate_neighbors[b].append(a)
        if a in matching or b in matching:
            continue
        matching[a], matching[b] = b, a
    # A short augmenting path improves regional pairing after the quality pass.
    for a in range(len(positions)):
        if a in matching:
            continue
        augmented = False
        for b in candidate_neighbors[a]:
            c = matching.get(b)
            if c is None:
                matching[a], matching[b] = b, a
                break
            for d in candidate_neighbors[c]:
                if d != a and d not in matching:
                    matching[a], matching[b], matching[c], matching[d] = b, a, d, c
                    augmented = True
                    break
            if augmented:
                break
    claimed = set(matching)
    quads = [choices[(a, b)] for a, b in sorted(matching.items()) if a < b]
    degenerate = []
    for triangle in range(len(positions)):
        if triangle in claimed:
            continue
        if lengths[triangle] < 1e-12:
            degenerate.append(triangle)
            continue
        for corner in range(3):
            quads.append([triangle, -1, corner, 0])
    region_id = [-1] * len(positions)
    region_count = 0
    for start in range(len(positions)):
        if region_id[start] >= 0 or start in degenerate:
            continue
        queue = [start]
        region_id[start] = region_count
        while queue:
            current = queue.pop()
            for other in neighbors[current]:
                if region_id[other] < 0 and normals[start] @ normals[other] >= math.cos(math.radians(20)):
                    region_id[other] = region_count
                    queue.append(other)
        region_count += 1
    covered = np.zeros(len(positions))
    for a, b, _, _ in quads:
        covered[a] += 1 if b >= 0 else 1 / 3
        if b >= 0:
            covered[b] += 1
    valid = lengths >= 1e-12
    error = float(np.max(np.abs(covered[valid] - 1))) if np.any(valid) else 0
    return quads, region_id, dict(triangles=len(positions), quads=len(quads),
                                  regions=region_count, degenerate=len(degenerate),
                                  validFourCornerRate=1.0, coverageAreaError=error)


def quad_coverage(quad):
    triangle, other, corner, _ = quad
    if other >= 0:
        identity = [[1, 0, 0], [0, 1, 0], [0, 0, 1]]
        return [(triangle, identity), (other, identity)]
    a = np.eye(3)[corner]
    b = np.eye(3)[(corner + 1) % 3]
    c = np.eye(3)[(corner + 2) % 3]
    return [(triangle, np.asarray([a, (a + b) / 2, [1 / 3] * 3, (a + c) / 2]).tolist())]


def validate_quads(positions, quads):
    assigned = defaultdict(set)
    for quad in quads:
        if len(quad) != 4 or not all(isinstance(value, int) for value in quad):
            raise ValueError("四边面定义无效")
        a, b, corner_a, corner_b = quad
        if not 0 <= a < len(positions) or not -1 <= b < len(positions) or not 0 <= corner_a < 3 or not 0 <= corner_b < 3:
            raise ValueError("四边面引用超出原生表面")
        if b >= 0:
            if a == b or assigned[a] or assigned[b]:
                raise ValueError("四边面覆盖重叠")
            first, second = positions[a], positions[b]
            shared = np.linalg.norm(first[:, None] - second[None, :], axis=2) < 1e-6
            if shared.sum() != 2 or shared[corner_a].any() or shared[:, corner_b].any():
                raise ValueError("配对四边面缺少有效的共享边")
            assigned[a].add("paired")
            assigned[b].add("paired")
        else:
            if "paired" in assigned[a] or corner_a in assigned[a]:
                raise ValueError("细分四边面覆盖重叠")
            assigned[a].add(corner_a)
    areas = np.linalg.norm(np.cross(positions[:, 1] - positions[:, 0], positions[:, 2] - positions[:, 0]), axis=1)
    for triangle in np.flatnonzero(areas >= 1e-12):
        if assigned[int(triangle)] not in ({"paired"}, {0, 1, 2}):
            raise ValueError("四边面覆盖不完整")
    return True


def explosion_layout(parts, gap=None):
    """Place XZ bounds on fixed role rays while retaining Y, rotation and scale."""
    if not parts:
        return {}
    anchor = next((part for part in parts if part["role"] in ("receiver", "shell", "backpack")), parts[0])
    anchor_box = np.asarray(anchor["bounds"], dtype=float)
    span = float(np.max(anchor_box[1] - anchor_box[0]))
    gap = gap if gap is not None else max(.006, span * .055)
    results = {anchor["instanceId"]: [0., 0., 0.]}
    placed = [(anchor_box.mean(0), (anchor_box[1] - anchor_box[0]) / 2)]
    for part in parts:
        if part is anchor:
            continue
        bounds = np.asarray(part["bounds"], dtype=float)
        center, half = bounds.mean(0), (bounds[1] - bounds[0]) / 2
        direction = np.asarray(DIRECTIONS.get(part["role"], [.7, 0, .7]), dtype=float)
        if np.linalg.norm(direction) < 1e-10:
            direction = center - anchor_box.mean(0)
            direction[1] = 0
        if np.linalg.norm(direction) < 1e-10:
            direction = np.array([1, 0, 1.])
        direction /= np.linalg.norm(direction)
        distance = gap * 2
        intervals = []
        for other_center, other_half in placed:
            lower, upper = -math.inf, math.inf
            for axis in (0, 2):
                offset = center[axis] - other_center[axis]
                extent = half[axis] + other_half[axis] + gap
                velocity = direction[axis]
                if abs(velocity) < 1e-12:
                    if abs(offset) >= extent:
                        lower, upper = 1, 0
                        break
                else:
                    start, end = sorted(((-extent - offset) / velocity, (extent - offset) / velocity))
                    lower, upper = max(lower, start), min(upper, end)
            if lower < upper:
                intervals.append((lower, upper))
        for lower, upper in sorted(intervals):
            if lower <= distance <= upper:
                distance = upper + gap * .01
        delta = direction * distance
        delta[1] = 0
        results[part["instanceId"]] = delta.tolist()
        placed.append((center + delta, half))
    return results
