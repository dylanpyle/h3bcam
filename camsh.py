#!/usr/bin/env python3
# Robust busybox-telnet command runner for the debrick-Linux camera at 10.9.9.1.
# Fixes the marker-in-echo bug: the completion marker is split ("__DO""NE__") so
# the literal "__DONE__" appears ONLY in shell OUTPUT, never in the echoed command.
# Usage: camsh.py "<shell command>" [deadline_seconds]
import socket, sys, time, re

HOST = "10.9.9.1"; PORT = 23

def strip_iac(sock, data):
    """Remove telnet IAC option negotiation, refusing every option."""
    out = bytearray(); resp = bytearray(); i = 0
    while i < len(data):
        b = data[i]
        if b == 255 and i + 2 < len(data):          # IAC
            cmd, opt = data[i+1], data[i+2]
            if   cmd == 253: resp += bytes([255,252,opt])   # DO   -> WONT
            elif cmd == 251: resp += bytes([255,254,opt])   # WILL -> DONT
            elif cmd == 254: resp += bytes([255,252,opt])   # DONT -> WONT
            elif cmd == 252: resp += bytes([255,254,opt])   # WONT -> DONT
            i += 3; continue
        elif b == 255 and i + 1 < len(data) and data[i+1] == 255:
            out.append(255); i += 2; continue
        out.append(b); i += 1
    if resp:
        try: sock.sendall(bytes(resp))
        except OSError: pass
    return bytes(out)

def main():
    cmd = sys.argv[1]
    deadline = float(sys.argv[2]) if len(sys.argv) > 2 else 60.0
    s = socket.socket(); s.settimeout(5.0); s.connect((HOST, PORT))
    # Drain initial negotiation + banner for a few seconds; nudge with a newline.
    end = time.time() + 4
    while time.time() < end:
        try: d = s.recv(4096)
        except socket.timeout: d = b""
        if d: strip_iac(s, d)
        else:
            try: s.sendall(b"\n")
            except OSError: pass
    # Send the command with a split completion marker.
    s.sendall((cmd + '; echo "__DO""NE__"$?"__"\n').encode())
    out = b""; s.settimeout(3.0); dl = time.time() + deadline
    while time.time() < dl:
        try: d = s.recv(4096)
        except socket.timeout: continue
        if not d: break
        out += strip_iac(s, d)
        m = re.search(rb"__DONE__(\d+)__", out)
        if m:
            sys.stdout.buffer.write(out[:m.start()]); sys.stdout.flush()
            sys.stderr.write("\n[rc=%d]\n" % int(m.group(1)))
            s.close(); sys.exit(0)
    sys.stdout.buffer.write(out); sys.stdout.flush()
    sys.stderr.write("\n[TIMEOUT: no completion marker in %.0fs]\n" % deadline)
    s.close(); sys.exit(2)

main()
