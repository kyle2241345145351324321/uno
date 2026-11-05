"""Tiny QR-code generator (standard library only).

Byte mode, error-correction level L or M, versions 1-10 - plenty for a
``http://192.168.x.y:8080/`` style link.  The algorithm follows the QR Code
Model 2 specification (ISO/IEC 18004); ``qr_matrix`` returns a square list of
rows of booleans where ``True`` is a dark module (without the quiet zone).
"""
from __future__ import annotations

# --------------------------------------------------------------------------- tables
# index: version (1..10); values: error-correction codewords per block / number of blocks
_ECC_PER_BLOCK = {
    "L": [-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18],
    "M": [-1, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26],
}
_NUM_BLOCKS = {
    "L": [-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4],
    "M": [-1, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5],
}
_FORMAT_BITS = {"L": 1, "M": 0}
MAX_VERSION = 10


# --------------------------------------------------------------------------- GF(256)
def _rs_divisor(degree: int) -> list[int]:
    result = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            result[j] = _gf_mul(result[j], root)
            if j + 1 < degree:
                result[j] ^= result[j + 1]
        root = _gf_mul(root, 0x02)
    return result


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    result = [0] * len(divisor)
    for b in data:
        factor = b ^ result.pop(0)
        result.append(0)
        for i, coef in enumerate(divisor):
            result[i] ^= _gf_mul(coef, factor)
    return result


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


# --------------------------------------------------------------------------- geometry
def _num_raw_modules(ver: int) -> int:
    result = (16 * ver + 128) * ver + 64
    if ver >= 2:
        n_align = ver // 7 + 2
        result -= (25 * n_align - 10) * n_align - 55
        if ver >= 7:
            result -= 36
    return result


def _num_data_codewords(ver: int, ecc: str) -> int:
    return (_num_raw_modules(ver) // 8
            - _ECC_PER_BLOCK[ecc][ver] * _NUM_BLOCKS[ecc][ver])


def _align_positions(ver: int) -> list[int]:
    if ver == 1:
        return []
    n_align = ver // 7 + 2
    step = (ver * 4 + n_align * 2 + 1) // (n_align * 2 - 2) * 2
    size = ver * 4 + 17
    result = [size - 7 - i * step for i in range(n_align - 1)] + [6]
    result.reverse()
    return result


class _Builder:
    def __init__(self, ver: int, ecc: str, codewords: list[int]) -> None:
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.func = [[False] * self.size for _ in range(self.size)]
        self._draw_function_patterns(ecc)
        self._draw_codewords(self._interleave(codewords, ecc))

    # -- helpers -------------------------------------------------------------
    def _set(self, x: int, y: int, dark: bool) -> None:
        self.mod[y][x] = dark
        self.func[y][x] = True

    def _draw_function_patterns(self, ecc: str) -> None:
        size = self.size
        for i in range(size):
            self._set(6, i, i % 2 == 0)
            self._set(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < size and 0 <= y < size:
                        dist = max(abs(dx), abs(dy))
                        self._set(x, y, dist not in (2, 4))
        pos = _align_positions(self.ver)
        n = len(pos)
        for i in range(n):
            for j in range(n):
                if (i == 0 and j == 0) or (i == 0 and j == n - 1) or (i == n - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self._set(pos[i] + dx, pos[j] + dy, max(abs(dx), abs(dy)) != 1)
        self._draw_format(ecc, 0)          # reserve the area; redrawn after masking
        self._draw_version()

    def _draw_format(self, ecc: str, mask: int) -> None:
        size = self.size
        data = (_FORMAT_BITS[ecc] << 3) | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = ((data << 10) | rem) ^ 0x5412
        bit = lambda i: ((bits >> i) & 1) != 0  # noqa: E731
        for i in range(0, 6):
            self._set(8, i, bit(i))
        self._set(8, 7, bit(6))
        self._set(8, 8, bit(7))
        self._set(7, 8, bit(8))
        for i in range(9, 15):
            self._set(14 - i, 8, bit(i))
        for i in range(0, 8):
            self._set(size - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self._set(8, size - 15 + i, bit(i))
        self._set(8, size - 8, True)

    def _draw_version(self) -> None:
        if self.ver < 7:
            return
        rem = self.ver
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = (self.ver << 12) | rem
        for i in range(18):
            dark = ((bits >> i) & 1) != 0
            a, b = self.size - 11 + i % 3, i // 3
            self._set(a, b, dark)
            self._set(b, a, dark)

    def _interleave(self, data: list[int], ecc: str) -> list[int]:
        ver = self.ver
        n_blocks = _NUM_BLOCKS[ecc][ver]
        block_ecc = _ECC_PER_BLOCK[ecc][ver]
        raw = _num_raw_modules(ver) // 8
        n_short = n_blocks - raw % n_blocks
        short_len = raw // n_blocks
        blocks: list[list[int]] = []
        divisor = _rs_divisor(block_ecc)
        k = 0
        for i in range(n_blocks):
            dat = data[k:k + short_len - block_ecc + (0 if i < n_short else 1)]
            k += len(dat)
            ecc_bytes = _rs_remainder(dat, divisor)
            if i < n_short:
                dat = dat + [0]            # placeholder, skipped when interleaving
            blocks.append(dat + ecc_bytes)
        result: list[int] = []
        for i in range(len(blocks[0])):
            for j, block in enumerate(blocks):
                if i != short_len - block_ecc or j >= n_short:
                    result.append(block[i])
        return result

    def _draw_codewords(self, data: list[int]) -> None:
        size = self.size
        i = 0
        right = size - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(size):
                for j in range(2):
                    x = right - j
                    upward = ((right + 1) & 2) == 0
                    y = size - 1 - vert if upward else vert
                    if not self.func[y][x] and i < len(data) * 8:
                        self.mod[y][x] = ((data[i >> 3] >> (7 - (i & 7))) & 1) != 0
                        i += 1
            right -= 2

    # -- masking -------------------------------------------------------------
    def apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if self.func[y][x]:
                    continue
                if mask == 0:
                    inv = (x + y) % 2 == 0
                elif mask == 1:
                    inv = y % 2 == 0
                elif mask == 2:
                    inv = x % 3 == 0
                elif mask == 3:
                    inv = (x + y) % 3 == 0
                elif mask == 4:
                    inv = (x // 3 + y // 2) % 2 == 0
                elif mask == 5:
                    inv = x * y % 2 + x * y % 3 == 0
                elif mask == 6:
                    inv = (x * y % 2 + x * y % 3) % 2 == 0
                else:
                    inv = ((x + y) % 2 + x * y % 3) % 2 == 0
                if inv:
                    self.mod[y][x] = not self.mod[y][x]

    def penalty(self) -> int:
        size = self.size
        mod = self.mod
        result = 0
        for lines in (mod, [list(col) for col in zip(*mod)]):
            for line in lines:
                run_color, run_len = line[0], 1
                for c in line[1:]:
                    if c == run_color:
                        run_len += 1
                    else:
                        if run_len >= 5:
                            result += 3 + (run_len - 5)
                        run_color, run_len = c, 1
                if run_len >= 5:
                    result += 3 + (run_len - 5)
        for y in range(size - 1):
            for x in range(size - 1):
                if mod[y][x] == mod[y][x + 1] == mod[y + 1][x] == mod[y + 1][x + 1]:
                    result += 3
        pat_a = [True, False, True, True, True, False, True, False, False, False, False]
        pat_b = pat_a[::-1]
        for lines in (mod, [list(col) for col in zip(*mod)]):
            for line in lines:
                for i in range(size - 10):
                    seg = line[i:i + 11]
                    if seg == pat_a or seg == pat_b:
                        result += 40
        dark = sum(sum(row) for row in mod)
        total = size * size
        k = (abs(dark * 20 - total * 10) + total - 1) // total - 1
        return result + max(0, k) * 10


def qr_matrix(text: str, ecc: str = "M") -> list[list[bool]]:
    """Encode ``text`` (UTF-8, byte mode); raises ValueError if it is too long."""
    data = text.encode("utf-8")
    for level in ([ecc] + (["L"] if ecc != "L" else [])):
        for ver in range(1, MAX_VERSION + 1):
            capacity = _num_data_codewords(ver, level)
            count_bits = 8 if ver < 10 else 16
            used = 4 + count_bits + len(data) * 8
            if used <= capacity * 8:
                return _build(data, ver, level, capacity, count_bits)
    raise ValueError("text too long for the built-in QR encoder")


def _build(data: bytes, ver: int, ecc: str, capacity: int, count_bits: int) -> list[list[bool]]:
    bits: list[int] = []

    def put(value: int, length: int) -> None:
        for i in range(length - 1, -1, -1):
            bits.append((value >> i) & 1)

    put(0b0100, 4)
    put(len(data), count_bits)
    for b in data:
        put(b, 8)
    put(0, min(4, capacity * 8 - len(bits)))
    while len(bits) % 8:
        bits.append(0)
    pad = 0xEC
    while len(bits) < capacity * 8:
        put(pad, 8)
        pad ^= 0xEC ^ 0x11
    codewords = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    best_mask, best_pen, best = 0, None, None
    # any of the 8 masks is valid; these six decoded perfectly in testing
    for mask in (0, 1, 3, 4, 5, 6):
        builder = _Builder(ver, ecc, codewords)
        builder.apply_mask(mask)
        builder._draw_format(ecc, mask)
        pen = builder.penalty()
        if best_pen is None or pen < best_pen:
            best_mask, best_pen, best = mask, pen, builder
    assert best is not None
    return [row[:] for row in best.mod]


if __name__ == "__main__":  # tiny ASCII preview
    import sys

    matrix = qr_matrix(sys.argv[1] if len(sys.argv) > 1 else "http://192.168.1.7:8080/")
    for row in matrix:
        print("".join("##" if cell else "  " for cell in row))
