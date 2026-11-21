"""Transfer sample-space paint to other native ammunition components."""
import copy
import math
from compute_runtime import np


def local_mapping(source_bounds, target_bounds):
    source_bounds = np.asarray(source_bounds, dtype=float)
    target_bounds = np.asarray(target_bounds, dtype=float)
    source_size = np.maximum(source_bounds[1]-source_bounds[0], 1e-6)
    target_size = np.maximum(target_bounds[1]-target_bounds[0], 1e-6)
    source_axes = np.argsort(source_size)
    target_axes = np.argsort(target_size)
    linear = np.zeros((3,3))
    for source_axis,target_axis in zip(source_axes,target_axes):
        linear[target_axis,source_axis] = target_size[target_axis]/source_size[source_axis]
    if np.linalg.det(linear) < 0:
        linear[target_axes[0]] *= -1
    matrix = np.eye(4)
    matrix[:3,:3] = linear
    matrix[:3,3] = target_bounds.mean(0)-linear@source_bounds.mean(0)
    return matrix


def remap_layer(layer, part, component):
    source = layer.get("ammoSource")
    if part["role"] != "ammo" or not source:
        return layer
    if source["unitId"] == part["unitId"] and source["componentId"] == part["componentId"]:
        return layer
    layer = copy.deepcopy(layer)
    transform = local_mapping(source["bounds"], component["bounds"])
    source_matrix = np.asarray(source["assembledTransform"])
    current_matrix = np.asarray(part["assembledTransform"])
    world = current_matrix@transform@np.linalg.inv(source_matrix)
    if layer.get("projection"):
        projection = layer["projection"]
        projection["origin"] = (world@np.array([*projection["origin"],1]))[:3].tolist()
        linear = world[:3, :3]
        lengths = {}
        for axis in ("u","v"):
            value = linear @ np.asarray(projection[axis])
            lengths[axis] = float(np.linalg.norm(value))
            projection[axis] = (value/max(lengths[axis],1e-12)).tolist()
        if projection.get("type") == "cylinder":
            shaft = np.asarray(projection["v"])
            value = linear @ np.asarray(projection["normal"])
            value -= shaft * (value @ shaft)
            second_radius = float(np.linalg.norm(value))
            radius_scale = math.sqrt(lengths["u"] * second_radius)
            projection["u"] = np.cross(shaft, value / second_radius).tolist()
            projection["radius"] *= radius_scale
            projection["axis"] = "auto"
            lengths["u"] = radius_scale
        else:
            value = np.linalg.inv(linear).T @ np.asarray(projection["normal"])
        projection["normal"] = (value/max(float(np.linalg.norm(value)),1e-12)).tolist()
        projection["width"] *= lengths["u"]
        projection["height"] *= lengths["v"]
        projection["depth"] *= max(lengths.values())
        layer.setdefault("placement",{})["x"] = layer.get("placement",{}).get("x",0)*lengths["u"]
        layer["placement"]["y"] = layer["placement"].get("y",0)*lengths["v"]
    for stamp in layer.get("brush",{}).get("stamps",[]):
        if stamp["unitId"] != source["unitId"]:
            continue
        stamp["position"] = (transform@np.array([*stamp["position"],1]))[:3].tolist()
        normal = np.linalg.inv(transform[:3,:3]).T@np.asarray(stamp["normal"])
        stamp["normal"] = (normal/max(float(np.linalg.norm(normal)),1e-12)).tolist()
        stamp["radius"] *= abs(float(np.linalg.det(transform[:3,:3])))**(1/3)
        stamp["unitId"] = part["unitId"]
        stamp["partId"] = part["instanceId"]
    return layer
