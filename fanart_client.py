import json
import socket
import sys
from datetime import datetime

SERVER_ADDR = ("127.0.0.1", 49731)

def main():
    # argv: <system> <rompath> <gamename...>
    args = sys.argv[1:]
    if not args:
        return

    system = args[0]
    rom = args[1] if len(args) > 1 else ""
    name = " ".join(args[2:]) if len(args) > 2 else ""

    msg = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "system": system,
        "rom": rom,
        "name": name,
    }

    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.sendto(data, SERVER_ADDR)
    finally:
        s.close()

if __name__ == "__main__":
    main()
