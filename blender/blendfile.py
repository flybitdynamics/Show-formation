"""Pure-Python reader for .blend files (no Blender needed).

Reads the file's own SDNA (struct layout) so it works across Blender
versions. Supports plain, gzip and zstd-compressed files, the classic
BHead8 header and the large BHead8 header of Blender 5.x.

    bf = BlendFile(path)
    for ob in bf.ids("OB"):
        print(ob.name, ob["loc"])

Only reading is supported. Only the standard library is used (zstd needs
Python 3.14+ ``compression.zstd`` or the ``zstd`` command line tool).
"""

import gzip
import shutil
import struct
import subprocess

_PRIM = {
    "char": ("b", 1), "uchar": ("B", 1), "int8_t": ("b", 1), "uint8_t": ("B", 1),
    "short": ("h", 2), "ushort": ("H", 2), "int16_t": ("h", 2), "uint16_t": ("H", 2),
    "int": ("i", 4), "uint": ("I", 4), "int32_t": ("i", 4), "uint32_t": ("I", 4),
    "float": ("f", 4), "double": ("d", 8),
    "int64_t": ("q", 8), "uint64_t": ("Q", 8), "long": ("q", 8), "ulong": ("Q", 8),
    "bool": ("?", 1),
}


def _decompress(raw):
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        try:
            from compression import zstd  # Python 3.14+
            return zstd.decompress(raw)
        except ImportError:
            exe = shutil.which("zstd")
            if not exe:
                raise RuntimeError("This .blend is zstd-compressed; install Python 3.14+ or the zstd tool.")
            return subprocess.run([exe, "-d", "-c"], input=raw, capture_output=True, check=True).stdout
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


class Field:
    __slots__ = ("name", "raw", "type", "off", "size", "is_ptr", "dims", "elem")

    def __init__(self, name, raw, type_, off, size, is_ptr, dims, elem):
        self.name, self.raw, self.type, self.off = name, raw, type_, off
        self.size, self.is_ptr, self.dims, self.elem = size, is_ptr, dims, elem


class Struct:
    def __init__(self, type_, size, fields):
        self.type, self.size = type_, size
        self.fields = {f.name: f for f in fields}


class Ref:
    """A struct instance at an offset of the file buffer."""
    __slots__ = ("bf", "st", "off", "block")

    def __init__(self, bf, st, off, block=None):
        self.bf, self.st, self.off, self.block = bf, st, off, block

    def has(self, name):
        return name in self.st.fields

    def __getitem__(self, name):
        return self.bf.read_field(self, name)

    def get(self, name, default=None):
        return self.bf.read_field(self, name) if name in self.st.fields else default

    def ptr(self, name):
        f = self.st.fields[name]
        return struct.unpack_from("<Q", self.bf.buf, self.off + f.off)[0]

    def deref(self, name, type_=None):
        """Follow a pointer field to a Ref (or None)."""
        f = self.st.fields[name]
        return self.bf.ref_at(self.ptr(name), type_ or f.type, ctx=self.block)

    def sub(self, name):
        f = self.st.fields[name]
        return Ref(self.bf, self.bf.structs[f.type], self.off + f.off, self.block)

    @property
    def name(self):
        return self.bf.id_name(self)

    @property
    def addr(self):
        if self.block is None:
            return 0
        return self.block["old"] + (self.off - self.block["off"])

    def __repr__(self):
        return "<%s %s>" % (self.st.type, self.name if self.has("id") else hex(self.addr))


class BlendFile:
    def __init__(self, path):
        with open(path, "rb") as f:
            raw = f.read()
        self.buf = buf = _decompress(raw)
        if buf[:7] != b"BLENDER":
            raise ValueError("Not a .blend file")
        if buf[7:9].isdigit():  # Blender 5.x: BLENDER17-01v0502
            hsize = int(buf[7:9])
            large = True
        else:  # BLENDER-v405 / BLENDER_v280
            if buf[7:8] != b"-" or buf[8:9] != b"v":
                raise ValueError("Only 64-bit little-endian .blend files are supported")
            hsize, large = 12, False
        self.blocks = []
        self.by_addr = {}   # addr -> [blocks]; Blender 5 addresses are only unique per ID
        owner = -1
        pos = hsize
        while pos < len(buf):
            code = buf[pos:pos + 4].rstrip(b"\0").decode("latin1")
            if large:
                sdna, old, ln, nr = struct.unpack_from("<iQqq", buf, pos + 4)
                data = pos + 32
            else:
                ln, old, sdna, nr = struct.unpack_from("<iQii", buf, pos + 4)
                data = pos + 24
            if code != "DATA" and len(code) == 2:
                owner = len(self.blocks)
            b = {"code": code, "sdna": sdna, "old": old, "len": ln, "nr": nr, "off": data, "owner": owner}
            self.blocks.append(b)
            if old:
                self.by_addr.setdefault(old, []).append(b)
            pos = data + ln
            if code == "ENDB":
                break
        self._sorted = sorted(self.by_addr)
        self._read_sdna()

    # ------------------------------------------------------------------ SDNA
    def _read_sdna(self):
        buf = self.buf
        b = next(b for b in self.blocks if b["code"] == "DNA1")
        base = b["off"]  # SDNA sections are 4-byte aligned from the block start

        def align(p):
            return base + ((p - base + 3) & ~3)

        p = base + 4

        def strings(p, tag):
            assert buf[p:p + 4] == tag, "Unreadable .blend structure table"
            n = struct.unpack_from("<i", buf, p + 4)[0]
            p += 8
            out = []
            for _ in range(n):
                e = buf.index(b"\0", p)
                out.append(buf[p:e].decode("latin1"))
                p = e + 1
            return out, align(p)

        names, p = strings(p, b"NAME")
        types, p = strings(p, b"TYPE")
        p += 4
        tlen = struct.unpack_from("<%dh" % len(types), buf, p)
        p = align(p + 2 * len(types))
        n = struct.unpack_from("<i", buf, p + 4)[0]
        p += 8
        self.structs = {}
        self.struct_list = []
        for _ in range(n):
            t, nf = struct.unpack_from("<hh", buf, p)
            p += 4
            fields, off = [], 0
            for _ in range(nf):
                ft, fn = struct.unpack_from("<hh", buf, p)
                p += 4
                raw = names[fn]
                is_ptr = raw.startswith("*") or raw.startswith("(*")
                dims = [int(x.rstrip("]")) for x in raw.split("[")[1:]] if not raw.startswith("(*") else []
                elem = 8 if is_ptr else tlen[ft]
                count = 1
                for d in dims:
                    count *= d
                base = raw.lstrip("*(").split("[")[0].split(")")[0]
                fields.append(Field(base, raw, types[ft], off, elem * count, is_ptr, dims, elem))
                off += elem * count
            st = Struct(types[t], tlen[t], fields)
            self.structs[types[t]] = st
            self.struct_list.append(st)

    # --------------------------------------------------------------- access
    def block_at(self, addr, ctx=None):
        """Block containing addr (pointers can point inside a block).
        ctx is the block holding the pointer: among blocks sharing an address
        the one of the same ID wins."""
        if not addr:
            return None
        cands = self.by_addr.get(addr)
        if not cands:
            import bisect
            i = bisect.bisect_right(self._sorted, addr) - 1
            if i < 0:
                return None
            cands = [b for b in self.by_addr[self._sorted[i]] if addr < b["old"] + b["len"]]
            if not cands:
                return None
        if len(cands) == 1 or ctx is None:
            return cands[0]
        own = ctx["owner"]
        for b in cands:
            if b["owner"] == own:
                return b
        for b in cands:  # pointer to another ID
            if b["code"] != "DATA":
                return b
        return cands[0]

    def ref_at(self, addr, type_=None, index=0, ctx=None):
        if not addr:
            return None
        b = self.block_at(addr, ctx)
        if not b:
            return None
        st = self.structs.get(type_) if type_ else None
        if st is None or type_ in ("void", "ID"):
            if b["sdna"] > 0 or b["code"] not in ("DATA",):
                st2 = self.struct_list[b["sdna"]]
                if st is None or type_ in ("void", "ID") and st2.size >= 8:
                    st = st2
        if st is None:
            return None
        return Ref(self, st, b["off"] + (addr - b["old"]) + index * st.size, b)

    def array_at(self, addr, type_, count, ctx=None):
        b = self.block_at(addr, ctx)
        if not b:
            return []
        st = self.structs[type_]
        start = b["off"] + (addr - b["old"])
        return [Ref(self, st, start + i * st.size, b) for i in range(count)]

    def read_field(self, ref, name):
        f = ref.st.fields[name]
        off = ref.off + f.off
        buf = self.buf
        if f.is_ptr:
            if f.dims:
                return list(struct.unpack_from("<%dQ" % (f.size // 8), buf, off))
            return struct.unpack_from("<Q", buf, off)[0]
        if f.type == "char" and f.dims:
            return buf[off:off + f.size].split(b"\0")[0].decode("utf-8", "replace")
        prim = _PRIM.get(f.type)
        if prim is None:
            if f.dims:
                st = self.structs[f.type]
                return [Ref(self, st, off + i * st.size, ref.block) for i in range(f.size // st.size)]
            return Ref(self, self.structs[f.type], off, ref.block)
        fmt, size = prim
        count = f.size // size
        vals = struct.unpack_from("<%d%s" % (count, fmt), buf, off)
        if not f.dims:
            return vals[0]
        if len(f.dims) == 2:
            a, b2 = f.dims
            return [list(vals[i * b2:(i + 1) * b2]) for i in range(a)]
        return list(vals)

    def listbase(self, ref, name, type_):
        """Items of a ListBase field."""
        lb = ref.sub(name)
        addr = lb.ptr("first")
        out, seen = [], set()
        while addr and addr not in seen:
            seen.add(addr)
            item = self.ref_at(addr, type_, ctx=ref.block)
            if item is None:
                break
            out.append(item)
            addr = item.ptr("next")
        return out

    def ids(self, code):
        st_cache = {}
        for b in self.blocks:
            if b["code"] == code:
                st = st_cache.get(b["sdna"]) or self.struct_list[b["sdna"]]
                yield Ref(self, st, b["off"], b)

    def id_name(self, ref):
        if not ref.has("id"):
            return None
        return ref.sub("id")["name"][2:]

    # ------------------------------------------------------------ IDProperty
    _IDP = {0: "STRING", 1: "INT", 2: "FLOAT", 5: "ARRAY", 6: "GROUP", 7: "ID", 8: "DOUBLE", 9: "IDPARRAY", 10: "BOOLEAN"}

    def idprops(self, ref):
        """ID custom + system (add-on) properties of an ID as a dict."""
        out = {}
        idr = ref.sub("id")
        for key in ("properties", "system_properties"):
            if idr.has(key):
                p = idr.deref(key, "IDProperty")
                if p:
                    v = self._idp(p)[1]
                    if isinstance(v, dict):
                        out.update(v)
        return out

    def _idp(self, p):
        buf = self.buf
        t = self._IDP.get(p["type"])
        sub = p["subtype"]
        name = p["name"]
        data = p.sub("data")
        length = p["len"]
        if t == "INT":
            v = data["val"]
        elif t == "BOOLEAN":
            v = bool(data["val"])
        elif t == "FLOAT":
            v = struct.unpack_from("<f", buf, data.off + data.st.fields["val"].off)[0]
        elif t == "DOUBLE":
            v = struct.unpack_from("<d", buf, data.off + data.st.fields["val"].off)[0]
        elif t == "STRING":
            b = self.block_at(data.ptr("pointer"), p.block)
            v = buf[b["off"]:b["off"] + b["len"]].split(b"\0")[0].decode("utf-8", "replace") if b else ""
        elif t == "ARRAY":
            b = self.block_at(data.ptr("pointer"), p.block)
            fmt = {1: "i", 2: "f", 8: "d", 10: "b"}.get(sub, "i")
            v = list(struct.unpack_from("<%d%s" % (length, fmt), buf, b["off"])) if b and length else []
        elif t == "GROUP":
            v = {}
            for child in self.listbase(data, "group", "IDProperty"):
                k, cv = self._idp(child)
                v[k] = cv
        elif t == "IDPARRAY":
            v = [self._idp(c)[1] for c in self.array_at(data.ptr("pointer"), "IDProperty", length, p.block)]
        elif t == "ID":
            r = self.ref_at(data.ptr("pointer"), ctx=p.block)
            v = r.name if r is not None and r.has("id") else None
        else:
            v = None
        return name, v
