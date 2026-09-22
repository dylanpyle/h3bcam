@ ============================================================================
@ HERO3 Black HD3.03.03.00 - live UVC webcam
@
@ Stock firmware contains a complete USB Video Class implementation built as a
@ factory test fixture: uvc_bulk_dummy_task streams a static JPEG that
@ uvc_bulk_load_frame_data reads off the SD card. Everything else - descriptors,
@ PROBE/COMMIT, the bulk endpoint, FID toggling, the >100KB chunked transfer -
@ is real and works. This repoints that fixture at the live MJPEG encoder.
@
@ The recorder is not involved: its context is NULL in plain video preview.
@ ============================================================================
.syntax unified
.arm

@ ---- scratch: the only state we keep ----
.equ CTX,          0xC061B000
.equ CTX_SPAWNED,  0x00          @ worker spawned once
.equ CTX_TASKID,   0x04
.equ CTX_STACK,    0x08
.equ CTX_TCREATE,  0x10          @ task-create descriptor
.equ CTX_HANDLE,   0x34          @ iavobj handle, 1-based
.equ CTX_PENDING,  0x4C          @ a published frame is awaiting release
.equ CTX_ARMED,    0x54          @ encoder is running

.equ SETUP,        0xC061B100    @ 0x34-byte dsp_jpeg_encode_setup struct
.equ PARAMS,       0xC061B140    @ 0x18-byte dsp_mjpeg_encode struct
.equ STOPST,       0xC061B180    @ 8-byte dsp_vid_encode_stop struct

.equ PUMP_CTX,     0xC0DD0A00    @ +0x00 streaming flag, +0x10 frame src, +0x14 len
.equ IAVOBJ_TBL,   0xC08E5E6C    @ 9 slots x 0x48
.equ IAVOBJ_RESET, 0xC0620E60    @ != 0 -> iavobj_create would wipe all nine slots

.equ FN_TASKCREATE, 0xC011A228
.equ FN_DLYTSK,     0xC010B950
.equ FN_ALLOC,      0xC011CDAC
.equ FN_OPMODE,     0xC0234DB8   @ () -> 5 = DSP_OP_H264ENC (video preview live)
.equ FN_ENCSTATE,   0xC02551EC   @ (0, codec) -> 0 or 2 = encoder ready
.equ FN_IAVOBJ_NEW, 0xC028DCDC   @ (bs_size, desc_size) -> 1-based handle
.equ FN_JPEG_SETUP, 0xC0284C88   @ (setup*, 0)
.equ FN_MJPEG_ENC,  0xC0284EF4   @ (params*)
.equ FN_DESC_GET,   0xC028F204   @ (handle, 1) -> descriptor*
.equ FN_DESC_REL,   0xC028F368   @ (handle)
.equ FN_ENC_STOP,   0xC02383A8   @ (stop*)
.equ FN_BUILD_DESC, 0xC05193AC   @ uvc_build_config_descriptors()
.equ FN_UVC_INIT,   0xC045614C   @ the stock class-7 ("Ambarella Video") init

.equ LOADER_NEXT,   0xC0573500
.equ BULK_NEXT,     0xC05733A4

.equ MJPEG_BS,      0x800000     @ 8 MB bitstream ring, ~50 frames at ~165 KB.
                                 @ Must be a multiple of 0x4000 and <= 0x4D00000.
.equ MJPEG_DESC,    0x4000       @ 256 descriptors. Must be a multiple of 0x400.
.equ WORKER_PRIO,   120          @ below every DSP-touching task (recorder = 100)
.equ WORKER_STACK,  0x2000

@ --------------------------------------------------------------------------
@ Class init - installed as class-table idx7's init pointer.
@
@ Stock ships two video descriptor sets. The active one is vendor class 0xFF,
@ so no OS UVC driver binds to it. A proper UVC 1.0 class-0x0E bulk set exists
@ at 0xC07F9158, but its config descriptors are assembled at runtime by
@ FUN_c05193ac, whose only caller has no references anywhere in the image.
@ Build them, then run the stock init, which picks up the descriptor-set
@ pointer redirected in .rodata.
@ --------------------------------------------------------------------------
.global uvc_class_init
uvc_class_init:
    push    {r4, lr}
    ldr     ip, =FN_BUILD_DESC
    blx     ip
    ldr     ip, =FN_UVC_INIT
    blx     ip                      @ pass its status through
    pop     {r4, pc}

@ --------------------------------------------------------------------------
@ Hook 1 - replaces the first instruction of FUN_c05734fc (the COMMIT handler).
@ --------------------------------------------------------------------------
.global uvc_start_hook
uvc_start_hook:
    push    {r0-r3, lr}
    ldr     r0, =CTX
    ldr     r1, [r0, #CTX_SPAWNED]
    cmp     r1, #0
    bne     1f
    mov     r1, #1
    str     r1, [r0, #CTX_SPAWNED]
    bl      spawn_worker
1:  pop     {r0-r3, lr}
    push    {r4, r5, r6, r7, lr}    @ the displaced instruction
    ldr     pc, =LOADER_NEXT

@ --------------------------------------------------------------------------
spawn_worker:
    push    {r4, lr}
    ldr     r4, =CTX
    sub     sp, sp, #8
    mov     r0, #1
    str     r0, [sp]
    add     r0, r4, #CTX_STACK
    add     r1, r4, #CTX_TASKID
    mov     r2, #WORKER_STACK
    mov     r3, #8
    ldr     ip, =FN_ALLOC
    blx     ip
    add     sp, sp, #8
    cmp     r0, #0
    blt     9f
    ldr     r0, [r4, #CTX_STACK]
    cmp     r0, #0
    beq     9f
    add     r1, r4, #CTX_TCREATE
    mov     r2, #0
    str     r2, [r1, #0x04]
    mov     r2, #2
    strh    r2, [r1, #0x00]         @ flags bit1 = auto-start
    ldr     r2, =worker_main
    str     r2, [r1, #0x08]
    mov     r2, #WORKER_PRIO
    strh    r2, [r1, #0x0C]
    mov     r2, #WORKER_STACK
    str     r2, [r1, #0x10]         @ stack size
    str     r0, [r1, #0x14]         @ stack base
    mov     r0, r1
    ldr     ip, =FN_TASKCREATE
    blx     ip
9:  pop     {r4, pc}

@ --------------------------------------------------------------------------
@ Worker: wait for the DSP preview pipeline, create the bitstream object, then
@ follow the host - arm the encoder while it streams, stop when it stops.
@ --------------------------------------------------------------------------
worker_main:
1:  mov     r0, #500
    ldr     ip, =FN_DLYTSK
    blx     ip
    ldr     ip, =FN_OPMODE
    blx     ip
    cmp     r0, #5
    bne     1b

    mov     r0, #0                  @ encoder must report ready for codec 0x11
    mov     r1, #0x11
    ldr     ip, =FN_ENCSTATE
    blx     ip
    cmp     r0, #0
    cmpne   r0, #2
    bne     1b

    ldr     r0, =IAVOBJ_RESET       @ non-zero => iavobj_create wipes all nine
    ldr     r0, [r0]                @ slots, which boot-loops the camera
    cmp     r0, #0
    bne     9f

    ldr     r0, =MJPEG_BS
    mov     r1, #MJPEG_DESC
    ldr     ip, =FN_IAVOBJ_NEW
    blx     ip
    ldr     r1, =CTX
    str     r0, [r1, #CTX_HANDLE]   @ 1-based; 0 = failed
    ands    r4, r0, #0xFF
    beq     9f

    @ ---- dsp_jpeg_encode_setup. Field names are the firmware's own DSP_LOG
    @      strings in FUN_c0284c88. ----
    ldr     r5, =SETUP
    mov     r0, #0
    mov     r2, #0x34
2:  str     r0, [r5, r2]
    subs    r2, r2, #4
    bpl     2b
    ldr     r0, =0x00001100
    str     r0, [r5, #0x00]         @ chan 0 | codec 0x11 (JPEG) << 8
    mov     r0, #1
    str     r0, [r5, #0x04]         @ chroma_format 1 -> 4:2:0
    mov     r0, #30
    str     r0, [r5, #0x1C]         @ frame_rate - timestamp metadata, not a throttle
    mov     r0, #1
    str     r0, [r5, #0x20]         @ bit0 = IS_MJPEG. With 0 you get ONE still.
    str     r4, [r5, #0x30]         @ iavobj handle
    mov     r0, r5
    mov     r1, #0
    ldr     ip, =FN_JPEG_SETUP
    blx     ip

    @ Deliberately not done here: iavobj frame/memrunout threshold registration,
    @ and interval-capture frame credit. Both need a consumer we do not have
    @ (the stock recorder) and destabilise the session without one.

    @ ---- run the encoder only while the host is streaming ------------------
    @ encode_duration = 0xFFFFFFFF runs forever, ~5 MB/s into the ring. Nothing
    @ drains it unless the host is streaming, because the release hook lives in
    @ the bulk task and that task only exists while streaming.
    @ *(u8*)PUMP_CTX is the streaming flag: set by uvc_bulk_dummy_task_create,
    @ cleared by the stop path at 0xC057389C.
30: ldr     r5, =PUMP_CTX
31: ldrb    r0, [r5]
    cmp     r0, #0
    bne     32f
    ldr     r1, =CTX
    mov     r0, #0
    str     r0, [r1, #CTX_ARMED]
    mov     r0, #100
    ldr     ip, =FN_DLYTSK
    blx     ip
    b       31b

32: ldr     r5, =PARAMS
    mov     r0, #0
    mov     r2, #0x18
33: str     r0, [r5, r2]
    subs    r2, r2, #4
    bpl     33b
    ldr     r0, =0x00001100
    str     r0, [r5, #0x00]         @ chan | codec<<8
                                    @ framerate_control_M/N at +0x14/+0x15 stay 0/0;
                                    @ the DSP never reads them.
    mvn     r0, #0
    str     r0, [r5, #0x0C]         @ start_encode_frame_no = -1, as Ambarella's own
                                    @ driver uses. 0 is an absolute frame number far
                                    @ in the past on a DSP that has been previewing.
    mvn     r0, #0
    str     r0, [r5, #0x10]         @ encode_duration = forever
    mov     r0, r5
    ldr     ip, =FN_MJPEG_ENC
    blx     ip
    ldr     r1, =CTX
    mov     r0, #1
    str     r0, [r1, #CTX_ARMED]

    ldr     r5, =PUMP_CTX           @ wait for the host to stop
34: ldrb    r0, [r5]
    cmp     r0, #0
    beq     35f
    mov     r0, #200
    ldr     ip, =FN_DLYTSK
    blx     ip
    b       34b

35: ldr     r1, =CTX                @ host stopped - stop the encoder so the ring
    mov     r0, #0                  @ stops filling with nobody draining it
    str     r0, [r1, #CTX_ARMED]    @ frame hook stands down first
    str     r0, [r1, #CTX_PENDING]
    ldr     r5, =STOPST
    ldr     r0, =0x00001100
    str     r0, [r5, #0x00]         @ chan | codec<<8
    mov     r0, #0
    str     r0, [r5, #0x04]         @ stop_method
    mov     r0, r5
    ldr     ip, =FN_ENC_STOP
    blx     ip
    b       30b

9:  ldr     r0, =10000              @ park; a sleeping prio-120 task costs nothing
    ldr     ip, =FN_DLYTSK
    blx     ip
    b       9b

@ --------------------------------------------------------------------------
@ Hook 2 - replaces `ldrb r0,[r7]` at the top of uvc_bulk_dummy_task's loop.
@ r7 = PUMP_CTX and must be preserved.
@
@ Uses the firmware's own descriptor API: FUN_c028f204 dcache-invalidates the
@ frame and handles ring wrap, FUN_c028f368 releases it so the encoder can keep
@ going. Without the release the ring fills and the encoder stops.
@ --------------------------------------------------------------------------
.global uvc_frame_hook
uvc_frame_hook:
    push    {r0-r6, lr}
    ldr     r6, =CTX
    ldr     r0, [r6, #CTX_ARMED]
    cmp     r0, #0
    beq     19f
    ldr     r4, [r6, #CTX_HANDLE]
    ands    r4, r4, #0xFF
    beq     19f

    ldr     r0, [r6, #CTX_PENDING]  @ release what we published last pass; the bulk
    cmp     r0, #0                  @ task has finished sending it by now
    beq     14f
    mov     r0, #0
    str     r0, [r6, #CTX_PENDING]
    mov     r0, r4
    ldr     ip, =FN_DESC_REL
    blx     ip

14: @ ---- drain the backlog -------------------------------------------------
    @ The sensor runs at 48 fps and the encoder follows it, but the bulk task
    @ paces itself to ~30 fps. A one-release-per-pass consumer loses ~18
    @ descriptors a second and the ring overflows. Release everything except the
    @ newest couple each pass, so we always send a fresh frame.
    sub     r0, r4, #1
    add     r0, r0, r0, lsl #3      @ (hnd-1) * 9
    ldr     r1, =IAVOBJ_TBL
    add     r5, r1, r0, lsl #3      @ slot = tbl + (hnd-1)*0x48
    mov     r3, #0
12: ldr     r0, [r5, #0x40]         @ avail_desc_count
    cmp     r0, #3
    blt     10f
    cmp     r3, #0x40               @ never spin more than a ring's worth
    bge     10f
    add     r3, r3, #1
    mov     r0, r4
    ldr     ip, =FN_DESC_REL
    blx     ip
    b       12b

10: mov     r0, r4
    mov     r1, #1
    ldr     ip, =FN_DESC_GET
    blx     ip
    cmp     r0, #0
    beq     19f
    cmn     r0, #1
    beq     19f
    ldr     r2, [r0, #0x10]         @ frame start address
    ldr     r3, [r0, #0x14]
    lsr     r3, r3, #8              @ frame length
    cmn     r2, #1                  @ sentinel: no frame ready
    beq     19f

    @ EOS: the DSP writes size == 0x00FFFFFF as the last descriptor of a session.
    @ It must be released or the read pointer can never advance past it.
    ldr     r5, =0x00FFFFFF
    cmp     r3, r5
    bne     23f
    mov     r0, r4
    ldr     ip, =FN_DESC_REL
    blx     ip
    mov     r0, #0
    str     r0, [r6, #CTX_PENDING]
    b       18f

23: cmp     r3, #0
    beq     19f
    ldr     r5, =0x00400000         @ reject absurd lengths
    cmp     r3, r5
    bhi     19f
    ldr     r5, =0xC0000000         @ frame must live in DRAM
    cmp     r2, r5
    blo     19f

    @ ---- reject frames that straddle the end of the bitstream ring ---------
    @ The bulk task does ONE memcpy of `len` bytes from `addr`. A wrapped frame
    @ has its tail back at bits_buf_base, so a single copy runs off the end of
    @ the buffer. One frame per lap is cheap to drop.
    @ slot+0x08 = bits_buf_base, slot+0x0C = bits_buf_lim (inclusive).
    sub     r0, r4, #1
    add     r0, r0, r0, lsl #3
    ldr     r1, =IAVOBJ_TBL
    add     r0, r1, r0, lsl #3
    ldr     r1, [r0, #0x08]
    cmp     r2, r1
    blo     15f                     @ before the buffer -> bogus
    ldr     r1, [r0, #0x0C]
    add     ip, r2, r3
    sub     ip, ip, #1              @ last byte of the frame
    cmp     ip, r1
    bls     16f                     @ fits in one run -> send it
15: mov     r0, r4                  @ release now so the ring keeps moving
    ldr     ip, =FN_DESC_REL
    blx     ip
    mov     r0, #0
    str     r0, [r6, #CTX_PENDING]
    b       18f

16: @ ---- publish -----------------------------------------------------------
    @ Do not touch bits_rp here. FUN_c028EC54 advances bits_wp with
    @ `addr + len - 1`, exactly as FUN_c028f368 advances bits_rp on release -
    @ writing it from this side just fights the release function for the pointer.
    ldr     r5, =PUMP_CTX
    str     r2, [r5, #0x10]
    str     r3, [r5, #0x14]
    mov     r0, #1
    str     r0, [r6, #CTX_PENDING]  @ release it next pass
    b       18f

19: @ ---- no frame this pass ------------------------------------------------
    @ Empty passes are normal: the sensor runs faster than we send. Point the
    @ bulk task at a small dummy buffer rather than leaving it on the frame we
    @ just released, which the encoder is free to overwrite.
    ldr     r5, =PUMP_CTX
    str     r6, [r5, #0x10]
    mov     r0, #0x100
    str     r0, [r5, #0x14]

18: pop     {r0-r6, lr}
    ldrb    r0, [r7]                @ the displaced instruction
    ldr     pc, =BULK_NEXT
