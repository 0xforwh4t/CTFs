
# 🧠 WingData – Advanced Exploit Research (HTB / CTF)

> A deep-dive security research repository demonstrating **Tar Symlink Path Traversal → Root Privilege Escalation**, written in a style inspired by top-tier researchers.

---

# 📌 Executive Summary

| Field                | Value                        |
| -------------------- | ---------------------------- |
| Target               | wingdata.htb                 |
| Vulnerability        | Tar Symlink Path Traversal   |
| Impact               | Arbitrary File Write         |
| Privilege Escalation | Root (via sudoers overwrite) |
| Difficulty           | Medium                       |

---

# 🎯 Attack Chain Overview

```
User Input (Tar Archive)
        ↓
restore_backup_clients.py (runs as root)
        ↓
Unsafe tar extraction
        ↓
Symlink traversal escape
        ↓
Write → /etc/sudoers
        ↓
Passwordless sudo
        ↓
ROOT SHELL
```

---

# 🔍 Phase 1 – Enumeration

## Subdomain Discovery

```bash
ffuf -u http://wingdata.htb -H "Host: FUZZ.wingdata.htb" \
-w /usr/share/seclists/Discovery/DNS/subdomains-top1million-5000.txt
```

### Key Finding

```
ftp.wingdata.htb
```

---

# 🔎 Phase 2 – Foothold Analysis

The system exposes a backup/restore mechanism:

```
/opt/backup_clients/restore_backup_clients.py
```

### Critical Observations

* Runs with **sudo/root privileges**
* Accepts user-controlled `.tar` files
* Uses Python `tarfile.extractall()` (unsafe by default)

---

# ⚠️ Phase 3 – Vulnerability Analysis

## Root Cause: Insecure Tar Extraction

Python's tarfile module is vulnerable when:

* Symlinks are not filtered
* Paths are not validated
* Extraction occurs with elevated privileges

### Vulnerability Class

* CWE-22: Path Traversal
* CWE-59: Link Following

---

# 💥 Phase 4 – Exploit Development

## Exploit Strategy

1. Create deep directory nesting
2. Abuse symlink chaining
3. Escape extraction directory
4. Redirect write into `/etc`
5. Overwrite `sudoers`

---

## 🔬 Exploit Internals

### 1. Directory Depth Trick

```python
comp = 'd' * 247
```

➡️ Forces long path resolution and bypasses naive checks

---

### 2. Symlink Ladder

```python
b.linkname = comp
```

➡️ Each step creates a chained traversal path

---

### 3. Escape Vector

```python
e.linkname = linkpath + "/../../../../../../../etc"
```

➡️ Breaks out of extraction directory

---

### 4. Arbitrary File Write

```python
f.linkname = "escape/sudoers"
```

➡️ Targets `/etc/sudoers`

---

### 5. Privilege Injection

```python
payload = b"wacky ALL=(ALL) NOPASSWD: ALL\n"
```

➡️ Grants full root access

---

# 🛠 Full Exploit

See [`exploit.py`](./backup.py)

---

# 🚀 Phase 5 – Exploitation

```bash
python3 exploit.py
```

### Expected Output

```
root
```

---

# 📸 Evidence

## Enumeration

![ffuf](screenshots/ffuf_enum.png)

## Exploit Execution

![exploit](screenshots/exploit_run.png)

## Root Shell

![root](screenshots/root_shell.png)

---

# 🧠 Why This Exploit Works

## Trust Boundary Violation

User-controlled archive → executed by root process

---

## Missing Security Controls

* ❌ No symlink filtering
* ❌ No path canonicalization
* ❌ No privilege separation

---

## Dangerous Default

```python
tar.extractall()
```

➡️ Extracts everything blindly

---

# 🛡️ Mitigation Strategies

## 1. Safe Extraction

```python
for member in tar.getmembers():
    if member.issym() or member.islnk():
        continue
```

---

## 2. Path Validation

```python
real_path = os.path.realpath(member.name)
if not real_path.startswith(safe_dir):
    raise Exception("Path Traversal Detected")
```

---

## 3. Drop Privileges

* Never extract archives as root

---

## 4. Use Sandboxing

* chroot / containers

---

# 🔬 Detection & Hunting

### Indicators of Compromise

* Unexpected tar extraction behavior
* Modified `/etc/sudoers`
* Suspicious backup archives

---

# 📚 References

* Python tarfile security advisory
* CWE-22, CWE-59
* Zip Slip research

---

# ⚡ Reproducibility

```bash
git clone https://github.com/yourusername/wingdata-advanced
cd wingdata-advanced
python3 exploit.py
```

---

# 🧾 License

MIT License

---

# ⚠️ Disclaimer

This research is intended for **educational purposes and authorized environments only**.

---

# ⭐ Author Notes

This repo is structured to reflect real-world vulnerability research methodology:

* Clear attack chain
* Root cause analysis
* Exploit reliability
* Defensive guidance

---

🔥 *If you found this useful, consider adding diagrams, pcap traces, or automation scripts to push it even further.*
