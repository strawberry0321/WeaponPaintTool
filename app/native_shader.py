"""DXBC containers and the material program table used by the baked surface shader."""

import ctypes
import math
import struct
from dataclasses import dataclass


def u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


def words(data):
    return list(struct.unpack(f"<{len(data) // 4}I", data))


def pack_words(values):
    return struct.pack(f"<{len(values)}I", *values)


def cstring(data, offset):
    return data[offset:data.index(0, offset)].decode("ascii")


_MD5_SHIFTS = ((7, 12, 17, 22), (5, 9, 14, 20), (4, 11, 16, 23), (6, 10, 15, 21))
_MD5_CONSTANTS = [int(abs(math.sin(i)) * (1 << 32)) for i in range(1, 65)]
_MASK = 0xffffffff


def shader_digest(container):
    """DXBC uses the MD5 transform with its own length words and block layout."""
    payload = container[20:]
    bits = len(payload) * 8
    tail_at = len(payload) & ~63
    tail = payload[tail_at:]
    length = struct.pack("<I", bits)
    closing = struct.pack("<I", (bits >> 2) | 1)
    if len(tail) >= 56:
        ending = tail + b"\x80" + bytes(63 - len(tail)) + length + bytes(56) + closing
    else:
        ending = length + tail + b"\x80" + bytes(55 - len(tail)) + closing
    stream = payload[:tail_at] + ending
    state = [0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476]
    for offset in range(0, len(stream), 64):
        block = struct.unpack_from("<16I", stream, offset)
        a, b, c, d = state
        for i in range(64):
            round_id = i // 16
            if round_id == 0:
                f, j = (b & c) | (~b & d), i
            elif round_id == 1:
                f, j = (d & b) | (~d & c), (5 * i + 1) % 16
            elif round_id == 2:
                f, j = b ^ c ^ d, (3 * i + 5) % 16
            else:
                f, j = c ^ (b | ~d), (7 * i) % 16
            s = _MD5_SHIFTS[round_id][i % 4]
            n = (a + f + _MD5_CONSTANTS[i] + block[j]) & _MASK
            a, b, c, d = d, (b + ((n << s) | (n >> (32 - s)))) & _MASK, b, c
        state = [(x + y) & _MASK for x, y in zip(state, (a, b, c, d), strict=True)]
    return pack_words(state)


def chunks(shader):
    if shader[:4] != b"DXBC" or u32(shader, 24) != len(shader):
        raise ValueError("原生着色器容器格式无效")
    return {shader[at:at + 4]: shader[at + 8:at + 8 + u32(shader, at + 4)]
            for at in struct.unpack_from(f"<{u32(shader, 28)}I", shader, 32)}


def make_container(blocks, alignment=0):
    # A private chunk rounds out the record without moving subsequent GPU data across its alignment.
    blocks = dict(blocks)
    size = 32 + len(blocks) * 4 + sum(8 + len(raw) for raw in blocks.values())
    if size % 8 != alignment:
        blocks[b"PRIV"] = blocks.get(b"PRIV", b"") + (b"\0" * (0 if b"PRIV" not in blocks else 4))
    offset = 32 + len(blocks) * 4
    positions, raw_chunks = [], []
    for tag, raw in blocks.items():
        positions.append(offset)
        chunk = tag + struct.pack("<I", len(raw)) + raw
        raw_chunks.append(chunk)
        offset += len(chunk)
    if offset % 8 != alignment:
        raise ValueError("原生着色器的记录对齐无效")
    output = b"DXBC" + bytes(16) + struct.pack("<III", 1, offset, len(blocks)) + pack_words(positions) + b"".join(raw_chunks)
    return output[:4] + shader_digest(output) + output[20:]


def reflection(block):
    cb_count, cb_offset, binding_count, binding_offset = struct.unpack_from("<4I", block)
    buffers = {}
    for i in range(cb_count):
        values = struct.unpack_from("<6I", block, cb_offset + i * 24)
        buffers[cstring(block, values[0])] = values
    resources = {}
    for i in range(binding_count):
        values = struct.unpack_from("<8I", block, binding_offset + i * 32)
        resources[cstring(block, values[0])] = values
    return buffers, resources


def signature(block):
    return {(cstring(block, name), index): (register, masks & 255)
            for name, index, _, _, register, masks in (
                struct.unpack_from("<6I", block, 8 + i * 24) for i in range(u32(block, 0)))}


def instructions(code):
    values = words(code)
    if values[1] != len(values):
        raise ValueError("原生着色器指令长度无效")
    at = 2
    while at < len(values):
        opcode = values[at] & 0x7ff
        length = values[at + 1] if opcode == 53 else (values[at] >> 24) & 127
        if not length or at + length > len(values):
            raise ValueError("原生着色器指令越界")
        yield opcode, values[at:at + length]
        at += length


def code_with(original, instructions_out):
    flat = [word for instruction in instructions_out for word in instruction]
    return pack_words([u32(original, 0), len(flat) + 2, *flat])


def operands(instruction):
    cursor = 1
    extension = instruction[0]
    while extension >> 31:
        extension = instruction[cursor]
        cursor += 1
    while cursor < len(instruction):
        start = cursor
        token = instruction[cursor]
        cursor += 1
        extension = token
        while extension >> 31:
            extension = instruction[cursor]
            cursor += 1
        kind = (token >> 12) & 255
        if kind in (4, 5):
            components = (0, 1, 4)[token & 3]
            cursor += components * (kind - 3)
            indexes = []
        else:
            dimension = (token >> 20) & 3
            if any((token >> (22 + index * 3)) & 7 for index in range(dimension)):
                raise ValueError("原生动画指令使用了不支持的寄存器寻址")
            indexes = list(range(cursor, cursor + dimension))
            cursor += dimension
        if cursor > len(instruction):
            raise ValueError("原生着色器操作数越界")
        yield start, cursor, kind, indexes


def mov(destination, source):
    return [54 | ((1 + len(destination) + len(source)) << 24), *destination, *source]


def _export(dll, name, arguments, result):
    lib = ctypes.WinDLL(dll)
    fn = getattr(lib, name)
    fn.argtypes = arguments
    fn.restype = result
    return fn


def read_blob(pointer):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    as_pointer = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(vtable[3])
    length = ctypes.WINFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(vtable[4])
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtable[2])
    try:
        return ctypes.string_at(as_pointer(pointer), length(pointer))
    finally:
        release(pointer)


def compile_shader(source, target=b"ps_5_0"):
    c = ctypes
    compiler = _export("d3dcompiler_47", "D3DCompile",
                       [c.c_char_p, c.c_size_t, c.c_char_p, c.c_void_p, c.c_void_p, c.c_char_p,
                        c.c_char_p, c.c_uint, c.c_uint, c.POINTER(c.c_void_p), c.POINTER(c.c_void_p)], c.c_long)
    blob, errors = c.c_void_p(), c.c_void_p()
    raw = source.encode("ascii")
    result = compiler(raw, len(raw), None, None, None, b"main", target, 1 << 15, 0, c.byref(blob), c.byref(errors))
    message = read_blob(errors).decode("ascii", errors="replace").strip("\0") if errors else ""
    if result or not blob:
        raise ValueError("原生动画着色器编译失败 " + message)
    return read_blob(blob)


def disassemble(shader):
    c = ctypes
    compiler = _export("d3dcompiler_47", "D3DDisassemble",
                       [c.c_char_p, c.c_size_t, c.c_uint, c.c_char_p, c.POINTER(c.c_void_p)], c.c_long)
    blob = c.c_void_p()
    if compiler(shader, len(shader), 0, None, c.byref(blob)) or not blob:
        raise ValueError("原生着色器反汇编失败")
    return read_blob(blob).decode("ascii", errors="replace").strip("\0")


@dataclass(frozen=True)
class ShaderRecord:
    header: int
    offset: int
    raw: bytes
    name: int

    @property
    def end(self):
        return self.offset + len(self.raw)


def programs(gpu):
    """Read the ordered program records while retaining their native byte offsets."""
    begin = u32(gpu, 20)
    if u32(gpu, 8) != len(gpu) - 8 or u32(gpu, 24) != len(gpu) - 8 - begin:
        raise ValueError("原生着色器资源段长度不符")
    count = u32(gpu, begin + 8)
    position = (begin + 104 + 7) & ~7
    counts = [u32(gpu, position + i * 16) for i in range(count)]
    position += count * 16

    def skip_padding(at):
        while at < len(gpu) and gpu[at] == 0:
            at += 1
        return at

    for number in counts:
        headers = []
        for _ in range(number):
            if not 0 < gpu[position] < 128:
                raise ValueError("原生着色器阶段标记无效")
            textures = u32(gpu, position + 16)
            stages = [position + 32 + i * 80 for i in range(6)]
            headers.append((textures, stages))
            position = skip_padding(position + 520)
        for texture_count, stages in headers:
            position = (position + 4 + u32(gpu, position) * 4 + 7) & ~7
            while u32(gpu, position) != 0xffffffff:
                position += 16
            position += 24
            samplers = sum(struct.unpack_from("<Q", gpu, position + i * 24 + 8)[0]
                           for i in range(texture_count))
            position += texture_count * 24 + samplers * 16
            program = {}
            for stage, head in enumerate(stages):
                length, expected_name = struct.unpack_from("<II", gpu, head)
                if not length:
                    continue
                code = gpu[position:position + length]
                block = chunks(code)
                name = u32(gpu, position + length)
                if name != expected_name:
                    raise ValueError("原生着色器标识不匹配")
                program[stage] = ShaderRecord(head, position, code, name)
                cbs, resources = reflection(block[b"RDEF"])
                end = (position + length + 4 + 7) & ~7
                textures = sum(row[1] == 2 for row in resources.values())
                shader_samplers = sum(row[1] == 3 for row in resources.values())
                position = skip_padding(end + 16 * (len(cbs) + textures) + 12 * shader_samplers)
            yield program
    if position != len(gpu):
        raise ValueError("原生着色器程序表尾部长度不符")
