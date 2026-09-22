#!/usr/bin/env python3
"""Build the HERO3 live-UVC image from a PRISTINE stock pri + ptb.

  python3 build.py <stock-mtd4-pri> <stock-mtd1-ptb> <out-pri> <out-ptb>

Pass `none` for <stock-mtd1-ptb> to skip the partition table entirely. The SD-card
update route does not need it: the camera's own updater writes ptb@0x8c from the
firmware section header's CRC, which is the same value. Only the nandwrite route
needs a real ptb, because there nothing else computes it.

Patches (all inside the CRC region; ptb@0x8c holds crc32(pri[:0x706004])):
  1. Class table idx7 (mode 0x0A, "CDC ACM") <- the Ambarella Video entry, with
     init pointed at our trampoline. The mode BYTE is left alone: usb_class_init
     linear-searches on it, and idx7 is the slot `t app test usb_rs232 1` selects.
     (Do not use idx0/Mass Storage - the connect app runs and tears down video.)
  2. Three UVC-enable getters forced to return 1. They read a config blob that
     is primed by uvc_task_init - which GoPro compiled out to a stub.
  3. beq -> nop at 0xC0573840: streaming was gated on the static test JPEG
     loading, and in USB mode the card is not mounted as a:, so it always failed.
  4. FUN_c030b830 -> `mov r0,#0; bx lr`, so plugging in USB does not switch to
     the USB app and tear down the video preview pipeline.
  5. Active descriptor set -> the real UVC class-0x0E set at 0xC07F9158.
  6. One advertised frame: 1280x960, 2 MiB buffer.
  7. dwMaxVideoFrameSize -> 2 MiB in all four static probe records.
  8. Revive uvc_task_init (mov r0,#0 -> b 0xC0517F70) for the control plane.
  9. 0xC0333618: `b app_switch` -> `bx lr`, so the 300 s power-saving countdown
     cannot switch the camera into Mass Storage mid-stream.
 10. The patch blob itself at 0xC061A000.

See TECHNICAL.md for why each of these is needed.
"""
import struct, zlib, sys, hashlib

BASE   = 0xC0100000
CRCLEN = 0x706004
PRILEN = 8912896
PTBLEN = 262144

TBL        = 0xC07AD170
DESCSET_PTR   = 0xC07DB2F8       # active USB descriptor set pointer (file 0x6DB2F8)
DESCSET_VENDOR = 0xC07F8B78      # stock: vendor class 0xFF "fake UVC"
DESCSET_UVC    = 0xC07F9158      # real UVC 1.0 class 0x0E, bulk EP 0x81
FRAME_ARR  = 0xC07F8FF8          # HS MJPEG frame descriptor array, bLength-chained, NUL-terminated
PROBE_RECS = (0xC0804C68, 0xC0804C84, 0xC0804CA0, 0xC0804CBC)  # CUR/MIN/MAX/DEF, 28 B each
GATES      = (0xC0456128, 0xC0456140, 0xC0517A4C)
LOADER     = 0xC05734FC          # push {r4,r5,r6,r7,lr}
SKIP_BEQ   = 0xC0573840          # beq 0xC0573870 - skips task create when the
                                 # static-JPEG load fails (it always does in USB
                                 # mode: the card is not mounted as a:)
BULK_TOP   = 0xC05733A0          # ldrb r0,[r7]
CAVE       = 0xC061A000
SCRATCH    = 0xC061B000

LOADER_ORIG   = 0xE92D40F0       # push {r4,r5,r6,r7,lr}
BULK_TOP_ORIG = 0xE5D70000       # ldrb r0,[r7]
SKIP_BEQ_ORIG = 0x0A00000A       # beq +0x28
NOP           = 0xE1A00000       # mov r0, r0

# "never-PC": FUN_c030b830 posts app event 0xC300100F and its two callers
# (0xC0446370 / 0xC0446504, the USB-connect debounce paths) do `cmp r0,#1` to
# decide whether to switch into the USB/PC app. That app tears down the video
# preview pipeline, which is why the DSP sat at op-mode 0 with the camera still
# reporting appmode "video". Stub it to return 0 and the camera stays in preview
# while USB is connected. 8 bytes; proven on hardware in the earlier
# h3b-v300-rtos-console-nopc.bin image.
# GoPro compiled out uvc_task_init to a `mov r0,#0; bx lr` stub, leaving the real
# body orphaned at 0xC0517F70. It creates the "Device Request"/"Class Request"
# event flags and the uavc_ctrl_tsk task, primes the 0x170-byte UVC config blob,
# and hands the flag id to all six entity modules. Without it every UVC class
# request addressed to an entity (Processing Unit 5, Input Terminal 1) is queued
# to a task that does not exist, and a real OS driver times out:
#   uvcvideo: Failed to query (GET_INFO) UVC control 2 on unit 5: -110
# One word restores the whole control plane.
UVCTASK      = 0xC0517808
UVCTASK_ORIG = 0xE3A00000        # mov r0, #0
UVCTASK_NEW  = 0xEA0001D8        # b 0xC0517F70   ((0xC0517F70-0xC0517808-8)/4 = 0x1D8)

NEVERPC      = 0xC030B830
NEVERPC_ORIG = (0xE59F0480, 0xE3A02000)   # ldr r0,[pc,#0x480] ; mov r2,#0
NEVERPC_NEW  = (0xE3A00000, 0xE12FFF1E)   # mov r0,#0 ; bx lr


# "no power-saving sleep": THE ~290 s USB mode switch.
# Verified chain, end to end:
#   cable in -> charger_detection_stage2 posts app msg 0xE1000001
#            -> rec-DV dispatcher case 0x24 @0xC042C53C
#            -> usb_mode != 5, so app_switch(app_usb_msc)  @0xC042C5B4
#            -> app_usb_msc_start -> usb_desc_override(0xC07C5B4C) = VID 2672 PID 0004
#               "GoPro"/"Storage" -> usb_stop_class() -> usb_class_init(1)
#            -> host sees our UVC device vanish and 2672:0004 appear.
# What FIRES it at ~290 s is the power-saving countdown at 0xC03335AC: a 1 Hz
# timer that loads **300** (`mov r1,#0x12C` @0xC03335C4, hard-coded, NOT a pref),
# counts down, and on expiry tail-calls app_switch(app_misc_powersaving)
# @0xC0333618. Entering a new app re-delivers the USB-connect message to it, and
# that is what drops us into Mass Storage. It is armed only when
# app_pref_user->powersaving (u16 @ +0x4A) == 1, at 0xC0333658.
# Matches the measurement: six runs at 276-295 s, timed from USB plug (the last
# "activity"), across four builds. H.264 recording is immune because recording
# blocks power-saving and pressing record is activity.
# Fix: make the expiry return instead of switching apps. `pop {r4,r5,r6,lr}`
# already ran at 0xC0333614, so `bx lr` returns cleanly. The timer still
# unregisters and the flag still clears - only the app switch is removed.
# A webcam should not go to sleep, so this is the right behaviour change, and it
# is 4 bytes.
PWRSAVE      = 0xC0333618
PWRSAVE_ORIG = 0xEAF9D8BF        # b 0xC01A991C   (app_switch)
PWRSAVE_NEW  = 0xE12FFF1E        # bx lr

def foff(va): return va - BASE
def w32(x):   return struct.pack("<I", x & 0xFFFFFFFF)
def b_(frm, to): return 0xEA000000 | (((to - (frm + 8)) >> 2) & 0xFFFFFF)

def load_obj(path, base):
    """Extract .text and resolve R_ARM_CALL (0x1c/0x0a) and R_ARM_ABS32 (0x02)."""
    d = open(path, 'rb').read()
    e_shoff = struct.unpack_from('<I', d, 0x20)[0]
    shent   = struct.unpack_from('<H', d, 0x2e)[0]
    shnum   = struct.unpack_from('<H', d, 0x30)[0]
    shstr   = struct.unpack_from('<H', d, 0x32)[0]
    sh = [struct.unpack_from('<10I', d, e_shoff + i*shent) for i in range(shnum)]
    stroff = sh[shstr][4]
    def nm(x):
        e = d.index(b'\0', stroff + x); return d[stroff+x:e].decode()
    sec = {nm(s[0]): s for s in sh}
    text = bytearray(d[sec['.text'][4]:sec['.text'][4]+sec['.text'][5]])
    symt, strt = sec['.symtab'], sec['.strtab'][4]
    def snm(x):
        e = d.index(b'\0', strt + x); return d[strt+x:e].decode()
    syms = {}
    for i in range(symt[5]//16):
        o = symt[4] + i*16
        nameoff, value = struct.unpack_from('<II', d, o)[0:2]
        if nameoff: syms[snm(nameoff)] = value
    rel = next((v for k, v in sec.items() if k.startswith('.rel') and 'text' in k), None)
    nfix = 0
    if rel:
        for i in range(rel[5]//8):
            off, info = struct.unpack_from('<II', d, rel[4] + i*8)
            symi, rtype = info >> 8, info & 0xFF
            o = symt[4] + symi*16
            value = struct.unpack_from('<I', d, o + 4)[0]
            if rtype in (0x1C, 0x0A):                      # R_ARM_CALL / PC24
                w = struct.unpack_from('<I', text, off)[0]
                delta = (value - (off + 8)) >> 2
                struct.pack_into('<I', text, off, (w & 0xFF000000) | (delta & 0xFFFFFF))
            elif rtype == 0x02:                            # R_ARM_ABS32
                addend = struct.unpack_from('<I', text, off)[0]
                struct.pack_into('<I', text, off, (base + value + addend) & 0xFFFFFFFF)
            else:
                raise SystemExit("unhandled reloc type 0x%x" % rtype)
            nfix += 1
    return bytes(text), syms, nfix

def main():
    if len(sys.argv) != 5:
        sys.exit(__doc__.strip() + "\n")
    src, mtd1, out_pri, out_ptb = sys.argv[1:5]
    b = bytearray(open(src, 'rb').read())
    assert len(b) == PRILEN, "pri is %d bytes, expected %d" % (len(b), PRILEN)
    stock = bytes(b)

    code, syms, nfix = load_obj("patch.o", CAVE)
    print("patch blob: %d bytes, %d relocations resolved" % (len(code), nfix))
    for k, v in sorted(syms.items(), key=lambda kv: kv[1]):
        print("   %-16s 0x%08x" % (k, CAVE + v))

    # cave + scratch must be virgin zero and inside the CRC region
    assert foff(CAVE) + len(code) < CRCLEN
    assert all(x == 0 for x in b[foff(CAVE):foff(CAVE)+len(code)+16]), "cave not virgin"
    assert all(x == 0 for x in b[foff(SCRATCH):foff(SCRATCH)+0x100]), "scratch not virgin"

    # 1. class table idx7 (mode 0x0a, "CDC ACM") <- idx4 ("Ambarella Video"),
    #    keeping the mode byte. `t app test usb_rs232 1` selects mode 0x0a and is a
    #    pure flag write, so the connect app never runs and the video preview
    #    pipeline stays up. Swapping idx0 (Mass Storage) instead DOES bring up the
    #    connect app, which tears the pipeline down and leaves the DSP at op-mode 0.
    #    Leaving idx0 stock also keeps USB mass storage working for SD card edits.
    i7, i4 = foff(TBL) + 7*0x18, foff(TBL) + 4*0x18
    assert struct.unpack_from("<I", b, i7)[0] == 0x0a, "idx7 is not mode 0x0a (CDC ACM)"
    assert struct.unpack_from("<I", b, i4)[0] == 7,    "idx4 is not mode 7 (Ambarella Video)"
    b[i7+4:i7+0x18] = stock[i4+4:i4+0x18]
    # ...but point its init at our trampoline so the real UVC descriptors get built
    b[i7+0x10:i7+0x14] = w32(CAVE + syms['uvc_class_init'])

    # --- native UVC: swap the active descriptor set from vendor-0xFF to class-0x0E ---
    got = struct.unpack_from("<I", b, foff(DESCSET_PTR))[0]
    assert got == DESCSET_VENDOR, "descset ptr is 0x%08x, expected 0x%08x" % (got, DESCSET_VENDOR)
    b[foff(DESCSET_PTR):foff(DESCSET_PTR)+4] = w32(DESCSET_UVC)

    # --- advertise exactly one frame: 1280x960 MJPEG (what the encoder actually makes) ---
    fa = foff(FRAME_ARR)
    assert b[fa] == 50 and b[fa+1] == 0x24 and b[fa+2] == 0x07, "not a VS_FRAME_MJPEG at FRAME_ARR"
    assert struct.unpack_from("<H", b, fa+5)[0] == 320, "frame[0] is not 320 wide"
    struct.pack_into("<H", b, fa+5,  1280)        # wWidth
    struct.pack_into("<H", b, fa+7,  960)         # wHeight
    struct.pack_into("<I", b, fa+13, 0x02DC6C00)  # dwMaxBitRate ~48 Mbit/s
    struct.pack_into("<I", b, fa+17, 0x00200000)  # dwMaxVideoFrameBufferSize = 2 MiB
    assert b[fa+50] == 50, "frame[1] missing"
    b[fa+50] = 0                                  # terminate: one frame only

    # --- probe controls: give hosts a buffer big enough for a ~190 KB frame ---
    for rec in PROBE_RECS:
        r = foff(rec)
        b[r+3] = 1                                # bFrameIndex -> 1 (only one frame now)
        struct.pack_into("<I", b, r+0x14, 0x00200000)   # dwMaxVideoFrameSize = 2 MiB

    # 2. UVC-enable getters -> return 1
    for g in GATES:
        b[foff(g):foff(g)+8] = w32(0xE3A00001) + w32(0xE12FFF1E)

    # 3./4. hooks (assert we are displacing the instruction we think we are)
    got = struct.unpack_from("<I", b, foff(LOADER))[0]
    assert got == LOADER_ORIG, "LOADER first insn is 0x%08x, expected 0x%08x" % (got, LOADER_ORIG)
    got = struct.unpack_from("<I", b, foff(BULK_TOP))[0]
    assert got == BULK_TOP_ORIG, "BULK_TOP insn is 0x%08x, expected 0x%08x" % (got, BULK_TOP_ORIG)
    got = struct.unpack_from("<I", b, foff(SKIP_BEQ))[0]
    assert got == SKIP_BEQ_ORIG, "SKIP_BEQ is 0x%08x, expected 0x%08x" % (got, SKIP_BEQ_ORIG)
    b[foff(SKIP_BEQ):foff(SKIP_BEQ)+4] = w32(NOP)   # create the task unconditionally

    got = struct.unpack_from("<I", b, foff(UVCTASK))[0]
    assert got == UVCTASK_ORIG, "UVCTASK is 0x%08x, expected 0x%08x" % (got, UVCTASK_ORIG)
    b[foff(UVCTASK):foff(UVCTASK)+4] = w32(UVCTASK_NEW)

    got = struct.unpack_from("<2I", b, foff(NEVERPC))
    assert got == NEVERPC_ORIG, "NEVERPC is %s, expected %s" % (
        tuple(hex(x) for x in got), tuple(hex(x) for x in NEVERPC_ORIG))
    b[foff(NEVERPC):foff(NEVERPC)+8] = w32(NEVERPC_NEW[0]) + w32(NEVERPC_NEW[1])


    got = struct.unpack_from("<I", b, foff(PWRSAVE))[0]
    assert got == PWRSAVE_ORIG, "PWRSAVE at 0x%08x is 0x%08X, expected 0x%08X" % (
        PWRSAVE, got, PWRSAVE_ORIG)
    b[foff(PWRSAVE):foff(PWRSAVE)+4] = w32(PWRSAVE_NEW)
    b[foff(LOADER):foff(LOADER)+4]     = w32(b_(LOADER,   CAVE + syms['uvc_start_hook']))
    b[foff(BULK_TOP):foff(BULK_TOP)+4] = w32(b_(BULK_TOP, CAVE + syms['uvc_frame_hook']))

    # 5. the blob
    b[foff(CAVE):foff(CAVE)+len(code)] = code

    assert len(b) == PRILEN
    open(out_pri, 'wb').write(bytes(b))

    crc = zlib.crc32(bytes(b[:CRCLEN])) & 0xFFFFFFFF
    if mtd1.lower() in ("none", "-", ""):
        print("ptb  skipped (SD-card update route - the camera writes it)")
    else:
        p = bytearray(open(mtd1, 'rb').read()); assert len(p) == PTBLEN
        old = struct.unpack_from("<I", p, 0x8c)[0]
        p[0x8c:0x90] = w32(crc)
        open(out_ptb, 'wb').write(bytes(p))

        print("\npri  %s  md5=%s" % (out_pri, hashlib.md5(bytes(b)).hexdigest()))
        print("ptb  %s  md5=%s  crc 0x%08x -> 0x%08x" % (out_ptb, hashlib.md5(bytes(p)).hexdigest(), old, crc))
    diff = [i for i in range(PRILEN) if b[i] != stock[i]]
    print("changed bytes: %d" % len(diff))
    runs = []
    for i in diff:
        if runs and i == runs[-1][1] + 1: runs[-1][1] = i
        else: runs.append([i, i])
    for a, z in runs:
        print("   file 0x%06x..0x%06x  VA 0x%08x  (%d B)" % (a, z, BASE + a, z - a + 1))

if __name__ == "__main__":
    main()
