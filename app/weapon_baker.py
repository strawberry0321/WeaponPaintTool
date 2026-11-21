"""Shared workbench/export rasterization in original native UV coordinates."""
from collections import defaultdict
from functools import lru_cache
import copy
import hashlib
import io
import json
import math

from PIL import Image
from compute_runtime import np
import studio_core as core
from ammo_mapping import remap_layer


def linear(rgb):
    values = np.asarray(rgb, dtype=np.float32)
    return np.where(values <= .04045, values / 12.92, ((values + .055) / 1.055) ** 2.4)


def srgb(rgb):
    values = np.maximum(np.asarray(rgb), 0)
    return np.where(values <= .0031308, values * 12.92, 1.055 * values ** (1 / 2.4) - .055)


def color(value):
    value = value.lstrip("#")
    if len(value) != 6:
        raise ValueError("颜色应为六位十六进制")
    return linear(np.array([int(value[index:index + 2], 16) for index in (0, 2, 4)]) / 255)


def projection_coordinates(points, normals, layer):
    projection = layer.get("projection", {})
    origin = np.asarray(projection.get("origin", [0, 0, 0]))
    u = np.asarray(projection.get("u", [1, 0, 0]))
    v = np.asarray(projection.get("v", [0, 0, 1]))
    normal = np.asarray(projection.get("normal", [0, -1, 0]))
    local = points - origin
    x, y, z = local @ u, local @ v, local @ normal
    placement = layer.get("placement", {})
    cylinder = projection.get("type") == "cylinder"
    if cylinder:
        radius = max(float(projection.get("radius", .1)), .00001)
        radial_length = np.hypot(x, z)
        facing = ((normals @ u) * x + (normals @ normal) * z) > float(projection.get("cutoff", .001)) * radial_length
        x = radius * np.arctan2(x, z)
        z = radial_length - radius
    else:
        facing = normals @ normal > float(projection.get("cutoff", .001))
    x = x - float(placement.get("x", 0))
    if cylinder:
        x = (x + math.pi * radius) % (2 * math.pi * radius) - math.pi * radius
    y = y - float(placement.get("y", 0))
    angle = math.radians(float(placement.get("angle", 0)))
    scale = max(.00001, float(placement.get("scale", 1)))
    width = max(.00001, float(projection.get("width", .2))) * scale
    if cylinder:
        width = min(width, 2 * math.pi * radius)
    height = max(.00001, float(projection.get("height", .2))) * scale
    uv = np.stack(((math.cos(angle) * x + math.sin(angle) * y) / width + .5,
                   .5 - (-math.sin(angle) * x + math.cos(angle) * y) / height), axis=1)
    depth = max(.00001, float(projection.get("depth", .2)))
    inside = (z <= .00002) & (z >= -depth - .00002) if projection.get("depthMode") == "inward" and projection.get("type", "plane") == "plane" else np.abs(z) <= depth
    if cylinder:
        inside &= radial_length >= .000001
    if projection.get("wrap", True):
        facing = np.ones(len(points), dtype=bool)
    valid = facing & inside
    valid &= np.all((uv >= 0) & (uv <= 1), axis=1)
    crop = np.asarray(layer.get("crop", [0, 0, 1, 1]), dtype=float)
    uv = crop[:2] + uv * (crop[2:] - crop[:2])
    return uv, valid


def cylinder_intersects(points, layer):
    """Find potentially painted triangles on either side of the angular seam."""
    projection = layer["projection"]
    placement = layer.get("placement", {})
    local = points - np.asarray(projection["origin"])
    x, y, z = (local @ np.asarray(projection[axis]) for axis in ("u", "v", "normal"))
    radius, depth = projection["radius"], projection["depth"]
    period = 2 * math.pi * radius
    scale = placement.get("scale", 1)
    width = min(period, projection["width"] * scale)
    height = projection["height"] * scale
    angle = math.radians(placement.get("angle", 0))
    cosine, sine = math.cos(angle), math.sin(angle)
    arc = (radius * np.arctan2(x, z) - placement.get("x", 0) + period / 2) % period - period / 2
    arc = arc[:, :1] + (arc - arc[:, :1] + period / 2) % period - period / 2
    y -= placement.get("y", 0)
    hit = np.zeros(len(points), dtype=bool)
    for shift in (-period, 0, period):
        horizontal = cosine * (arc + shift) + sine * y
        vertical = -sine * (arc + shift) + cosine * y
        hit |= ((horizontal.min(1) <= width / 2) & (horizontal.max(1) >= -width / 2) &
                (vertical.min(1) <= height / 2) & (vertical.max(1) >= -height / 2))
    near_x = np.maximum(0, np.maximum(x.min(1), -x.max(1)))
    near_z = np.maximum(0, np.maximum(z.min(1), -z.max(1)))
    # A cap can cross the axis and span angles beyond its vertex extrema.
    half_height = (abs(cosine) * height + abs(sine) * width) / 2
    hit |= (near_x == 0) & (near_z == 0) & (y.min(1) <= half_height) & (y.max(1) >= -half_height)
    return (hit & (np.hypot(near_x, near_z) <= radius + depth) &
            (np.hypot(x, z).max(1) >= radius - depth))


def layer_applies(layer, part):
    if not layer.get("enabled", True):
        return False
    target = layer.get("target", {})
    if part["instanceId"] in target.get("excludedComponentInstanceIds", []):
        return False
    listed = target.get("componentInstanceIds", [])
    if part["role"] == "ammo":
        return (part["instanceId"] in listed or part["hostEntityId"] in (target.get("ammoHosts") or []) or
                (target.get("ammoHosts") is None and part.get("ammoSyncSourceId") in listed))
    if target.get("scope", "assembly") == "assembly":
        return part["hostEntityId"] in target.get("assemblyWeaponIds", [layer["weaponId"]])
    return part["instanceId"] in listed


def layers_for(part, project):
    layers = [copy.deepcopy(layer) for layer in core.paint_layers(project) if layer_applies(layer, part)]
    anchored = [layer for layer in layers if layer.get("imageHash") and layer.get("anchor", {}).get("assembledTransform")]
    ammo = part["role"] == "ammo"
    missing_sources = [layer for layer in layers if ammo and not layer.get("ammoSource") and
                       (layer.get("imageHash") or has_stamps(layer))]
    if anchored or missing_sources:
        assembly = core.assembly(project, part["hostEntityId"])
        components = {item["instanceId"]: item for item in assembly["parts"]}
        for layer in anchored:
            anchor = layer["anchor"]
            current = components.get(anchor["instanceId"])
            if current is None:
                continue
            delta = np.asarray(current["assembledTransform"]) @ np.linalg.inv(np.asarray(anchor["assembledTransform"]))
            projection = layer["projection"]
            projection["origin"] = (delta @ np.array([*projection["origin"], 1]))[:3].tolist()
            for axis in ("u", "v", "normal"):
                projection[axis] = (delta[:3, :3] @ np.asarray(projection[axis])).tolist()
            if layer.get("ammoSource", {}).get("instanceId") == current["instanceId"]:
                layer["ammoSource"]["assembledTransform"] = current["assembledTransform"]
        for layer in missing_sources:
            targets = [components.get(identity) for identity in layer.get("target", {}).get("componentInstanceIds", ())]
            sample = next((item for item in targets if item and item["role"] == "ammo"), None)
            sample = sample or components.get(part.get("ammoSyncSourceId"))
            if sample:
                layer["ammoSource"] = {key: sample[key] for key in ("unitId", "componentId", "instanceId", "assembledTransform")}
                layer["ammoSource"]["bounds"] = sample["localBounds"]
    if ammo:
        layers = [remap_layer(layer, part, {"bounds": part["localBounds"]}) for layer in layers]
    return [layer for layer in layers if not layer.get("imageHash") or projection_intersects(layer, part["bounds"])]


def projection_intersects(layer, bounds):
    minimum, maximum = bounds
    corners = np.array([[x, y, z] for x in (minimum[0], maximum[0]) for y in (minimum[1], maximum[1]) for z in (minimum[2], maximum[2])])
    projection = layer["projection"]
    if projection.get("type") == "cylinder":
        return True
    local = corners - np.asarray(projection["origin"])
    placement = layer.get("placement", {})
    x = local @ np.asarray(projection["u"]) - placement.get("x", 0)
    y = local @ np.asarray(projection["v"]) - placement.get("y", 0)
    angle = math.radians(placement.get("angle", 0))
    projected = np.stack((math.cos(angle) * x + math.sin(angle) * y,
                          -math.sin(angle) * x + math.cos(angle) * y,
                          local @ np.asarray(projection["normal"])), axis=1)
    scale = placement.get("scale", 1)
    limits = np.array([projection["width"] * scale / 2, projection["height"] * scale / 2, projection["depth"]])
    upper = limits.copy()
    if projection.get("depthMode") == "inward":
        upper[2] = .00002
    return bool(np.all(projected.max(0) >= -limits) and np.all(projected.min(0) <= upper))


def layer_coverage(layer, part, triangle, barycentric):
    coverage = layer.get("coverage", {})
    if layer.get("coverageMode") != "quads":
        return np.ones(len(barycentric), dtype=bool)
    selection = coverage.get(part["instanceId"], [])
    source = layer.get("coverageSources", {}).get(part["instanceId"])
    if source and source["unitId"] != part["unitId"]:
        return np.zeros(len(barycentric), dtype=bool)
    mask = np.zeros(len(barycentric), dtype=bool)
    for index in selection:
        if not 0 <= index < len(part["quads"]):
            continue
        quad = part["quads"][index]
        if quad[0] != triangle and quad[1] != triangle:
            continue
        if quad[1] >= 0:
            mask[:] = True
        else:
            # Q_A is exactly the barycentric Voronoi sector at vertex A.
            corner = quad[2]
            mask |= ((barycentric[:, corner] >= barycentric[:, (corner + 1) % 3] - 1e-7) &
                     (barycentric[:, corner] >= barycentric[:, (corner + 2) % 3] - 1e-7))
    return mask


def triangle_pixels(uv, size):
    """Return wrapped native UV pixel centers and original triangle barycentrics."""
    shifted = uv - np.floor(uv.mean(0))
    output = []
    for dx in range(math.floor(float(shifted[:, 0].min())), math.floor(float(shifted[:, 0].max())) + 1):
        for dy in range(math.floor(float(shifted[:, 1].min())), math.floor(float(shifted[:, 1].max())) + 1):
            points = (shifted - [dx, dy]) * size
            minimum = np.clip(np.floor(points.min(0)).astype(int), 0, size - 1)
            maximum = np.clip(np.ceil(points.max(0)).astype(int), 0, size - 1)
            a, b, c = points
            denominator = (b[1] - c[1]) * (a[0] - c[0]) + (c[0] - b[0]) * (a[1] - c[1])
            if abs(denominator) < 1e-12:
                continue
            yy, xx = np.mgrid[minimum[1]:maximum[1] + 1, minimum[0]:maximum[0] + 1]
            x, y = xx + .5, yy + .5
            first = ((b[1] - c[1]) * (x - c[0]) + (c[0] - b[0]) * (y - c[1])) / denominator
            second = ((c[1] - a[1]) * (x - c[0]) + (a[0] - c[0]) * (y - c[1])) / denominator
            third = 1 - first - second
            mask = (first >= -1e-7) & (second >= -1e-7) & (third >= -1e-7)
            if np.any(mask):
                output.append((xx[mask], yy[mask], np.stack((first[mask], second[mask], third[mask]), axis=1)))
    return output


def source_images(layers):
    result = {}
    for layer in layers:
        for identity, artwork in ((layer.get("imageHash"), True), *((mask, False) for mask in core.emission_masks(layer))):
            key = identity if artwork else ("mask", identity)
            if identity and key not in result:
                image = Image.open(core.image_path(identity)).convert("RGBA")
                pixels = np.asarray(image, dtype=np.float32) / 255
                if artwork:
                    pixels[:, :, :3] = linear(pixels[:, :, :3]) * pixels[:, :, 3:]
                result[key] = pixels
    return result


def sample_pixels(pixels, uv):
    height, width = pixels.shape[:2]
    x = np.clip(uv[:, 0] * width - .5, 0, width - 1)
    y = np.clip(uv[:, 1] * height - .5, 0, height - 1)
    x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
    x1, y1 = np.minimum(x0 + 1, width - 1), np.minimum(y0 + 1, height - 1)
    fx, fy = (x - x0)[:, None], (y - y0)[:, None]
    return ((pixels[y0, x0] * (1 - fx) + pixels[y0, x1] * fx) * (1 - fy) +
            (pixels[y1, x0] * (1 - fx) + pixels[y1, x1] * fx) * fy)


@lru_cache(maxsize=24)
def native_pixels(path, display):
    pixels = np.asarray(Image.open(path).convert("RGB"), dtype=np.float32) / 255
    return linear(pixels) if display else pixels


def original_values(part, triangle, uv, root=None):
    root = root or core.BUILTIN
    group = next((group for group in part.get("materialGroups", []) if group["start"] <= triangle * 3 < group["start"] + group["count"]), None)
    if group is None:
        return None
    folder = root / "preview"
    identity = group.get("previewMaterial", group["material"])
    base_path = folder / (identity + "_base.png")
    data_path = folder / (identity + "_mra.png")
    if not base_path.is_file() or not data_path.is_file():
        return None
    coordinates = uv - np.floor(uv)
    base = sample_pixels(native_pixels(str(base_path), True), coordinates)
    data = sample_pixels(native_pixels(str(data_path), False), coordinates)
    parameters = np.ones((len(uv), 4), dtype=np.float32)
    parameters[:, :2] = data[:, :2]
    return base, parameters


def emission_strength(settings, points, seconds):
    mode = settings.get("mode", "static")
    if mode == "breathe":
        period = max(.1, float(settings["period"]))
        phase = float(settings.get("phase", 0))
        wave = (1 - math.cos(2 * math.pi * seconds / period + phase)) / 2
        return settings.get("minimum", .2) + (settings.get("maximum", 4) - settings.get("minimum", .2)) * wave
    strength = settings.get("intensity", 1)
    if mode != "flow":
        return strength
    progress = flow_position(settings, points)
    width = settings["width"]
    phase = (seconds * settings["speed"] + settings.get("phase", 0) / math.tau) % 1
    distance = phase * (1 + width) - progress
    start = np.clip(distance / (width * .2), 0, 1)
    tail = np.clip((width - distance) / (width * .8), 0, 1)
    return strength * (start * start * (3 - 2 * start)) * (tail * tail * (3 - 2 * tail))


def flow_position(settings, points):
    low, high = settings["range"]
    progress = np.clip((points[:, 0] - low) / (high - low), 0, 1)
    return 1 - progress if settings["direction"] == "frontToBack" else progress


def is_animated(layer):
    light = layer.get("emission", {})
    return (layer["kind"] == "emission" or light.get("enabled")) and light.get("mode") in ("flow", "breathe")


def has_stamps(layer):
    return layer["kind"] == "stroke" or layer["kind"] == "emission" and "brush" in layer


def masked_coverage(images, identity, uv):
    sample = sample_pixels(images[("mask", identity)], uv)
    return sample[:, 0] * sample[:, 3]


def evaluate(points, normals, barycentric, triangle, part, layers, images, seconds=0, original=None, animated_slots=None,
             prepared_brushes=None):
    glass = part["surfaceRole"] == "glass"
    base = np.tile(color("#56627f" if glass else "#656b78"), (len(points), 1))
    emission = np.zeros((1 + len(animated_slots or ()), len(points), 3), dtype=np.float32)
    parameters = np.tile([.05, .13, 1., 1.] if glass else [.2, .65, 1., 1.], (len(points), 1))
    if original is not None:
        base, parameters = (value.copy() for value in original)
    # paint_layers places component colors before the ordered editable layers.
    for layer in layers:
        selected = layer_coverage(layer, part, triangle, barycentric)
        opacity = np.clip(float(layer.get("opacity", 1)), 0, 1)
        alpha = selected.astype(np.float32) * opacity
        if has_stamps(layer):
            stamps = prepared_brushes[layer["layerId"]] if prepared_brushes is not None else prepare_brush(layer, part)
            alpha *= brush_weights(points, normals, layer, stamps)
        uv = None
        if layer.get("imageHash"):
            uv, valid = projection_coordinates(points, normals, layer)
            sample = sample_pixels(images[layer["imageHash"]], uv)
            alpha *= valid * sample[:, 3]
            rgb = np.divide(sample[:, :3], sample[:, 3:], out=np.zeros_like(sample[:, :3]), where=sample[:, 3:] > 1e-8)
        else:
            rgb = color(layer.get("color", "#ffffff"))
        if layer["kind"] != "emission":
            base = base * (1 - alpha[:, None]) + rgb * alpha[:, None]
            emission *= 1 - alpha[:, None]
            parameters[:, 0] = parameters[:, 0] * (1 - alpha) + float(layer.get("metalness", .2)) * alpha
            parameters[:, 1] = parameters[:, 1] * (1 - alpha) + float(layer.get("roughness", .65)) * alpha
        settings = layer.get("emission", {})
        if layer["kind"] == "emission" or settings.get("enabled"):
            slot = animated_slots.get(layer["layerId"], 0) if animated_slots else 0
            if slot:
                peak = settings.get("maximum", 4) if settings["mode"] == "breathe" else settings.get("intensity", 1)
            else:
                peak = emission_strength(settings, points, seconds)
            amount = alpha * peak
            if uv is not None and settings.get("regions"):
                for region in settings["regions"]:
                    mask = masked_coverage(images, region["maskHash"], uv)
                    tint = color(region["color"]) * (rgb if region.get("useImageColor") else 1)
                    emission[slot] += tint * (amount * mask)[:, None]
            else:
                if settings.get("maskHash") and uv is not None:
                    amount *= masked_coverage(images, settings["maskHash"], uv)
                tint = color(settings.get("color", layer.get("color", "#ffffff")))
                emission[slot] += tint * (rgb if uv is not None else 1) * amount[:, None]
    return np.clip(base, 0, 1), np.maximum(emission if animated_slots else emission[0], 0), parameters


def prepare_brush(layer, part):
    matrix = np.asarray(part["assembledTransform"])
    inverse = np.linalg.inv(matrix[:3, :3])
    prepared = []
    for stamp in layer.get("brush", {}).get("stamps", []):
        if stamp["unitId"] != part["unitId"] or stamp["partId"] != part["instanceId"]:
            continue
        center = (matrix @ np.array([*stamp["position"], 1]))[:3]
        normal = np.asarray(stamp["normal"]) @ inverse
        normal /= max(float(np.linalg.norm(normal)),1e-12)
        prepared.append((center, normal, stamp["radius"]))
    return prepared


def brush_weights(points, normals, layer, stamps):
    result = np.zeros(len(points),dtype=np.float32)
    hardness = float(layer.get("brush",{}).get("hardness",.7))
    lower, upper = points.min(0), points.max(0)
    for center, normal, radius in stamps:
        # The stamp is confined to a cylinder of depth ±radius/2.
        if any(np.maximum(lower - center, center - upper) > radius * 1.12):
            continue
        relative = points-center
        depth = relative@normal
        distance = np.linalg.norm(relative-depth[:,None]*normal,axis=1)/radius
        fade = np.clip((1-distance)/max(1-hardness,1e-5),0,1)
        fade = fade*fade*(3-2*fade)
        result = np.maximum(result,fade*(normals@normal>.05)*(np.abs(depth)<=radius*.5))
    return result


def part_arrays(part, project):
    path = core.pack_for(project) / "models" / (part["unitId"][2:] + ".npz")
    with np.load(path, allow_pickle=False) as arrays:
        role = part["componentId"]
        representative = arrays[role + "_representative"]
        positions = arrays[role + "_positions"][representative]
        uvs = arrays[role + "_uvs"][representative]
        refs = arrays[role + "_refs"][representative]
    return positions, uvs, refs


def paint_masks(part, positions, layers):
    matrix = np.asarray(part["assembledTransform"])
    points = positions @ matrix[:3, :3].T + matrix[:3, 3]
    normals = np.cross(positions[:, 1] - positions[:, 0], positions[:, 2] - positions[:, 0]) @ np.linalg.inv(matrix[:3, :3])
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-20)
    result = {}
    for layer in layers:
        mask = np.full(len(positions), layer.get('opacity', 1) > 0, dtype=bool)
        if has_stamps(layer):
            centers = points.mean(1)
            radii = np.linalg.norm(points-centers[:,None,:],axis=2).max(1)
            touched = np.zeros(len(positions),dtype=bool)
            for stamp in layer.get("brush",{}).get("stamps",[]):
                if stamp["unitId"]==part["unitId"] and stamp["partId"]==part["instanceId"]:
                    center = (matrix@np.array([*stamp["position"],1]))[:3]
                    normal = np.asarray(stamp["normal"])@np.linalg.inv(matrix[:3,:3])
                    normal /= max(float(np.linalg.norm(normal)),1e-12)
                    depth = (points-center)@normal
                    touched |= ((np.linalg.norm(centers-center,axis=1) <= radii+stamp["radius"]*1.2) &
                                (normals@normal > .05) & (depth.min(1)<=stamp["radius"]*.5) & (depth.max(1)>=-stamp["radius"]*.5))
            mask &= touched
        if layer.get('coverageMode') == 'quads':
            selected = np.zeros(len(positions), dtype=bool)
            for index in layer.get('coverage', {}).get(part['instanceId'], []):
                quad = part['quads'][index]
                selected[quad[0]] = True
                if quad[1] >= 0:
                    selected[quad[1]] = True
            mask &= selected
        if layer.get('imageHash'):
            projection = layer['projection']
            if projection.get('type') == 'cylinder':
                mask &= cylinder_intersects(points, layer)
            else:
                uv, _ = projection_coordinates(points.reshape(-1,3), np.repeat(normals,3,axis=0), layer)
                crop = np.asarray(layer.get('crop',[0,0,1,1]))
                uv = ((uv-crop[:2])/(crop[2:]-crop[:2])).reshape(-1,3,2)
                mask &= np.all(uv.max(1) >= 0, axis=1) & np.all(uv.min(1) <= 1, axis=1)
                normal = np.asarray(projection['normal'])
                depth = (points-np.asarray(projection['origin'])) @ normal
                front = .00002 if projection.get('depthMode') == 'inward' else projection['depth']
                mask &= (depth.min(1) <= front) & (depth.max(1) >= -projection['depth'])
                if not projection.get('wrap', True):
                    mask &= normals @ normal > projection.get('cutoff',.001)
        result[layer['layerId']] = mask
    return result


def surfaces(part, project, size=512, cancel=lambda: None, for_preview=False, masks=None, seconds=0,
             native_motion=False, geometry=None):
    layers = layers_for(part, project)
    if for_preview:
        layers = copy.deepcopy(layers)
        for layer in layers:
            settings = layer.get("emission", {})
            if settings.get("mode") == "breathe":
                settings["mode"] = "static"
                settings["intensity"] = settings.get("maximum", 4)
    positions, uvs, refs = geometry if geometry is not None else part_arrays(part, project)
    masks = masks if masks is not None else paint_masks(part, positions, layers)
    affected = np.logical_or.reduce(list(masks.values())) if masks else np.zeros(len(positions), dtype=bool)
    native_root = core.pack_for(project)
    opaque_fill = any(layer["kind"] in ("fill", "brush") and layer.get("coverageMode") != "quads" and layer.get("opacity", 1) == 1 for layer in layers)
    has_native = not opaque_fill and bool(part.get("materialGroups")) and (native_root / "preview/materials.json").is_file()
    if not for_preview and geometry is None and any(layer.get("imageHash") for layer in layers):
        size = max(size, 2048)
    matrix = np.asarray(part["assembledTransform"])
    assembled = positions @ matrix[:3, :3].T + matrix[:3, 3]
    local_normals = np.cross(positions[:, 1] - positions[:, 0], positions[:, 2] - positions[:, 0])
    normals = local_normals @ np.linalg.inv(matrix[:3, :3])
    normals /= np.maximum(np.linalg.norm(normals, axis=1, keepdims=True), 1e-20)
    sides = np.argmax(np.abs(local_normals), axis=1) * 2 + (np.take_along_axis(local_normals, np.argmax(np.abs(local_normals), axis=1)[:, None], axis=1)[:, 0] < 0)
    images = source_images(layers)
    prepared_brushes = {layer["layerId"]: prepare_brush(layer, part) for layer in layers if has_stamps(layer)}
    partition_uv = any(layer.get("imageHash") or layer.get("coverageMode") == "quads" or has_stamps(layer) or
                       (is_animated(layer) if native_motion else layer.get("emission", {}).get("mode") == "flow") for layer in layers)
    grouped = defaultdict(list)
    for index, side in enumerate(sides):
        if not affected[index]:
            continue
        partition_side = int(side) if partition_uv and part["componentId"] != "ammo" else 0
        grouped[(int(refs[index, 1]), partition_side)].append(index)
    for (group, side), indices in grouped.items():
        cancel()
        bins = []
        for serial, triangle in enumerate(indices):
            if serial % 128 == 0:
                cancel()
            chosen = None
            pixels = triangle_pixels(uvs[triangle], min(size, 192)) if partition_uv else []
            for candidate in bins:
                conflict = False
                for x, y, barycentric in pixels:
                    old = candidate["occupancy"][y, x]
                    sample = barycentric @ positions[triangle]
                    occupied = np.isfinite(old[:, 0])
                    if np.any(occupied & (np.linalg.norm(np.nan_to_num(old) - sample, axis=1) > .001)):
                        conflict = True
                        break
                if not conflict:
                    chosen = candidate
                    break
            if chosen is None:
                chosen = dict(triangles=[], occupancy=np.full((min(size, 192), min(size, 192), 3), np.nan, dtype=np.float32))
                bins.append(chosen)
            chosen["triangles"].append(triangle)
            for x, y, barycentric in pixels:
                chosen["occupancy"][y, x] = barycentric @ positions[triangle]
        for bin_index, partition in enumerate(bins):
            cancel()
            surface_layers = [layer for layer in layers if np.any(masks[layer['layerId']][partition['triangles']])]
            animated_layers = [(layer["layerId"], layer["emission"]) for layer in surface_layers if native_motion and is_animated(layer)]
            animated_slots = {identity: i + 1 for i, (identity, _) in enumerate(animated_layers)}
            surface_partitioned = any(layer.get("imageHash") or layer.get("coverageMode") == "quads" or has_stamps(layer) or
                                      layer.get("emission", {}).get("mode") == "flow" for layer in surface_layers) or bool(animated_layers)
            surface_opaque = any(layer['kind'] in ('fill','brush') and layer.get('coverageMode') != 'quads' and layer.get('opacity',1) == 1 for layer in surface_layers)
            surface_nonuniform = surface_partitioned or (has_native and not surface_opaque)
            surface_size = size if any(layer.get('imageHash') for layer in surface_layers) else min(size,512)
            key = "/".join((part["instanceId"], str(group), str(side), str(bin_index)))
            metadata = dict(key=key, instanceId=part["instanceId"], unitId=part["unitId"], componentId=part["componentId"],
                            surfaceRole=part["surfaceRole"], triangles=partition["triangles"],
                            refs=refs[partition["triangles"]].tolist())
            if not surface_nonuniform:
                triangle = partition["triangles"][0]
                values = evaluate(assembled[triangle].mean(0)[None], normals[triangle][None],
                                  np.array([[1 / 3] * 3]), triangle, part, surface_layers, images, seconds=seconds,
                                  prepared_brushes=prepared_brushes)
                uniform = (np.broadcast_to(value[0].astype(np.float32),
                                           (surface_size, surface_size, value.shape[-1])) for value in values)
                base, emission, parameters = uniform
                yield dict(**metadata, base=base, emission=emission, parameters=parameters)
                continue
            glass = part["surfaceRole"] == "glass"
            base = np.tile(color("#56627f" if glass else "#656b78"), (surface_size, surface_size, 1))
            emission = np.zeros_like(base)
            animated = [np.zeros((surface_size, surface_size, 4), dtype=np.float32) for _ in animated_layers]
            parameters = np.tile(np.asarray([.05, .13, 1, 1] if glass else [.2, .65, 1, 1], dtype=np.float32), (surface_size, surface_size, 1))
            painted = np.zeros((surface_size, surface_size), dtype=bool)
            for serial, triangle in enumerate(partition["triangles"]):
                if serial % 128 == 0:
                    cancel()
                for x, y, barycentric in triangle_pixels(uvs[triangle], surface_size):
                    points = barycentric @ assembled[triangle]
                    original = None if surface_opaque else original_values(part, triangle, barycentric @ uvs[triangle], native_root)
                    rgb, light, values = evaluate(points, np.tile(normals[triangle], (len(points), 1)), barycentric,
                                                   triangle, part, surface_layers, images, seconds=seconds, original=original,
                                                   animated_slots=animated_slots, prepared_brushes=prepared_brushes)
                    base[y, x], parameters[y, x] = rgb, values
                    emission[y, x] = light[0] if animated else light
                    for channel, (_, settings), radiance in zip(animated, animated_layers, light[1:] if animated else (), strict=True):
                        channel[y, x, :3] = radiance
                        if settings["mode"] == "flow":
                            channel[y, x, 3] = flow_position(settings, points)
                    painted[y, x] = True
            extend_texels(painted, (base, emission, parameters, *animated))
            output = [(identity, settings, pixels) for (identity, settings), pixels in zip(animated_layers, animated, strict=True)
                      if np.any(pixels[..., :3] > 0)]
            yield dict(**metadata, base=base.astype(np.float32, copy=False), emission=emission.astype(np.float32, copy=False),
                       parameters=parameters.astype(np.float32, copy=False), animated=output)


def extend_texels(painted, channels):
    """Extend painted texels by four pixels, scanning only their UV bounds."""
    rows = np.flatnonzero(painted.any(axis=1))
    if not len(rows):
        return
    columns = np.flatnonzero(painted.any(axis=0))
    margin = 4
    window = (slice(max(0, rows[0] - margin), rows[-1] + margin + 1),
              slice(max(0, columns[0] - margin), columns[-1] + margin + 1))
    painted = painted[window]
    channels = [channel[window] for channel in channels]
    for _ in range(margin):
        border = np.zeros_like(painted)
        for axis, shift in ((0, -1), (0, 1), (1, -1), (1, 1)):
            shifted = np.zeros_like(painted)
            if axis == 0:
                if shift == 1:
                    shifted[1:, :] = painted[:-1, :]
                else:
                    shifted[:-1, :] = painted[1:, :]
            elif shift == 1:
                shifted[:, 1:] = painted[:, :-1]
            else:
                shifted[:, :-1] = painted[:, 1:]
            neighbor = shifted & ~painted & ~border
            rows, columns = np.nonzero(neighbor)
            source = (rows - shift, columns) if axis == 0 else (rows, columns - shift)
            for channel in channels:
                channel[rows, columns] = channel[source]
            border |= neighbor
        painted |= border


def png_bytes(pixels, display=True):
    values = srgb(pixels) if display else pixels
    image = Image.fromarray(np.rint(np.clip(values, 0, 1) * 255).astype(np.uint8))
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def preview(project):
    data = core.assembly(project)
    key_data = dict(weapons=project["weapons"], links=project.get("links", []), layers=core.paint_layers(project),
                    active=project["activeWeaponId"], assetLocks=project["assetLocks"], baker=6)
    signature = hashlib.sha256(core.json_bytes(key_data)).hexdigest()
    folder = core.DATA / "cache/preview" / signature
    manifest = folder / "manifest.json"
    if manifest.is_file():
        try:
            info = json.loads(manifest.read_text(encoding="utf-8"))
            if all((folder / name).is_file() and core.file_hash(folder / name) == expected for name, expected in info["hashes"].items()):
                return info
        except (OSError, ValueError, KeyError, TypeError):
            pass
    folder.mkdir(parents=True, exist_ok=True)
    descriptions = []
    hashes = {}
    for part in data["parts"]:
        if not part["active"]:
            continue
        for surface in surfaces(part, project, 256, for_preview=True):
            identity = hashlib.sha256(surface["key"].encode()).hexdigest()[:16]
            files = {}
            peak = float(surface["emission"].max())
            for channel, pixels, display in (("base", surface["base"], True), ("emission", surface["emission"] / max(peak, 1), False), ("parameters", surface["parameters"], False)):
                name = identity + "_" + channel + ".png"
                raw = png_bytes(pixels, display)
                core.atomic_write(folder / name, raw)
                hashes[name] = hashlib.sha256(raw).hexdigest()
                files[channel] = "/cache/preview/" + signature + "/" + name
            descriptions.append(dict(instanceId=part["instanceId"], triangles=surface["triangles"], files=files,
                                     emissionScale=max(peak, 1), surfaceRole=surface["surfaceRole"]))
    result = dict(signature=signature, surfaces=descriptions, hashes=hashes)
    core.atomic_write(manifest, core.json_bytes(result))
    return result
