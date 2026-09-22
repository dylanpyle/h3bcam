#!/usr/bin/env python3
"""Disassemble the patch blob out of the BUILT image and check every constant."""
import struct, sys
try:
    import capstone
except ImportError:
    sys.exit("verify.py needs capstone:  pip install capstone\n"
             "(optional - it only disassembles the patch back out of a built image)")
BASE=0xC0100000
img=open(sys.argv[1],'rb').read()
md=capstone.Cs(capstone.CS_ARCH_ARM, capstone.CS_MODE_ARM)
def rd32(va): return struct.unpack_from('<I',img,va-BASE)[0]
def dis(va,n,label=""):
    print(f"--- {label} 0x{va:08x} ---")
    o=va-BASE
    for i in md.disasm(img[o:o+n*4], va):
        line=f"  {i.address:08x}  {i.mnemonic:<7} {i.op_str}"
        s=i.op_str.replace(' ','')
        if i.mnemonic.startswith('ldr') and '[pc,' in s:
            try:
                imm=s.split('[pc,#')[1].rstrip(']')
                neg=imm.startswith('-'); v=int(imm.lstrip('-'),0)
                t=(i.address+8)+(-v if neg else v)
                line+=f"   ; [{t:08x}] = 0x{rd32(t):08x}"
            except Exception: pass
        print(line)

print("=== HOOK SITES ===")
for va,name in ((0xC05734FC,"FUN_c05734fc (COMMIT/load)"),(0xC05733A0,"bulk loop top")):
    w=rd32(va)
    imm=w&0xFFFFFF
    if imm & 0x800000: imm-=0x1000000
    tgt=va+8+imm*4
    print(f"  0x{va:08x}: 0x{w:08x}  -> b 0x{tgt:08x}   {name}")

print("\n=== CLASS TABLE ===")
print("  idx7 is the patched slot: mode byte must stay 0x0A, name/flags/mask/init from idx4.")
t=0xC07AD170
for i in (4,7):
    w=[rd32(t+i*0x18+k*4) for k in range(6)]
    tag = "  <- patched" if i == 7 else "  <- source (Ambarella Video)"
    print(f"  idx{i}: "+" ".join("%08x"%x for x in w)+tag)
mode = rd32(t+7*0x18) & 0xFF
print(f"  idx7 mode byte = 0x{mode:02X}" + ("  OK" if mode == 0x0A else "  *** WRONG - usb_class_init searches on this ***"))

print("\n=== GATES (expect mov r0,#1 ; bx lr) ===")
for g in (0xC0456128,0xC0456140,0xC0517A4C):
    print(f"  0x{g:08x}: {rd32(g):08x} {rd32(g+4):08x}")

print()

# Function addresses come from patch.o so they cannot drift as the blob changes.
CAVE = 0xC061A000
def symbols(path="patch.o"):
    try:
        d = open(path, "rb").read()
    except OSError:
        return {}
    e_shoff = struct.unpack_from("<I", d, 0x20)[0]
    shent   = struct.unpack_from("<H", d, 0x2e)[0]
    shnum   = struct.unpack_from("<H", d, 0x30)[0]
    shstr   = struct.unpack_from("<H", d, 0x32)[0]
    sh = [struct.unpack_from("<10I", d, e_shoff + i*shent) for i in range(shnum)]
    stroff = sh[shstr][4]
    def nm(x):
        return d[stroff+x:d.index(b"\0", stroff+x)].decode()
    sec = {nm(x[0]): x for x in sh}
    symt, strt = sec[".symtab"], sec[".strtab"][4]
    out = {}
    for k in range(symt[5] // 16):
        o = symt[4] + k*16
        nameoff, value = struct.unpack_from("<II", d, o)[:2]
        if nameoff:
            n = d[strt+nameoff:d.index(b"\0", strt+nameoff)].decode()
            if n and not n.startswith("$"):
                out[n] = CAVE + value
    return out

syms = symbols()
if syms:
    for name, length in (("uvc_class_init", 6), ("uvc_start_hook", 10),
                         ("spawn_worker", 22), ("worker_main", 26), ("uvc_frame_hook", 42)):
        if name in syms:
            dis(syms[name], length, name)
else:
    print("(patch.o not found - skipping per-function disassembly)")

print("\n=== LITERAL POOL CHECK ===")
want = {0xC061B000: "CTX",          0xC0573500: "LOADER_NEXT",
        0xC011CDAC: "FN_ALLOC",     0xC011A228: "FN_TASKCREATE",
        0xC010B950: "FN_DLYTSK",    0xC0234DB8: "FN_OPMODE",
        0xC0620E60: "IAVOBJ_RESET",
        0xC08E5E6C: "IAVOBJ_TBL",   0xC0DD0A00: "PUMP_CTX",
        0xC05733A4: "BULK_NEXT",    0xC05193AC: "FN_BUILD_DESC",
        0xC045614C: "FN_UVC_INIT"}
if "worker_main" in syms:
    want[syms["worker_main"]] = "worker_main"
# MJPEG_BS (0x800000) and MJPEG_DESC (0x4000) are encodable ARM immediates, so
# they appear as MOVs rather than pool entries - checked separately below.
found = {}
for va in range(CAVE, CAVE + 0x800, 4):          # whole blob, not a fixed window
    v = rd32(va)
    if v in want and v not in found:
        found[v] = va
bad = 0
for v, n in want.items():
    ok = v in found
    bad += not ok
    print(("  OK   " if ok else "  MISS ") + f"{n:<16} 0x{v:08x}" +
          (f" @0x{found[v]:08x}" if ok else ""))
print(("\nall literals present" if not bad else f"\n{bad} MISSING - build is suspect"))

print("\n=== RING GEOMETRY (encoded as MOV immediates) ===")
for va in range(CAVE, CAVE + 0x800, 4):
    w = rd32(va)
    if (w & 0x0FEF0000) == 0x03A00000:           # mov rD, #imm
        imm, rot = w & 0xFF, (w >> 8) & 0xF
        val = ((imm >> (2*rot)) | (imm << (32 - 2*rot))) & 0xFFFFFFFF
        if val in (0x800000, 0x4000):
            print(f"  0x{va:08x}  mov r{(w>>12)&0xF}, #0x{val:X}" +
                  ("   MJPEG_BS" if val == 0x800000 else "   MJPEG_DESC"))
