from compute_runtime import np

def blocks(pixels):
    height, width, channels = pixels.shape
    padded = np.pad(pixels, ((0, (-height) % 4), (0, (-width) % 4), (0, 0)), mode="edge")
    return padded.reshape((height + 3) // 4, 4, (width + 3) // 4, 4, channels).transpose(0, 2, 1, 3, 4).reshape(-1, 16, channels)

def block_total(pixels):
    height, width = pixels.shape[:2]
    return ((height + 3) // 4) * ((width + 3) // 4)

def pack_bc1(values):
    low, high = values.min(axis=1), values.max(axis=1)
    quantizers = np.array([31, 63, 31])
    minimum, maximum = np.rint(low * quantizers).astype(np.uint16), np.rint(high * quantizers).astype(np.uint16)
    first = (maximum[:, 0] << 11) | (maximum[:, 1] << 5) | maximum[:, 2]
    second = (minimum[:, 0] << 11) | (minimum[:, 1] << 5) | minimum[:, 2]
    first_color, second_color = maximum / quantizers, minimum / quantizers
    palette = np.stack([first_color, second_color, (2 * first_color + second_color) / 3, (first_color + 2 * second_color) / 3], axis=1)
    flat = np.all(low == high, axis=1)
    packed = np.empty(len(values), dtype="<u4")
    if flat.any():
        # All sixteen texels select the same palette entry in a uniform block.
        distances = ((values[flat, :1] - palette[flat, :3]) ** 2).sum(axis=2)
        packed[flat] = distances.argmin(axis=1).astype(np.uint32) * np.uint32(0x55555555)
    variable = ~flat
    if variable.any():
        distances = ((values[variable, :, None, :] - palette[variable, None, :, :]) ** 2).sum(axis=3)
        distances[first[variable] <= second[variable], :, 3] = np.inf
        selected = distances.argmin(axis=2).astype(np.uint32)
        packed[variable] = (selected << (np.arange(16, dtype=np.uint32) * 2)).sum(axis=1)
    output = np.empty(len(values), dtype=[("first", "<u2"), ("second", "<u2"), ("indices", "<u4")])
    output["first"], output["second"], output["indices"] = first, second, packed
    return output

def quantize_bc4(channel):
    return np.rint(blocks(channel[:, :, None])[:, :, 0] * 255).astype(np.uint8)

def pack_bc4(values):
    first, second = values.max(axis=1), values.min(axis=1)
    output = np.zeros((len(values), 8), dtype=np.uint8)
    output[:, 0], output[:, 1] = first, second
    variable = first != second
    if variable.any():
        start, end = first[variable].astype(float), second[variable].astype(float)
        palette = np.stack([start, end] + [((7 - i) * start + i * end) / 7 for i in range(1, 7)], axis=1)
        selected = ((values[variable, :, None].astype(float) - palette[:, None, :]) ** 2).argmin(axis=2).astype(np.uint64)
        packed = (selected << (np.arange(16, dtype=np.uint64) * 3)).sum(axis=1)
        for byte in range(6):
            output[variable, 2 + byte] = (packed >> (byte * 8)) & 255
    return output

def encode_bc1(pixels):
    return pack_bc1(blocks(pixels[:, :, :3])).tobytes()

def encode_bc4(channel):
    return pack_bc4(quantize_bc4(channel)).tobytes()

def prepare_mip(pixels, fmt):
    """把一张 MIP 预处理成可并行分块的中间量，只做一次。"""
    if fmt == 71:
        return (blocks(pixels[:, :, :3]),)
    if fmt == 77:
        return (quantize_bc4(pixels[:, :, 3]), blocks(pixels[:, :, :3]))
    if fmt == 83:
        return (quantize_bc4(pixels[:, :, 0]), quantize_bc4(pixels[:, :, 1]))
    raise ValueError(f"Unsupported sample texture format {fmt}")

def pack_range(prepared, fmt, start, stop):
    """按块区间编码；整段调用时输出与 encode_mip 完全一致。"""
    if fmt == 71:
        return pack_bc1(prepared[0][start:stop]).tobytes()
    if fmt == 77:
        color = np.frombuffer(pack_bc1(prepared[1][start:stop]).tobytes(), dtype=np.uint8).reshape(-1, 8)
        return np.concatenate([pack_bc4(prepared[0][start:stop]), color], axis=1).tobytes()
    if fmt == 83:
        return np.concatenate([pack_bc4(prepared[0][start:stop]), pack_bc4(prepared[1][start:stop])], axis=1).tobytes()
    raise ValueError(f"Unsupported sample texture format {fmt}")

def encode_mip(pixels, fmt):
    return pack_range(prepare_mip(pixels, fmt), fmt, 0, block_total(pixels))
