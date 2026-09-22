# Recovery

The Ambarella A7 runs a USB bootrom **before** it touches NAND, so a bad
firmware image can't lock you out — the chip will still talk over USB even when
the firmware it's holding is garbage.

You'll need a Linux machine and
[`gopro-usb-tools`](https://github.com/evilwombat/gopro-usb-tools):

```sh
git clone https://github.com/evilwombat/gopro-usb-tools && cd gopro-usb-tools && make
```

**Enter bootrom mode.** With the camera **off**, hold the **shutter** button,
wait a few seconds, **plug USB into the Linux machine**, press the **power**
button, then release shutter. The rear LED should be dimly lit and `lsusb`
should show `4255:0003`. If the camera powers on normally instead, it didn't
take — repeat.

**Boot a recovery Linux on the camera itself.** This runs entirely in RAM and
never touches the installed firmware, so it works no matter how broken that
firmware is:

```sh
sudo ./gpboot --h3b-linux
```

Give it a few seconds. The camera comes up as a USB network device and answers
at **10.9.9.1** — that's how the tools below reach it.

### Backing up your own firmware

The camera's flash is split into partitions. Two matter here: **`mtd4`** (`pri`,
the main ARM firmware, 8912896 bytes) and **`mtd1`** (`ptb`, the partition table,
262144 bytes, which holds a checksum over `pri`).

With the recovery Linux running, dump both — these commands run **on the camera**,
over its shell:

```sh
python3 camsh.py "nanddump -f /tmp/mtd4 /dev/mtd4"
python3 camsh.py "nanddump -f /tmp/mtd1 /dev/mtd1"
```

They land in the camera's RAM disk, so copy them to your machine before you
power-cycle it or they're gone. The recovery Linux has `netcat`, so listen on
your machine and have the camera dial out — the same trick `flash.sh` uses to
send files the other way.

First find your own address on the link to the camera:

```sh
ip -4 -o addr show | grep 10\.9\.9\.     # e.g. 10.9.9.2
```

Then, for each partition — start the listener, then trigger the send:

```sh
nc -l -p 9000 > mtd4 &
python3 camsh.py "nc [your 10.9.9.x address] 9000 < /tmp/mtd4"

nc -l -p 9001 > mtd1 &
python3 camsh.py "nc [your 10.9.9.x address] 9001 < /tmp/mtd1"
```

Check they arrived intact — sizes must be exactly 8912896 and 262144, and the
md5s must match what the camera reports:

```sh
ls -l mtd4 mtd1
md5sum mtd4 mtd1
python3 camsh.py "md5sum /tmp/mtd4 /tmp/mtd1"
```

Keep both files somewhere safe. **`mtd4` alone is not enough** — the two have to
be written back together.

### Writing partitions directly

With the recovery Linux running, `flash.sh` pushes a pair of images to the
camera and writes them to flash, verifying the readback md5 of each:

```sh
./flash.sh <pri.bin> <ptb.bin>
```

To restore your backup, pass the files you saved above. To install h3bcam this
way instead of by SD card — useful if the camera is too broken to run an update
— build from *your own* dump rather than GoPro's package:

```sh
python3 build.py mtd4 mtd1 uvclive_pri.bin uvclive_ptb.bin
./flash.sh uvclive_pri.bin uvclive_ptb.bin
```

> [!WARNING]
> Both partitions must be written together. `ptb` holds a CRC32 over the first
> `0x706004` bytes of `pri`, so a mismatched pair won't boot.
>
> Don't write an image built with `--extract-pri` this way. That path pads the
> tail with `0xFF` where a real partition has data — it's fine inside the SD-card
> package, which only carries the checksummed region, but `flash.sh` writes the
> whole partition.

