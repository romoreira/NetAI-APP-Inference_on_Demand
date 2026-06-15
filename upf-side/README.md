# UPF Side

This directory contains the BESS/UPF datapath used to validate the SplitController-based inference-on-demand experiment.

The validated datapath is:

```text
UE -> GTP-U/N3 -> UPF/BESS -> GTP-U decapsulation -> SplitController -> PFCP QoS/FAR rules -> N6 -> MEC
```

## Expected directory layout

```text
upf-side/
└── bess-upf/
    ├── bess/
    │   ├── core/modules/split_controller.cc
    │   ├── core/modules/split_controller.h
    │   └── protobuf/module_msg.proto
    ├── upf/
    │   └── conf/
    │       ├── up4.bess
    │       ├── ports.py
    │       ├── parser.py
    │       ├── utils.py
    │       ├── sim.py
    │       └── gtp_echo.py
    └── config/
        ├── docker-compose-dpdk.yml
        ├── Dockerfile.bess-mlx5
        ├── upf-dpdk-local.jsonc
        └── inference_on_demand.validated.bess
```

## Validated topology

### N3 access side

| Role | Interface | IP | MAC |
|---|---|---:|---:|
| UE N3 sender | `enp9s0` | `192.168.70.100` | `06:ef:18:c6:68:d7` |
| UPF N3 ingress | `enp7s0np0` | `192.168.70.1` | `04:3f:72:fa:75:60` |

### N6 core side

| Role | Interface | IP | MAC |
|---|---|---:|---:|
| UPF N6 egress | `enp8s0np0` | `192.168.90.1` | `04:3f:72:fa:75:61` |
| MEC N6 ingress | `enp7s0` | `192.168.90.2` | `3e:89:68:ca:1a:c8` |

### N4 / PFCP

| Role | Interface | IP |
|---|---|---:|
| UE PFCP simulator | `enp7s0` | `192.168.80.2` |
| UPF PFCP endpoint | `enp9s0` | `192.168.80.1` |

## Host preparation

Install basic tools:

```bash
sudo apt update
sudo apt install -y docker.io docker-compose zip unzip ethtool tcpdump pciutils
```

Install RDMA/verbs support for Mellanox/ConnectX ports:

```bash
sudo apt install -y linux-modules-extra-$(uname -r) rdma-core ibverbs-providers infiniband-diags
sudo depmod -a
sudo modprobe mlx5_ib
sudo modprobe ib_uverbs
sudo modprobe rdma_ucm
```

Validate RDMA devices:

```bash
lsmod | grep -E "mlx5|ib_uverbs|rdma|ib_core"
ls -la /dev/infiniband
ibv_devinfo || true
```

## Configure UPF host interfaces

```bash
sudo ip addr add 192.168.70.1/24 dev enp7s0np0 2>/dev/null || true
sudo ip link set enp7s0np0 up

sudo ip addr add 192.168.90.1/24 dev enp8s0np0 2>/dev/null || true
sudo ip link set enp8s0np0 up

sudo ip addr add 192.168.80.1/24 dev enp9s0 2>/dev/null || true
sudo ip link set enp9s0 up

ip -br addr show enp7s0np0 enp8s0np0 enp9s0
```

Note: once BESS/DPDK owns the NICs, host ping through DPDK-owned interfaces may not be a valid datapath test. Prefer BESS counters and MEC tcpdump.

## SplitController module

The SplitController files must exist in BESS:

```text
bess/core/modules/split_controller.cc
bess/core/modules/split_controller.h
```

The protobuf argument must exist in:

```text
bess/protobuf/module_msg.proto
```

Expected definition:

```proto
message SplitControllerArg {
  uint32 bat_low = 1;
  uint32 cpu_high = 2;
  uint32 sig_low = 3;
  uint32 k_min = 4;
  uint32 k_max = 5;
}
```

Check for duplicates:

```bash
grep -n "message SplitControllerArg" bess/protobuf/module_msg.proto
grep -n "bat_low\|cpu_high\|sig_low\|k_min\|k_max" bess/protobuf/module_msg.proto
```

## Pipeline integration

The GTP-U decapsulation path must include SplitController before the QoS lookup:

```text
pdrLookup:GTPUDecap
    -> gtpuDecap::GtpuDecap()
    -> splitController::SplitController(bat_low=20, cpu_high=80, sig_low=30, k_min=1, k_max=5)
    -> appQERLookup
```

Validated BESS path:

```text
pktParse -> pdrLookup -> gtpuDecap -> splitController -> appQERLookup -> sessionQERLookup -> farLookup -> executeFAR -> enp8s0np0Routes -> n6MecDstEther -> enp8s0np0Merge -> enp8s0np0SrcEther -> enp8s0np0Fast
```

## Required `ports.py` patch

The manual N6 route needs access to the egress `Merge` module. Patch `upf/conf/ports.py` so the `merge` object is stored as `self.merge`.

Expected code:

```python
merge = Merge(name="{}Merge".format(self.name))
self.merge = merge

# Attach it to merge
merge.connect(update)
```

Patch command:

```bash
grep -q "self.merge = merge" /home/ubuntu/bess-upf/upf/conf/ports.py || \
sudo sed -i '/merge = Merge(name="{}Merge".format(self.name))/a\        self.merge = merge' /home/ubuntu/bess-upf/upf/conf/ports.py

grep -n "self.merge = merge" /home/ubuntu/bess-upf/upf/conf/ports.py
```

## Manual N6 route to MEC

Append this block to the BESS pipeline file loaded inside the container:

```python
# ---- Manual N6 route to MEC ----
# UPF N6: enp8s0np0 / 04:3f:72:fa:75:61
# MEC N6: enp7s0    / 3e:89:68:ca:1a:c8
n6_mec_dst = Update(
    name="n6MecDstEther",
    fields=[
        {"offset": 0, "size": 6, "value": mac2hex("3e:89:68:ca:1a:c8")}
    ],
)

ports[parser.core_ifname].rtr.add(prefix="192.168.90.2", prefix_len=32, gate=0)
ports[parser.core_ifname].rtr.connect(next_mod=n6_mec_dst, ogate=0)
n6_mec_dst.connect(next_mod=ports[parser.core_ifname].merge)
# ---- End manual N6 route ----
```

This creates:

```text
enp8s0np0Routes gate 0 -> n6MecDstEther -> enp8s0np0Merge
```

`n6MecDstEther` rewrites the Ethernet destination MAC to the MEC MAC:

```text
3e:89:68:ca:1a:c8
```

`enp8s0np0SrcEther` rewrites the Ethernet source MAC to the UPF N6 MAC:

```text
04:3f:72:fa:75:61
```

## Build Docker images

From the UPF repository root:

```bash
cd /home/ubuntu/bess-upf
```

Build the BESS image:

```bash
sudo docker build --network=host \
  -f config/Dockerfile.bess-mlx5 \
  -t upf-bess:2.4.2-dev-mlx5 \
  config
```

Build the PFCP image:

```bash
sudo docker build --network=host \
  -f upf/Dockerfile \
  --target pfcp \
  -t upf-pfcp:2.4.2-dev \
  upf
```

Check images:

```bash
sudo docker images | grep -E "upf-bess|upf-pfcp"
```

## Docker Compose

Use:

```text
bess-upf/config/docker-compose-dpdk.yml
```

The validated BESS service uses:

```yaml
entrypoint: ["bessd", "-f"]
command: ["-grpc-url=0.0.0.0:10514"]
```

Start the UPF:

```bash
cd /home/ubuntu/bess-upf/config
sudo docker-compose -f docker-compose-dpdk.yml up -d
```

Check containers:

```bash
sudo docker ps --format "table {{.Names}}\t{{.Image}}\t{{.Status}}"
```

Expected containers:

```text
bess
pfcpiface
```

## Copy runtime files into the BESS container

If the container was recreated, copy the validated pipeline and helper files:

```bash
sudo docker cp /home/ubuntu/bess-upf/config/inference_on_demand.validated.bess \
  bess:/opt/bess/bessctl/conf/up4.bess

sudo docker cp /home/ubuntu/bess-upf/upf/conf/__init__.py bess:/opt/bess/bessctl/conf/__init__.py
sudo docker cp /home/ubuntu/bess-upf/upf/conf/parser.py   bess:/opt/bess/bessctl/conf/parser.py
sudo docker cp /home/ubuntu/bess-upf/upf/conf/ports.py    bess:/opt/bess/bessctl/conf/ports.py
sudo docker cp /home/ubuntu/bess-upf/upf/conf/utils.py    bess:/opt/bess/bessctl/conf/utils.py
sudo docker cp /home/ubuntu/bess-upf/upf/conf/sim.py      bess:/opt/bess/bessctl/conf/sim.py
sudo docker cp /home/ubuntu/bess-upf/upf/conf/gtp_echo.py bess:/opt/bess/bessctl/conf/gtp_echo.py
```

Install Python dependencies inside the BESS container:

```bash
sudo docker exec bess bash -lc \
  'python3 -m pip install --break-system-packages iptools jsoncomment psutil'
```

## Load the BESS pipeline

Always load the pipeline with `CONF_FILE`:

```bash
sudo docker exec \
  -e CONF_FILE=/opt/bess/bessctl/conf/upf.jsonc \
  bess /opt/bess/bessctl/bessctl run file /opt/bess/bessctl/conf/up4.bess
```

Important: `bessctl run file` resets the current BESS pipeline. After every pipeline reload, recreate PFCP sessions from the PFCPsim node.

## Validate BESS pipeline

```bash
sudo docker exec bess /opt/bess/bessctl/bessctl show module splitController
sudo docker exec bess /opt/bess/bessctl/bessctl show module enp8s0np0Routes
sudo docker exec bess /opt/bess/bessctl/bessctl show module n6MecDstEther
sudo docker exec bess /opt/bess/bessctl/bessctl show port
```

Expected route modules:

```text
enp8s0np0Routes::IPLookup()
  Output gates:
    0 -> n6MecDstEther
    8191 -> enp8s0np0bad_route

n6MecDstEther::Update()
  Output gates:
    0 -> enp8s0np0Merge
```

## Validate PFCP sessions

After creating sessions from PFCPsim, validate:

```bash
sudo docker exec bess /opt/bess/bessctl/bessctl show module pdrLookup | head -25
sudo docker exec bess /opt/bess/bessctl/bessctl show module farLookup | head -25
sudo docker exec bess /opt/bess/bessctl/bessctl show module appQERLookup | head -25
sudo docker exec bess /opt/bess/bessctl/bessctl show module sessionQERLookup | head -25

curl -s http://127.0.0.1:8080/metrics | grep 'pfcp_sessions'
```

Expected state:

```text
pdrLookup = 20 rules
farLookup = 20 rules
appQERLookup = 40 rules
sessionQERLookup = 20 rules
pfcp_sessions{node_id="192.168.80.2"} 10
```

If `pfcp_sessions` becomes larger than 10 after repeated experiments, reset the PFCP interface container and reload the pipeline:

```bash
cd /home/ubuntu/bess-upf/config
sudo docker restart pfcpiface
sleep 3

sudo docker exec \
  -e CONF_FILE=/opt/bess/bessctl/conf/upf.jsonc \
  bess /opt/bess/bessctl/bessctl run file /opt/bess/bessctl/conf/up4.bess
```

Then recreate the 10 PFCP sessions from PFCPsim.

## Datapath validation

After sending GTP-U traffic from the UE, validate counters:

```bash
sudo docker exec bess /opt/bess/bessctl/bessctl show module splitController
sudo docker exec bess /opt/bess/bessctl/bessctl show module enp8s0np0Routes
sudo docker exec bess /opt/bess/bessctl/bessctl show module n6MecDstEther
sudo docker exec bess /opt/bess/bessctl/bessctl show port

curl -s http://127.0.0.1:8080/metrics | grep -Ei "upf_packets|upf_bytes|dropped|pfcp_sessions" | head -80
```

Validated result:

```text
splitController: 105 packets
enp8s0np0Routes gate 0: 105 packets
n6MecDstEther: 105 packets
enp8s0np0Fast TX: 105 packets
pfcp_sessions: 10
drops: 0
```

This confirms the complete path:

```text
UE -> GTP-U/N3 -> UPF/BESS -> SplitController -> PFCP rules -> N6 -> MEC
```

## Backup validated state

Save the validated runtime pipeline from the container:

```bash
mkdir -p /home/ubuntu/bess-upf/backups

sudo docker cp bess:/opt/bess/bessctl/conf/up4.bess \
  /home/ubuntu/bess-upf/config/inference_on_demand.validated.bess

sudo docker cp bess:/opt/bess/bessctl/conf/ports.py \
  /home/ubuntu/bess-upf/upf/conf/ports.py
```

Create a backup zip:

```bash
cd /home/ubuntu

TS=$(date +%Y%m%d-%H%M%S)

zip -r "bess-upf/backups/bess-upf-splitcontroller-validado-${TS}.zip" bess-upf \
  -x "bess-upf/.git/*" \
     "bess-upf/**/__pycache__/*" \
     "bess-upf/**/*.pyc" \
     "bess-upf/build/*" \
     "bess-upf/bess/build/*" \
     "bess-upf/bess/core/kmod/*.ko" \
     "bess-upf/backups/*.zip"

ls -lh bess-upf/backups/
```

## Useful commands

Show the BESS pipeline:

```bash
sudo docker exec bess /opt/bess/bessctl/bessctl show pipeline
```

Show ports:

```bash
sudo docker exec bess /opt/bess/bessctl/bessctl show port
```

Show SplitController:

```bash
sudo docker exec bess /opt/bess/bessctl/bessctl show module splitController
```

Show N6 route:

```bash
sudo docker exec bess /opt/bess/bessctl/bessctl show module enp8s0np0Routes
sudo docker exec bess /opt/bess/bessctl/bessctl show module n6MecDstEther
```

Check metrics:

```bash
curl -s http://127.0.0.1:8080/metrics | grep -Ei "upf_packets|upf_bytes|dropped|pfcp_sessions" | head -80
```

## Notes

- BESS CLI inside the container:

```bash
/opt/bess/bessctl/bessctl
```

- BESS gRPC endpoint:

```text
127.0.0.1:10514
```

- Validated DPDK PMD ports:

```text
enp7s0np0 -> Access/N3
enp8s0np0 -> Core/N6
enp9s0    -> PFCP/N4 host-side control
```

