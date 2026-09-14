import json
import socket

SERVER_ADDR = ("127.0.0.1", 49731)

msg = {"cmd": "quit"}
data = json.dumps(msg).encode("utf-8")

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.sendto(data, SERVER_ADDR)
finally:
    s.close()
