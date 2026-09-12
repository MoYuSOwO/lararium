"""手写几份 PDF 当夹具(M6-6b):好的、坏的、加密的。

**为什么自己写**:仓库里唯一的 PDF 库是渲染用的 pypdfium2,它不管"造一份";为测试再拉一个
PDF 写库进来违反 D1(这里一百来行)。同 `injection_image.py` 的理由。

**坏 PDF 是真打出来的形状,不是推演的**(2026-09-13,pdfium 153.0.7999.0 实测):

```
截掉后一半 / 只剩开头 40 字节 / 截掉末尾 60 字节   → Data format error (err_code=3)
空文件 / 头对了正文是垃圾                         → Data format error
页树里一页都没有                                   → Data format error(pypdfium2 在页数 < 1 时抛)
标准安全处理器 + 用户密码                          → Incorrect password error (err_code=4)
页树说 3 页、只挂了 1 页                           → 能打开、len 是 3,第 2、3 页 Failed to load page
```

产物是确定性的:同样的输入永远得到同样的字节。
"""

import hashlib

# A4 与 16:9 幻灯片(pt)。讲义和课件最常见的两种纸。
A4 = (595.0, 842.0)
SLIDE = (720.0, 405.0)


def _assemble(pages: list[tuple[tuple[float, float], bytes]], kids_override: bytes = b"") -> bytes:
    """最小可用的 PDF:Catalog → Pages → Page x N,字体 Helvetica。"""
    objects: list[bytes] = [b"", b""]  # 1 Catalog、2 Pages,最后回填
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")  # 3
    kids = []
    for (width, height), content in pages:
        objects.append(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content))
        stream_no = len(objects)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %g %g] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
            % (width, height, stream_no)
        )
        kids.append(len(objects))
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    kid_refs = b" ".join(b"%d 0 R" % k for k in kids)
    objects[1] = kids_override or b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kid_refs, len(kids))
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


def pdf(count: int = 3, size: tuple[float, float] = A4) -> bytes:
    """count 页,每页写着「page N」。"""
    return _assemble(
        [
            (size, b"BT /F1 24 Tf 60 %d Td (page %d) Tj ET" % (size[1] - 80, n))
            for n in range(1, count + 1)
        ]
    )


def truncated() -> bytes:
    """没传完的那种:只有前一半。"""
    whole = pdf(3)
    return whole[: len(whole) // 2]


def zero_pages() -> bytes:
    return _assemble([], kids_override=b"<< /Type /Pages /Kids [] /Count 0 >>")


def lying_count() -> bytes:
    """页树说 3 页,实际只挂了 1 页:能打开,第 2 页读不出来。"""
    return _assemble(
        [(A4, b"BT /F1 24 Tf 60 700 Td (only page) Tj ET")],
        kids_override=b"<< /Type /Pages /Kids [5 0 R 99 0 R 98 0 R] /Count 3 >>",
    )


# PDF 标准安全处理器的填充串(PDF 1.7 规范 7.6.3.3,算法 2)。
_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _rc4(key: bytes, data: bytes) -> bytes:
    box = list(range(256))
    j = 0
    for i in range(256):
        j = (j + box[i] + key[i % len(key)]) % 256
        box[i], box[j] = box[j], box[i]
    i = j = 0
    out = bytearray()
    for byte in data:
        i = (i + 1) % 256
        j = (j + box[i]) % 256
        box[i], box[j] = box[j], box[i]
        out.append(byte ^ box[(box[i] + box[j]) % 256])
    return bytes(out)


def encrypted() -> bytes:
    """R2 / 40 位 RC4,用户密码 secret——空密码打不开。

    正文流没加密(pdfium 先验密码,验不过就停,轮不到读流),所以只需要 O / U 两项算对。
    """
    user_pw, owner_pw, file_id, permissions = b"secret", b"owner", b"0123456789abcdef", -4
    owner = _rc4(hashlib.md5((owner_pw + _PAD)[:32]).digest()[:5], (user_pw + _PAD)[:32])
    key = hashlib.md5(
        (user_pw + _PAD)[:32] + owner + permissions.to_bytes(4, "little", signed=True) + file_id
    ).digest()[:5]
    user = _rc4(key, _PAD)
    body = pdf(1)
    encrypt = (
        b"/Encrypt << /Filter /Standard /V 1 /R 2 /O <%s> /U <%s> /P %d >> /ID [<%s> <%s>] "
        % (
            owner.hex().encode(),
            user.hex().encode(),
            permissions,
            file_id.hex().encode(),
            file_id.hex().encode(),
        )
    )
    return body.replace(b"trailer\n<< ", b"trailer\n<< " + encrypt)


def png_size(blob: bytes) -> tuple[int, int]:
    """读 PNG 头里的宽高——断言"真的是一张这么大的 PNG",不必拉 Pillow。"""
    assert blob[:8] == b"\x89PNG\r\n\x1a\n", "不是 PNG"
    assert blob[12:16] == b"IHDR"
    return int.from_bytes(blob[16:20], "big"), int.from_bytes(blob[20:24], "big")
