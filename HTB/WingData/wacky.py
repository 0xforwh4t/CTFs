
import hashlib

target = "32940de----------------------------------f8a503ca"

with open("/usr/share/wordlists/rockyou.txt", "r", errors="ignore") as f:
    for line in f:
        pw = line.strip()
        h = hashlib.sha256((pw + "WingFTP").encode()).hexdigest()
        if h == target:
            print("FOUND:", pw)
            break
