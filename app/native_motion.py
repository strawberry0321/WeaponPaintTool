"""Runtime lighting on the game's existing character-emission material.

The gameplay vertex programs already receive global_viewport from the engine.
They carry its time through the emissive-power interpolant in the cloned
material. The original power multiplier is replaced with the exported value 1.
The armory programs retain their normal vertex inputs and display the mask.
"""

import hashlib
import json
import math
import struct

from PIL import Image
from compute_runtime import np
from hashing import murmur64_hash
from native_shader import (chunks, code_with, compile_shader, cstring, instructions, make_container,
                           mov, operands, programs, reflection, signature, u32)


SURFACE_PARENT = 0x87da288c087e87e2
EMISSION_TEMPLATE = 0x6135204a71fd3f47
CLOCK = "global_viewport"
ANIMATED = {"flow", "breathe"}
PIXEL_CLOCK_WORD = 43
TILE_PAD = 4
MAX_SIZE = 4096

TEMP, INPUT, OUTPUT, SAMPLER, RESOURCE = 0, 1, 2, 6, 7
LOG, MUL, RET, SAMPLE, DCL_CB, DCL_TEMPS = 47, 56, 62, 69, 89, 104


def settings_for_shader(settings):
    if settings["mode"] == "flow":
        return ("flow", float(settings["speed"]), float(settings["width"]), float(settings.get("phase", 0)))
    maximum = settings.get("maximum", 4)
    return ("breathe", float(settings["period"]), settings.get("phase", 0), settings.get("minimum", .2) / maximum if maximum else 0.)


def atlas_for(surface):
    static = surface["emission"]
    moving = surface["animated"]
    grid = 1 << math.ceil(math.log2(math.sqrt(len(moving) + 1)))
    tile = min(static.shape[0], MAX_SIZE // grid)
    if tile <= 2 * TILE_PAD:
        raise ValueError("同一绘制表面的动画图层超出原生纹理容量")
    inner = tile - TILE_PAD * 2
    strength = max(float(static.max()), *(float(image[..., :3].max()) for _, _, image in moving))
    atlas = np.zeros((grid * tile, grid * tile, 4), dtype=np.float32)
    for index, image in enumerate([static, *(pixels for _, _, pixels in moving)]):
        x, y = (index % grid) * tile, (index // grid) * tile
        channels = [np.asarray(Image.fromarray(np.ascontiguousarray(image[..., channel], dtype=np.float32)).resize(
            (inner, inner), Image.Resampling.BILINEAR)) for channel in range(image.shape[2])]
        if len(channels) == 3:
            channels.append(np.ones((inner, inner), dtype=np.float32))
        interior = np.stack(channels, axis=-1)
        interior[..., :3] /= strength
        atlas[y:y + tile, x:x + tile] = np.pad(interior, ((TILE_PAD, TILE_PAD), (TILE_PAD, TILE_PAD), (0, 0)), mode="wrap")
    profile = tuple(settings_for_shader(settings) for _, settings, _ in moving)
    return atlas, strength, (grid, tile, profile)


def _float(value):
    return f"{float(value):.9e}"


def atlas_shader(recipe, gameplay):
    grid, tile, effects = recipe
    uv_scale = _float((tile - 2 * TILE_PAD) / (grid * tile))
    lines = [
        "Texture2D<float4> atlas : register(t0);",
        "SamplerState surfaceSampler : register(s0);",
        "float4 main(float3 source : TEXCOORD0) : SV_Target {",
        f"    float2 uv = frac(source.xy) * {uv_scale};",
        f"    float2 dx = ddx(source.xy) * {uv_scale};",
        f"    float2 dy = ddy(source.xy) * {uv_scale};",
    ]
    for i in range(len(effects) + 1):
        u = _float((i % grid + TILE_PAD / tile) / grid)
        v = _float((i // grid + TILE_PAD / tile) / grid)
        lines.append(f"    float4 s{i} = atlas.SampleGrad(surfaceSampler, uv + float2({u}, {v}), dx, dy);")
    lines.append("    float3 result = s0.rgb;")
    for i, values in enumerate(effects, 1):
        if not gameplay:
            lines.append(f"    result += s{i}.rgb;")
        elif values[0] == "flow":
            _, speed, width, phase = values
            width, start, scale, phase = map(_float, (width, width * .2, 1 + width, phase / math.tau))
            lines.append(f"    float d{i} = frac(source.z * {_float(speed)} + {phase}) * {scale} - s{i}.a;")
            lines.append(f"    result += s{i}.rgb * smoothstep(0.0, {start}, d{i}) * (1.0 - smoothstep({start}, {width}, d{i}));")
        else:
            _, period, phase, minimum = values
            radians = _float(math.tau / max(.1, period))
            lines.append(f"    float b{i} = (1.0 - cos(source.z * {radians} + {_float(phase)})) * .5;")
            lines.append(f"    result += s{i}.rgb * lerp({_float(minimum)}, 1.0, b{i});")
    lines.append("    return float4(result, 1.0); }")
    return compile_shader("\n".join(lines))


def _register(kind, index, mask):
    return [2 | mask << 4 | kind << 12 | 1 << 20, index]


def _scalar(kind, index, lane):
    return [2 | 2 << 2 | lane << 4 | kind << 12 | 1 << 20, index]


def _operands(instruction):
    return [(start, end, kind, [instruction[index] for index in indexes])
            for start, end, kind, indexes in operands(instruction)]


def _connect(snippet, temp_offset, sample, time):
    code = chunks(snippet)[b"SHEX"]
    source_instructions = list(instructions(code))
    temp_total = next(line[1][1] for line in source_instructions if line[0] == DCL_TEMPS)
    coordinate_temp = temp_offset + temp_total
    _, coordinate, texture, sampler = sample
    target = sample[0][3][0]
    coordinate_operand, signed_time = time
    output = [mov(_register(TEMP, coordinate_temp, 3), coordinate_operand[coordinate[0]:coordinate[1]])]
    if signed_time is not None:
        output.append(mov(_register(TEMP, coordinate_temp, 4), signed_time))
    replacements = {INPUT: (TEMP, coordinate_temp), OUTPUT: (TEMP, target),
                    RESOURCE: (RESOURCE, texture[3][0]), SAMPLER: (SAMPLER, sampler[3][0])}
    for opcode, line in source_instructions:
        if 88 <= opcode <= 106 or opcode == RET:
            continue
        for start, _, kind, indexes in operands(line):
            if kind == TEMP:
                line[indexes[0]] += temp_offset
            elif kind in replacements:
                dest, index = replacements[kind]
                line[start] = (line[start] & ~(255 << 12)) | (dest << 12)
                line[indexes[0]] = index
            elif kind not in (4, 13):
                raise ValueError("原生光效编译结果含未映射的寄存器")
        output.append(line)
    return output, coordinate_temp + 1


def patch_pixel(original, snippet, gameplay):
    blocks = chunks(original)
    _, bindings = reflection(blocks[b"RDEF"])
    texture_slot = bindings["__tex_emissive_color"][5]
    code = blocks[b"SHEX"]
    program = list(instructions(code))
    temp_index = next(i for i, (opcode, _) in enumerate(program) if opcode == DCL_TEMPS)
    candidates = []
    for i, (opcode, word) in enumerate(program):
        if opcode == SAMPLE:
            operands_here = _operands(word)
            if operands_here[2][3] == [texture_slot]:
                candidates.append((i, operands_here))
    if len(candidates) != 1:
        raise ValueError("原生发光采样指令不匹配")
    sample_index, sample = candidates[0]
    time = None
    if gameplay:
        log_at = next(i for i in range(sample_index + 1, len(program)) if program[i][0] == LOG)
        opcode, power = program[log_at + 1]
        dest, tint, source = _operands(power)
        if opcode != MUL or source[2] != INPUT or dest[3] != tint[3] or source[3] == []:
            raise ValueError("原生发光输入布局不匹配")
        token = power[source[0]]
        if (token >> 4) & 255 != 255:
            raise ValueError("原生发光输入未使用 W 通道")
        varying = source[3][0]
        time = _scalar(INPUT, varying, 3)
        program[log_at + 1] = (54, mov(power[dest[0]:dest[1]], power[tint[0]:tint[1]]))
        clock_semantics = [key for key, reg in signature(blocks[b"ISGN"]).items() if reg[0] == varying and reg[1] & 8]
        if len(clock_semantics) != 1:
            raise ValueError("原生发光通道缺少输入语义")
    else:
        clock_semantics = [None]
    inserted, temps = _connect(snippet, program[temp_index][1][1], sample, (program[sample_index][1], time))
    program[temp_index][1][1] = temps
    updated = [line for _, line in program]
    updated[sample_index:sample_index + 1] = inserted
    blocks[b"SHEX"] = code_with(code, updated)
    return make_container(blocks, len(original) % 8), clock_semantics[0]


def patch_vertex(original, semantic):
    blocks = chunks(original)
    variables, resources = reflection(blocks[b"RDEF"])
    if CLOCK not in resources or variables[CLOCK][3] < 704:
        raise ValueError("原生顶点着色器缺少游戏时钟缓冲区")
    _, count, start, *_ = variables[CLOCK]
    declared_time = [(u32(blocks[b"RDEF"], start + i * 40 + 4))
                     for i in range(count)
                     if cstring(blocks[b"RDEF"], u32(blocks[b"RDEF"], start + i * 40)) == "time"]
    if declared_time != [700]:
        raise ValueError("原生材质时间字段布局已变化")
    register = resources[CLOCK][5]
    output, mask = signature(blocks[b"OSGN"])[semantic]
    if not mask & 8:
        raise ValueError("原生顶点着色器缺少所需发光输出")
    code = blocks[b"SHEX"]
    original_instructions = list(instructions(code))
    clock_src = [2 | 2 << 2 | 3 << 4 | 8 << 12 | 2 << 20, register, PIXEL_CLOCK_WORD]
    write_time = mov(_register(OUTPUT, output, 8), clock_src)
    updated, declared = [], False
    for opcode, line in original_instructions:
        if opcode == DCL_CB and line[2] == register:
            line[3] = max(line[3], PIXEL_CLOCK_WORD + 1)
            declared = True
        if opcode == RET:
            updated.append(write_time)
        updated.append(line)
    if not declared:
        raise ValueError("原生顶点着色器未绑定游戏时钟")
    blocks[b"SHEX"] = code_with(code, updated)
    return make_container(blocks, len(original) % 8)


def specialize_parent(main, gpu, recipe):
    identity_key = json.dumps(recipe, separators=(",", ":"), allow_nan=False)
    digest = hashlib.sha256(gpu).hexdigest()
    identity = murmur64_hash(("content/mods/weaponpaint/animated/v1/" + digest + "/" + identity_key).encode("ascii"))
    templates = {value: atlas_shader(recipe, value) for value in (False, True)}
    edits = {}
    gameplay_passes = armory_passes = 0
    pixel_cache, vertex_cache = {}, {}
    for group in programs(gpu):
        pixel = group.get(4)
        if not pixel or "__tex_emissive_color" not in reflection(chunks(pixel.raw)[b"RDEF"])[1]:
            continue
        vertex = group.get(1, group[0])
        gameplay = CLOCK in reflection(chunks(vertex.raw)[b"RDEF"])[1]
        key = pixel.name, gameplay
        if key not in pixel_cache:
            pixel_cache[key] = patch_pixel(pixel.raw, templates[gameplay], gameplay)
        patched, semantic = pixel_cache[key]
        edits[pixel.offset] = (pixel, patched)
        if gameplay:
            key = vertex.name, semantic
            if key not in vertex_cache:
                vertex_cache[key] = patch_vertex(vertex.raw, semantic)
            edits[vertex.offset] = (vertex, vertex_cache[key])
            gameplay_passes += 1
        else:
            armory_passes += 1
    if gameplay_passes != 10 or armory_passes != 10:
        raise ValueError("此资产版本的原生发光程序集合不匹配")
    renamed, source = {}, bytearray(gpu)
    root_name = u32(gpu, 4)
    if root_name != SURFACE_PARENT >> 32:
        raise ValueError("原生发光父材质不匹配")
    name_at = u32(gpu, 28) + 4
    if u32(gpu, name_at) != root_name:
        raise ValueError("原生发光父材质记录不匹配")
    for offset in (4, name_at):
        struct.pack_into("<I", source, offset, identity >> 32)
    sections, start = [], 0
    for at, (record, patched) in sorted(edits.items()):
        renamed.setdefault(record.name, int.from_bytes(hashlib.sha256(
            struct.pack("<QI", identity, record.name) + patched).digest()[:4], "little"))
        if not renamed[record.name]:
            raise ValueError("生成的着色器标识无效")
        struct.pack_into("<II", source, record.header, len(patched), renamed[record.name])
        struct.pack_into("<I", source, record.end, renamed[record.name])
    for at, (record, patched) in sorted(edits.items()):
        sections.extend([source[start:at], patched])
        start = record.end
    sections.append(source[start:])
    output = bytearray(b"".join(sections))
    struct.pack_into("<I", output, 8, len(output) - 8)
    struct.pack_into("<I", output, 24, len(output) - 8 - u32(output, 20))
    new_main = bytearray(main)
    struct.pack_into("<I", new_main, 16, len(output))
    list(programs(bytes(output)))
    return identity, bytes(new_main), bytes(output), dict(gameplayPasses=gameplay_passes, armoryPasses=armory_passes,
                                                        profile=recipe, originalSha256=digest)
