# How it works

Reverse-engineering notes for HERO3 Black firmware **HD3.03.03.00** (Ambarella A7, ARM RTOS).
Virtual addresses; **file offset = VA − 0xC0100000**.

## The model

Stock firmware ships a complete USB Video Class implementation, built as a factory test fixture that
streams a static JPEG off the SD card. Its own symbols name it: `uvc_bulk_dummy_task_create`,
`uvc_bulk_load_frame_data`, `a:\uvc_1280x720_1.jpg`.

Descriptors, enumeration, PROBE/COMMIT, the bulk endpoint, UVC payload headers, frame-ID toggling and
a chunked path for frames over 100 KB are all present and working. The DSP's MJPEG encoder is fully
implemented and emits standards-compliant JFIF. A second, spec-compliant **UVC class-0x0E descriptor
set** sits at `0xC07F9158`, assembled by `FUN_c05193ac`, whose only caller is unreachable.

The bulk task reads its frame from two words in RAM:

```c
src = *(uint32_t *)(0xC0DD0A00 + 0x10);   // frame address
len = *(uint32_t *)(0xC0DD0A00 + 0x14);   // frame length, re-read every ~33 ms
```

Stock fills that buffer from a **file**. We fill it from the **encoder**.

## The injected worker

Hooked onto the UVC COMMIT handler, running at **priority 120** — below every DSP-touching task,
because the DSP batch-command writer does an unprotected read-modify-write on a shared buffer and the
priority-10 UVC control task can preempt it mid-update.

```c
hnd = iavobj_create(0x800000, 0x4000);   // 8 MB bitstream ring, 256 descriptors
dsp_jpeg_encode_setup(&setup, 0);        // setup+0x20 bit0 = IS_MJPEG (0 yields ONE still)
for (;;) {
    wait until *(uint8_t *)0xC0DD0A00;   // host started streaming
    dsp_mjpeg_encode(&params);           // params+0x10 = 0xFFFFFFFF = run until stopped
    wait until !*(uint8_t *)0xC0DD0A00;  // host stopped
    dsp_vid_encode_stop(&stop);
}
```

Gating on the streaming flag matters: with `encode_duration = 0xFFFFFFFF` the DSP writes ~5 MB/s
forever and nothing drains the ring unless the host is streaming.

Per frame, inside the bulk task's loop:

```c
release the frame published last pass;                        // FUN_c028f368, advances bits_rp
while (avail_desc_count > 3) release();                       // sensor is 48 fps, we send ~30
desc = FUN_c028f204(hnd, 1);                                  // invalidates D-cache, handles wrap
addr = desc[0x10];  len = desc[0x14] >> 8;
if (addr + len - 1 > bits_buf_lim) { release(); continue; }    // would overrun the ring - drop
*(uint32_t *)(0xC0DD0A00 + 0x10) = addr;
*(uint32_t *)(0xC0DD0A00 + 0x14) = len;
```

Use the firmware's own API (`FUN_c028f204` / `FUN_c028f368`), not raw ring scanning: because setup
goes through the real `dsp_jpeg_encode_setup`, the object is registered with purpose 3 (PIC_ENCODE),
so `parse_enc_status` drives `iavobj_update_one_desc` for it — advancing the pointers *and* applying
the `| 0xC0000000` fixup. Addresses out of `FUN_c028f204` are already virtual.

## The patches

| # | address | what |
|---|---|---|
| 1 | `0xC07AD218` | Class-table slot 7 → "Ambarella Video", init → our trampoline |
| 2 | `0xC0456128`, `0xC0456140`, `0xC0517A4C` | UVC-enable getters → `return 1` |
| 3 | `0xC0573840` | `beq` → `nop` |
| 4 | `0xC030B830` | → `mov r0,#0; bx lr` |
| 5 | `0xC07DB2F8` | Active descriptor set → the class-0x0E set |
| 6 | `0xC07F8FF8` | One frame: 1280×960, 2 MiB buffer |
| 7 | `0xC0804C68`, `0xC0804C84`, `0xC0804CA0`, `0xC0804CBC` | `dwMaxVideoFrameSize` → 2 MiB |
| 8 | `0xC0517808` | `mov r0,#0` → `b 0xC0517F70` |
| 9 | `0xC0333618` | `b 0xC01A991C` → `bx lr` |
| 10 | `0xC061A000` | The blob |

**Patch 3 — streaming is gated on a file load.**
```
c0573838  bl   FUN_c05734fc   ; load a:\uvc_<res>_1.jpg
c057383c  cmp  r0, #0
c0573840  beq  0xc0573870     ; load failed => skip everything below
c0573844..64                  ; ...create uvc_bulk_dummy_task
c0573868  mov  r0, #1
c057386c  strb r0, [r4]       ; ...and set the streaming flag
```
In USB mode the card isn't mounted as `a:`, so the load always fails. Without this patch you get a
perfect control plane and zero bytes on the endpoint.

**Patch 4 — stay in preview.** Plugging in USB launches the USB app, which tears down the video
pipeline: the camera still reports `appmode = "video"` while the DSP sits at op-mode 0 with a NULL
recorder context. `FUN_c030b830` posts the event its callers use to decide whether to switch apps.

**Patch 8 — the control plane.** GoPro compiled `uvc_task_init` down to a `mov r0,#0` stub and left
the real body orphaned at `0xC0517F70`. It creates the Device/Class Request tasks that service UVC
class requests addressed to an entity. Without it Linux reaches `Found UVC 1.00 device` and then
times out on every control (`-110`, then `-71`).

**Patch 9 — the five-minute cutoff.** A 1 Hz countdown at `0xC03335AC` loads a hard-coded **300**
(`mov r1,#0x12C` at `0xC03335C4`) and on expiry calls `app_switch(app_misc_powersaving)`. Entering
any app re-delivers the USB-connect message, which reaches `app_usb_msc`, which overrides the
descriptors to `2672:0004` "GoPro"/"Storage" and re-inits the gadget as Mass Storage — so the video
device vanishes mid-stream. Armed at boot whenever `app_pref_user->powersaving == 1`, the ROM default
(`0xC0620FE0`: `poweroff=0`, `powersaving=1`). `pop {r4,r5,r6,lr}` has already run at `0xC0333614`,
so `bx lr` returns cleanly: the timer still unregisters, only the app switch is gone.

## Resolution

The frame array at `0xC07F8FF8` advertises exactly one format, 1280×960, and the four probe records
at `0xC0804C68`/`0xC0804C84`/`0xC0804CA0`/`0xC0804CBC` carry a matching `dwMaxVideoFrameSize`. The
camera must be set to **960p video mode** so the encoder produces frames of that geometry; other
modes fail in various ways.

Retargeting means changing all three together — the frame array, the probe records, and the ring
size in `patch.s` if the frames get materially larger.

**The shipped `autoexec.ash` handles this**, so the camera's menu settings do not matter:

```
t app video_settings res 960     # tokens 1440|1080|960|720, parser at 0xC02976F4, 960 -> index 6
t app video_settings fps 48      # tokens 12|12.5|15|24|25|30|48|50|60|100|120|240
t app appmode photo              # the settings alone are NOT enough - see below
t app appmode video
```

48 fps is what the drain thresholds and the 8 MB ring in `patch.s` are sized for. At 100 fps the ring
wraps twice as fast and ring-straddling frames are dropped twice as often, so pinning it keeps
behaviour the same whatever the user had selected.

Setting the preference on its own is insufficient: the settings page reflects it immediately, but the
*running* pipeline stays on whatever mode the camera booted with, and video never comes up. The mode
cycle forces the reconfigure. Confirmed on hardware from cameras booted in 4K and in 100 fps.

## Key addresses

```
0xC0DD0A00 +0x00  u8  streaming flag (set by uvc_bulk_dummy_task_create)
           +0x10  u32 frame address     +0x14  u32 frame length
0xC07AD170        USB class table, 8 entries x 0x18:
                  {u8 mode, u8 disabled, u16 pad, char *name, u32 flags, u32 mask, init*, deinit*}
                  mode 1 = Mass Storage, 7 = Ambarella Video, 0x0A = CDC ACM
0xC08E5E6C        iavobj table, 9 x 0x48; +0x08 bits_base +0x14 bits_rp +0x40 avail_desc +0x44 purpose
0xC09E0140        -> app_pref_user_t (253 B). +0x44 usb_mode +0x48 poweroff +0x4A powersaving
0xC08E6660        app_status. +0x49 cur_app_mode +0x4A return_to_mode +0x4B flags
0xC0620FE0        app_pref_user_t ROM defaults
0xC0334D34        whole-struct dump of app_pref_user_t (offset -> field name)

FUN_c028f204(hnd,1)   fetch descriptor      FUN_c028f368(hnd)   release
FUN_c0234db8()        DSP op-mode (5 = H264ENC, correct for MJPEG)
FUN_c02551ec(ch,st)   encoder state (0 or 2 = ready)
0xC034D644            usb_class_init(mode)   0xC01A991C  app_switch(app_id)
0xC0338AD4(n)         play n beeps - only 1, 3 and 7 exist; 7 is power-off
```

⚠ **Never overwrite a class-table entry's `mode` byte** — `usb_class_init` linear-searches on it.

## Firmware package format

Sections, each a 0x100-byte header followed by its body:

```
+0x00 u32 crc32     zlib.crc32 of the body
+0x0C u32 img_len
+0x10 u32 mem_addr
+0x18 u32 magic     0xA324EB90
```

The section at `mem_addr = 0xC0100000` is byte-for-byte the `pri` partition, `img_len = 0x706004`.
Its header CRC is the same value the partition table holds at `ptb+0x8c`, which is why an SD-card
update needs only `pri` — the camera writes `ptb` itself.
