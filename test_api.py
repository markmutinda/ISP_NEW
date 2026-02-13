import socket

target_ip = "127.0.0.1"
target_port = 8728

try:
    s = socket.create_connection((target_ip, target_port), timeout=5)
    print("✅ SUCCESS: Your local Python can reach the MikroTik API through the tunnel!")
    s.close()
except Exception as e:
    print(f"❌ FAILED: {e}")