# MEC Side

This directory contains the MEC-side validation instructions for the inference-on-demand datapath.

The MEC node is the N6-side endpoint. It receives decapsulated IPv4/UDP traffic after the UPF processes GTP-U packets through BESS and SplitController.

## Validated topology

### N6 data plane

| Role | Interface | IP | MAC |
|---|---|---:|---:|
| UPF N6 egress | `enp8s0np0` | `192.168.90.1` | `04:3f:72:fa:75:61` |
| MEC N6 ingress | `enp7s0` | `192.168.90.2` | `3e:89:68:ca:1a:c8` |

The MEC receives plain IPv4/UDP traffic. GTP-U encapsulation should already have been removed by the UPF.

## Configure the MEC interface

```bash
sudo ip addr add 192.168.90.2/24 dev enp7s0 2>/dev/null || true
sudo ip link set enp7s0 up
ip -br addr show dev enp7s0
```

Optional link validation:

```bash
sudo ethtool enp7s0 | grep -E 'Link detected|Speed'
```

## Capture one UE flow

Use this command for the first UE session:

```bash
sudo tcpdump -ni enp7s0 -e -vv 'host 10.250.0.1 or udp port 5001 or ip6'
```

Expected packet:

```text
04:3f:72:fa:75:61 > 3e:89:68:ca:1a:c8, ethertype IPv4
10.250.0.1.40001 > 192.168.90.2.5001: UDP
```

This confirms that the packet arrived on N6 after UPF decapsulation.

## Capture all 10 UE flows

```bash
sudo tcpdump -ni enp7s0 -e -vv 'net 10.250.0.0/24 or udp portrange 5001-5010'
```

Expected flows:

```text
10.250.0.1  -> 192.168.90.2:5001
10.250.0.2  -> 192.168.90.2:5002
10.250.0.3  -> 192.168.90.2:5003
10.250.0.4  -> 192.168.90.2:5004
10.250.0.5  -> 192.168.90.2:5005
10.250.0.6  -> 192.168.90.2:5006
10.250.0.7  -> 192.168.90.2:5007
10.250.0.8  -> 192.168.90.2:5008
10.250.0.9  -> 192.168.90.2:5009
10.250.0.10 -> 192.168.90.2:5010
```

## Optional UDP listeners

For application-level validation, start UDP listeners:

```bash
for p in $(seq 5001 5010); do
  nohup nc -ul -p "$p" > "udp-$p.log" 2>&1 &
done
```

Check listeners:

```bash
ss -lunp | grep -E '5001|5002|5003|5004|5005|5006|5007|5008|5009|5010'
```

Stop listeners:

```bash
pkill -f "nc -ul -p"
```

## Troubleshooting

### No packets arrive

Check the MEC interface:

```bash
ip -br addr show dev enp7s0
sudo ethtool enp7s0 | grep -E 'Link detected|Speed'
```

Then validate the UPF N6 transmit counter on the UPF node:

```bash
sudo docker exec bess /opt/bess/bessctl/bessctl show port
```

Expected UPF counter:

```text
enp8s0np0Fast Out/TX packets > 0
```

### Packets arrive with GTP-U

That indicates capture before decapsulation or an incorrect path. On MEC N6, packets should be plain IPv4/UDP, not UDP/2152 GTP-U.

### Wrong destination MAC

The validated destination MAC on MEC is:

```text
3e:89:68:ca:1a:c8
```

The validated source MAC from UPF N6 is:

```text
04:3f:72:fa:75:61
```

If these are wrong, check the UPF-side `n6MecDstEther` module.

## Validated result

The MEC received decapsulated traffic from 10 UE sessions. The validated UPF counters showed 105 packets forwarded to N6 with zero drops.

