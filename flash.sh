#!/usr/bin/env bash
# Flash a HERO3 Black pri (mtd4) + ptb (mtd1) pair, with readback verification.
#
# Prereq: the camera must already be running the recovery Linux, i.e. put it in
# bootrom mode (hold top Shutter, plug USB, press front Power, release) and then:
#     sudo ./gpboot --h3b-linux
# Wait until the camera answers at 10.9.9.1, then run this.
#
# Usage: ./flash.sh <pri.bin> <ptb.bin>
#
# BOTH partitions must be written together: the ptb carries a CRC32 over the pri.
set -eu

[ $# -eq 2 ] || { echo "usage: $0 <pri.bin> <ptb.bin>" >&2; exit 2; }
PRI="$1"; PTB="$2"
CAM=10.9.9.1
CS="${CAMSH:-python3 $(dirname "$0")/camsh.py}"

[ -f "$PRI" ] || { echo "!! no such file: $PRI" >&2; exit 1; }
[ -f "$PTB" ] || { echo "!! no such file: $PTB" >&2; exit 1; }
[ "$(stat -c%s "$PRI" 2>/dev/null || stat -f%z "$PRI")" = 8912896 ] || \
    { echo "!! $PRI is not 8912896 bytes - that is not a pri image" >&2; exit 1; }
[ "$(stat -c%s "$PTB" 2>/dev/null || stat -f%z "$PTB")" = 262144 ] || \
    { echo "!! $PTB is not 262144 bytes - that is not a ptb image" >&2; exit 1; }

PIIP=$(ip -4 -o addr show | awk '$4 ~ /^10\.9\.9\./ {split($4,a,"/"); print a[1]; exit}')
[ -n "$PIIP" ] && [ "$PIIP" != "" ] || {
    echo "!! no 10.9.9.x address on this machine." >&2
    echo "   The camera is not running the recovery Linux. Put it in bootrom mode and run:" >&2
    echo "       sudo ./gpboot --h3b-linux" >&2
    exit 1
}
echo "[flash] host=$PIIP  camera=$CAM"
ping -c1 -W2 $CAM >/dev/null || { echo "!! camera not reachable at $CAM" >&2; exit 1; }

push() {  # push <localfile> <remotepath> <port>
  local f="$1" r="$2" p="$3" md rmd
  md=$(md5sum "$f" | cut -d' ' -f1)
  echo "[flash] sending $(basename "$f") -> $r  (md5 $md)"
  ( nc -N -l -p "$p" < "$f" & ) ; sleep 1
  $CS "nc $PIIP $p > $r" 180 >/dev/null
  rmd=$($CS "md5sum $r" 60 | grep -oE '^[0-9a-f]{32}')
  [ "$rmd" = "$md" ] || { echo "!! md5 MISMATCH sending $r ($rmd)" >&2; exit 1; }
}

burn() {  # burn <remotefile> <mtdnum> <expected-md5>
  local r="$1" m="$2" md="$3" rb
  echo "[flash] erasing  /dev/mtd$m"; $CS "flash_eraseall -q /dev/mtd$m" 300 >/dev/null
  echo "[flash] writing  /dev/mtd$m"; $CS "nandwrite -p /dev/mtd$m $r" 600 >/dev/null
  echo "[flash] verifying /dev/mtd$m"; $CS "nanddump -f /tmp/rb$m /dev/mtd$m" 600 >/dev/null
  rb=$($CS "md5sum /tmp/rb$m" 60 | grep -oE '^[0-9a-f]{32}')
  echo "[flash] mtd$m readback md5: $rb  (want $md)"
  [ "$rb" = "$md" ] || {
      echo "!! READBACK MISMATCH on mtd$m - DO NOT POWER-CYCLE, reflash now" >&2; exit 1; }
  $CS "rm -f /tmp/rb$m" 30 >/dev/null
}

PRIMD=$(md5sum "$PRI" | cut -d' ' -f1)
PTBMD=$(md5sum "$PTB" | cut -d' ' -f1)

push "$PTB" /tmp/ptb.bin 9101; burn /tmp/ptb.bin 1 "$PTBMD"; $CS "rm -f /tmp/ptb.bin" 30 >/dev/null
push "$PRI" /tmp/pri.bin 9102; burn /tmp/pri.bin 4 "$PRIMD"; $CS "rm -f /tmp/pri.bin" 30 >/dev/null

echo
echo "[flash] DONE - both partitions written and readback-verified."
echo "[flash] Now: unplug USB, power the camera on with the FRONT BUTTON,"
echo "[flash]      wait for 3 beeps + ~15 s, then plug USB in."
