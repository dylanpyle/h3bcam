#!/usr/bin/env python3
r"""Build an SD-card firmware update package - no Pi, no bootrom, no nandwrite.

The camera updates itself the way GoPro intended: drop two files in the root of
the SD card, power on, and it flashes itself.

HOW THE OFFICIAL PACKAGE WORKS (reverse-engineered, all verified against
HD3.03-firmware.bin):

  * The file is a concatenation of sections. Each section is a 0x100-byte header
    followed immediately by its body:

        +0x00 u32 crc32     zlib.crc32 of the body (standard, not a variant)
        +0x04 u32 ver_num
        +0x08 u32 ver_date
        +0x0C u32 img_len   body length in bytes
        +0x10 u32 mem_addr  load address
        +0x14 u32 flag
        +0x18 u32 magic     0xA324EB90

  * The section with mem_addr 0xC0100000 is the main ARM firmware - the exact
    same bytes as the `pri` NAND partition, truncated to img_len (0x706004).

  * The partition-table CRC we otherwise hand-patch at ptb+0x8c is THE SAME
    VALUE as that section header's crc32. The camera's own updater writes it.
    So a package only has to carry `pri`; `ptb` is regenerated on the camera.
    (Verified: sec4 crc == ptb@0x8c == zlib.crc32(pri[:0x706004]) == 0x08246FE3.)

  * `update.cmd` in the card root drives it. Ours says CAMERA:1 only, so the
    Wi-Fi app is left alone - there is no reason to reflash it.

The camera looks for `d:\update.cmd` and `HD3.03-firmware.bin` in the card root,
and only at boot: the firmware logs "Card inserted after boot or soft disconnect
- ignoring update", so the card must already be in when you power on.

Because that section IS the stock `pri`, you do not need to dump your own camera
to build a patched image. Extract it from GoPro's published firmware file:

    python3 make_update.py --extract-pri stock_pri.bin HD3.03-firmware.bin

The extracted image is padded with 0xFF to the full 8912896-byte partition size,
which is what build.py expects. Only the first 0x706004 bytes are real; the rest
is NAND padding that no patch touches (build.py refuses to emit a package if one
ever does).

Usage:
    python3 make_update.py --extract-pri <out pri> <stock HD3.03-firmware.bin>
    python3 make_update.py <stock HD3.03-firmware.bin> <patched pri> <outdir> [stock pri]
"""
import sys, os, struct, zlib

MAGIC     = 0xA324EB90
HDR       = 0x100
ARM_ADDR  = 0xC0100000

UPDATE_CMD = b"# Camera upgrade rules file\r\nOPTIONS:10\r\n\r\n# Load sequence\r\nCAMERA:1\r\n"

def sections(d):
    out, off = [], 0
    while off + HDR <= len(d):
        crc, vnum, vdate, ln, mem, flag, magic = struct.unpack_from("<7I", d, off)
        if magic != MAGIC:
            off += 0x800
            continue
        out.append(dict(hdr=off, crc=crc, ln=ln, mem=mem, body=off + HDR))
        off = (off + HDR + ln + 0x7FF) & ~0x7FF
    return out

PRILEN = 8912896      # full pri NAND partition size

def extract(out, pkg_path):
    pkg = bytearray(open(pkg_path, "rb").read())
    secs = [x for x in sections(pkg) if x["mem"] == ARM_ADDR]
    if len(secs) != 1:
        sys.exit("expected exactly one section at 0x%08X, found %d" % (ARM_ADDR, len(secs)))
    s = secs[0]
    body = bytes(pkg[s["body"]:s["body"] + s["ln"]])
    if (zlib.crc32(body) & 0xFFFFFFFF) != s["crc"]:
        sys.exit("section failed its own CRC - wrong or corrupt input file")
    if len(body) > PRILEN:
        sys.exit("section is larger than the pri partition")
    open(out, "wb").write(body + b"\xff" * (PRILEN - len(body)))
    print("extracted stock pri: %s" % out)
    print("  section len 0x%X, CRC 0x%08X verified, padded to %d bytes" % (s["ln"], s["crc"], PRILEN))
    print("\nNow:  python3 build.py %s none uvclive_pri.bin -" % out)

def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--extract-pri":
        return extract(sys.argv[2], sys.argv[3])
    if len(sys.argv) not in (4, 5):
        sys.exit(__doc__)
    stock_pkg, patched_pri, outdir = sys.argv[1:4]
    stock_pri_path = sys.argv[4] if len(sys.argv) == 5 else None
    pkg = bytearray(open(stock_pkg, "rb").read())
    pri = open(patched_pri, "rb").read()

    secs = [s for s in sections(pkg) if s["mem"] == ARM_ADDR]
    if len(secs) != 1:
        sys.exit("expected exactly one section at 0x%08X, found %d" % (ARM_ADDR, len(secs)))
    s = secs[0]
    ln = s["ln"]
    print("ARM firmware section: header 0x%06X, body 0x%06X, len 0x%06X" % (s["hdr"], s["body"], ln))

    if len(pri) < ln:
        sys.exit("patched pri is 0x%X bytes, need at least 0x%X" % (len(pri), ln))

    old = bytes(pkg[s["body"]:s["body"] + ln])
    if (zlib.crc32(old) & 0xFFFFFFFF) != s["crc"]:
        sys.exit("stock package failed its own CRC - wrong or corrupt input file")
    print("stock section CRC verified: 0x%08X" % s["crc"])

    new = pri[:ln]
    diff = sum(1 for a, b in zip(old, new) if a != b)
    if diff == 0:
        sys.exit("patched pri is identical to stock - nothing to do")
    print("bytes differing from stock: %d" % diff)

    # The package only carries the first img_len bytes. The nandwrite route writes
    # the whole partition, so a patch touching anything past img_len would install
    # via nandwrite but be SILENTLY LOST via the SD card. Guard against that.
    if stock_pri_path:
        stock = open(stock_pri_path, "rb").read()
        if len(stock) != len(pri):
            sys.exit("stock pri and patched pri differ in size")
        lost = [i for i in range(ln, len(pri)) if pri[i] != stock[i]]
        if lost:
            sys.exit("REFUSING: %d patched bytes lie past img_len 0x%X (first at file 0x%X, "
                     "VA 0x%08X). They cannot be delivered by an SD-card update - use the "
                     "nandwrite route, or move the patch below 0x%X."
                     % (len(lost), ln, lost[0], 0xC0100000 + lost[0], ln))
        print("verified: no patched bytes past img_len (SD-card update is lossless)")
    else:
        print("NOTE: pass the stock pri as a 4th argument to verify no patched bytes")
        print("      lie past img_len 0x%X, which an SD-card update cannot deliver." % ln)

    crc = zlib.crc32(new) & 0xFFFFFFFF
    pkg[s["body"]:s["body"] + ln] = new
    struct.pack_into("<I", pkg, s["hdr"], crc)
    print("new section CRC: 0x%08X" % crc)

    os.makedirs(outdir, exist_ok=True)
    fw = os.path.join(outdir, "HD3.03-firmware.bin")
    with open(fw, "wb") as f:
        f.write(pkg)
    with open(os.path.join(outdir, "update.cmd"), "wb") as f:
        f.write(UPDATE_CMD)

    # re-verify what we just wrote
    chk = open(fw, "rb").read()
    s2 = [x for x in sections(bytearray(chk)) if x["mem"] == ARM_ADDR][0]
    body = chk[s2["body"]:s2["body"] + s2["ln"]]
    assert (zlib.crc32(body) & 0xFFFFFFFF) == s2["crc"], "self-check failed"
    assert body == new, "self-check failed"
    print("\nself-check passed. wrote:")
    print("  %s  (%d bytes)" % (fw, len(pkg)))
    print("  %s" % os.path.join(outdir, "update.cmd"))
    print("\nCopy BOTH files to the root of the SD card, put the card in the camera,")
    print("then power on. The card must be in BEFORE boot - the firmware ignores a")
    print("card inserted afterwards.")

if __name__ == "__main__":
    main()
