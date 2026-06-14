# Inferência de IA como Serviço de Rede — Split Inference Adaptativo sobre 5G Core

## Resumo em uma frase

Testbed onde a **rede móvel vende capacidade de inferência de IA** (não apenas conectividade): dispositivos fracos (UEs) executam *parte* de uma CNN/YOLO localmente e delegam o restante ao núcleo da rede, que processa as camadas finais em GPU. O ponto de corte ("split point") é **negociado por uma API estilo CAMARA** (`InferenceOnDemand`) e **transportado in-band via SRv6**, lido e executado pelo BESS-UPF.

---

## 1. Motivação

As operadoras já expõem capacidades de rede via **APIs CAMARA** (QoD, DeviceStatus, Location, SimSwap) — todas sobre **conectividade**, já consolidadas. A proposta aqui é diferente: **expor a capacidade de computação de IA da própria rede como produto**. Em vez de vender "banda garantida", a operadora vende "inferência adaptativa" — ex.: **vigilância (surveillance) adaptativa** em que dispositivos baratos rodam detecção em tempo real porque a rede assume parte do trabalho conforme o estado do dispositivo.

> QoD vende **conectividade** sob demanda. A nossa API vende **cognição** (inferência) sob demanda.

---

## 2. Conceito central: Split Inference

Uma CNN/YOLO é uma sequência de camadas `L0..LN`. No split:

- O UE executa `L0..Lk` (até o **ponto de corte** `k`).
- O UE serializa a **ativação intermediária** (saída da camada `k`) — **não** o frame.
- O tensor vai pela rede ao servidor, que executa `Lk+1..LN` em GPU.

Quando o dispositivo está fraco (bateria/CPU/sinal), corta-se mais cedo (`k` menor → menos compute no UE, mais tráfego). Quando tem folga, corta-se mais tarde.

### Tabela de custo por ponto de corte (validada no testbed, frame 640×640)

| k | shape ativação | FP32 | FP16 | INT8 |
|---|---|---|---|---|
| 1 | (1,32,320,320) | 12.8 MB | 6.4 MB | 3.2 MB |
| 2 | (1,64,160,160) | 6.4 MB | 3.2 MB | 1.6 MB |
| 3 | (1,128,80,80) | 3.2 MB | 1.6 MB | 800 KB |
| 4 | (1,256,40,40) | 1.6 MB | 800 KB | 400 KB |
| 5 | (1,512,20,20) | 800 KB | 400 KB | 200 KB |

Esta tabela É a tese em miniatura: cortar cedo = mais bytes na rede; quantizar = 2-4× menos. O par ótimo `(k, quantização)` depende do estado do dispositivo.

---

## 3. A API proposta: `InferenceOnDemand`

Nova família CAMARA, espelhando `QualityOnDemand`:

- **Quem decide o split**: a API — quem vende o serviço (operadora / provedor de surveillance adaptativa).
- **Granularidade**: por **sessão/intent** (segundos), não por frame. O cliente compra "surveillance adaptativa"; a API negocia o envelope `(modelo, SLA de latência, faixa de split, política de quantização, gatilhos por estado)`.
- **Controle vs. dados**: CAMARA estabelece o *contrato* (intent plane); a decisão fina dentro do envelope é modulada pelo estado do UE e transportada **in-band** no plano de dados.

---

## 4. Transporte in-band via SRv6

A decisão de split (`k` + metadados do tensor) viaja num **TLV do SRH** do SRv6.

### Encapsulamento (decisões tomadas)
- UE gera **GTP-U** rumo ao UPF pela **N3** (UDP 2152).
- **Dentro** do GTP-U vai o pacote **SRv6** (IPv6 + SRH + TLV).
- **BESS-UPF lê o TLV** (UPF ciente de inferência — contribuição mais forte).
- **PDR casa só por TEID** (o IP interno IPv6/SRH não é inspecionado).

Stack do pacote (de fora para dentro):
```
Ethernet / IP / UDP(2152) / GTP-U(TEID) / IPv6 / SRH(+TLV split) / Raw(tensor)
```

### TLV de split (tipo experimental 124) — layout validado
```
[0]      k (uint8)
[1]      dtype code (uint8: 0=fp32 1=fp16 2=int8)
[2:4]    frag_off (uint16)
[4:6]    frag_total (uint16)
[6:10]   scale (float32, para dequantizar int8)
[10:11]  ndim (uint8)
[11:..]  shape dims (uint16 cada)
```
Tensores grandes são fragmentados em chunks de aplicação (8000 B); o BESS/servidor remonta pelo `frag_off`/`frag_total`.

---

## 5. Topologia física (validada)

Duas máquinas com GPU, ligadas por **Mellanox ConnectX-6** (VFs, `mlx5_core`).

### Máquina UE (`ue`, control-plane K8s)
| Interface | MAC | Papel | Config |
|---|---|---|---|
| `enp3s0` | fa:16:3e:e7:40:a0 | gerência | 10.30.6.123, MTU 1280* |
| `enp9s0` | **06:EF:18:C6:68:D7** | **N3 (dados)** | na bridge OVS `n3br` |
| `enp7s0` | 02:19:1A:56:71:44 | controle/PFCP | livre |

\* MTU reduzido para 1280 na gerência por causa do path IPv6 (ver §7).

### Máquina UPF (`upf`)
| Interface | MAC | Papel | Config |
|---|---|---|---|
| `enp3s0` | fa:16:3e:b0:9f:6e | gerência | 10.30.6.140 |
| `enp7s0np0` | **04:3F:72:FA:75:60** | **N3 (recebe GTP-U)** | 192.168.70.1/24 |
| `enp8s0np0` | 04:3F:72:FA:75:61 | N6 | 192.168.80.1/24 (a configurar) |
| `enp9s0` | 36:F1:72:66:77:D9 | PFCP | 192.168.90.1/24 |

**Caminho de dados N3 confirmado por fabric:**
`ue:enp9s0 (06:EF:...) ──cabo Mellanox──> upf:enp7s0np0 (04:3F:72:FA:75:60, 192.168.70.1)`

O `04:3F:72:FA:75:60` é o **`--next-hop-mac`** do `ue_sender.py`.

> **Importante:** a config inicial assumia a N3 na `enp7s0` do UE — estava errada. O fabric mostrou que a porta de dados é a **`enp9s0`**. A bridge `n3br` foi corrigida para usar `enp9s0`.

---

## 6. Stack de software (estado atual)

| Componente | Tecnologia | Estado |
|---|---|---|
| Orquestração | Kubernetes single-node (kubeadm v1.28, IPv4-primary) | ✅ |
| CNI primária | Flannel (`--iface=enp3s0`) | ✅ |
| CNI meta | Multus | ✅ |
| CNI secundária (N3) | OVS-CNI (bridge `n3br` → `enp9s0`) | ✅ |
| GPU | NVIDIA device plugin, time-slicing 10 fatias (Quadro RTX 6000) | ✅ |
| Imagem UE | `ue-split:local` (CUDA 12.4 + PyTorch 2.4.1 + Pillow + socket nativo) | ✅ |
| Geração de pacotes | **Raw socket AF_PACKET nativo** (Scapy só prototipou o formato) | ✅ validado fim-a-fim |
| Modelo | CNN/YOLO-like particionada (`split_model.py`) | ✅ |
| UPF | BESS-UPF + container PFCP (trabalho anterior) | ⏳ adaptar p/ TEID+SRv6 |
| Estressores | ChaosMesh | ⏳ pendente |
| Canal | ns-3 | ⏳ pendente |

---

## 7. Bring-up: problemas resolvidos hoje (cronologia)

A subida do cluster passou por uma cascata de problemas todos com **raiz num ambiente IPv6-primary** que conflitava com a config IPv4 do testbed:

1. **`kubeadm init` falhou no preflight** — `/proc/sys/net/ipv6/conf/default/forwarding != 1`. Corrigido habilitando IPv6 forwarding (necessário p/ SRv6 depois).
2. **Cluster subiu IPv6-primary** — porque a `enp3s0` tinha IPv6 global e o forwarding ligado. O kube-proxy ficava em modo dual-stack elegendo IPv6, mas o podCIDR/serviceCIDR eram IPv4 (`10.244/16`, `10.96/12`). Resultado: kube-proxy não programava as regras IPv4 do ClusterIP → `10.96.0.1` morto → Flannel/coredns/device-plugin todos em cascata de `network unreachable`.
3. **Solução definitiva** — reinit com `kubeadm-config.yaml` forçando IPv4: `advertiseAddress: 10.30.6.123`, `nodeRegistration.kubeletExtraArgs.node-ip`, e um bloco `KubeProxyConfiguration` com `clusterCIDR: 10.244.0.0/16`. Isso ancorou a família primária em IPv4 desde o init.
4. **Falta de rota IPv4 para o serviceCIDR** — o ambiente só tinha default IPv6. `ip route get 10.96.0.1 → unreachable`. Corrigido com `ip route add 10.96.0.0/12 dev enp3s0` (persistido via systemd unit).
5. **Flannel não detectava interface** — sem default IPv4, precisou de `--iface=enp3s0` explícito.
6. **GPU device plugin com 0 pods** — nodeSelector não casava; corrigido com patch removendo `nodeSelector`/`affinity` e desabilitando MPS/gfd.
7. **Pull de imagens travando (TLS handshake timeout)** — path de saída só IPv6 com MTU jumbo (9000) quebrava o handshake. Corrigido baixando MTU da `enp3s0` para **1280** (mínimo IPv6). A `enp3s0` é só gerência, então fica permanente sem custo.
8. **OVS-CNI ausente** — o re-init perdeu o `cluster-network-addons-operator`; reinstalado (binário `ovs` em `/opt/cni/bin`).
9. **Build do Docker sem DNS** — `resolv.conf` aponta para `127.0.0.53` (stub systemd-resolved), inacessível no namespace do build, e os upstreams reais são IPv6. Corrigido com `docker build --network=host`.

### Estado validado do cluster
- Node `ue` Ready, IPv4-primary
- `nvidia.com/gpu: 10` em Capacity/Allocatable
- Pod de teste: CUDA True (Quadro RTX 6000), scapy gtp+srh OK, `split_model` roda, `net1` (N3) presente

### Alcançabilidade N3 validada (sem OFED)
- `ping ue→upf` via `192.168.70.1` OK; ARP resolve `04:3f:72:fa:75:60` REACHABLE
- Latência alta/irregular (68–137 ms) — **esperado sem driver Mellanox OFED**; será resolvido na instalação do OFED
- MTU jumbo (9000) ainda não consistente ponta-a-ponta (UPF N3 em 1500)

---

## 8. Pods-UE: Deployment escalável + dataset compartilhado

O modelo atual é **Deployment** (escalável via `kubectl scale deploy/ue --replicas=N`),
com 1 réplica para validação. O dataset de teste é montado via **hostPath
read-only** em `/data` (todos os pods leem a mesma pasta). O `ue_sender.py` lê
frames de `/data` (jpg/png); se vazio, usa frame sintético (`torch.randn`).

Caminho do dataset no host: `/home/ubuntu/NetAI-APP-Inference_on_Demand/ue-side/dataset/test`

Ao escalar para N réplicas com TEIDs/IPs distintos, voltar ao StatefulSet
(ordinal estável) ou adicionar IPAM — réplicas de Deployment compartilham
IP/TEID, o que causa conflito.

## 8b. Alternativa: 10 pods-UE (StatefulSet)

10 pods `ue-0..ue-9`, cada um um dispositivo de borda independente (1 fatia de GPU cada = 10 fatias).

| Pod | TEID | IP N3 | IPv6 interno |
|---|---|---|---|
| ue-0 | 10 | 192.168.70.10 | 2001:db8::10 |
| ... | ... | ... | ... |
| ue-9 | 19 | 192.168.70.19 | 2001:db8::19 |

O StatefulSet dá hostnames ordinais; um initContainer lê o ordinal de `$HOSTNAME`, configura `net1` com `192.168.70.(10+N)/24` + MTU 9000; o `ue_sender.py --auto-ordinal` deriva o TEID (`10+N`) sozinho. Zero config manual por pod.

A sessão PFCP precisa de 10 sessões (`setup_pfcpsim_10.sh`, `--count 10 --baseID 10`), PDR por TEID, `app-filter "ip:any:any"` (aceita IPv6 interno).

---

## 9. Arquivos do projeto

| Arquivo | Papel |
|---|---|
| `split_model.py` | CNN/YOLO-like com `forward_head(x,k)`/`forward_tail(x,k)`; serialização sem pickle + quantização FP32/FP16/INT8 |
| `ue_sender.py` | Monta GTP-U/SRv6/TLV e injeta na N3 via socket L2 persistente; `--auto-ordinal` deriva TEID/IP do StatefulSet; retry em ENOBUFS |
| `Dockerfile` | `ue-split:local` — CUDA 12.4 + PyTorch 2.4.1 + Scapy 2.5 |
| `ue-split-pod.yaml` | Pod único de teste (GPU + N3) |
| `ue-statefulset.yaml` | Os 10 pods-UE com IP/TEID por ordinal |
| `network-n3br.yaml` | NetworkAttachmentDefinition OVS → `n3br` |
| `setup_pfcpsim_10.sh` | Provisiona 10 sessões PFCP (TEIDs 10–19) |

---

## 10. Sequência de build e execução

```bash
# build (rede do host por causa do DNS IPv6)
sudo docker build --network=host -t ue-split:local .
# importar pro containerd do k8s (stores separados)
sudo docker save ue-split:local | sudo ctr -n k8s.io images import -

# subir os 10 UEs
kubectl apply -f network-n3br.yaml
kubectl apply -f ue-statefulset.yaml

# provisionar 10 sessões PFCP no UPF
./setup_pfcpsim_10.sh

# disparar um UE (auto-deriva TEID/IP do ordinal)
kubectl exec ue-3 -- python3 /app/ue_sender.py --auto-ordinal \
  --iface net1 --k 3 --quant int8 --loop 5 --fps 5 \
  --next-hop-mac 04:3f:72:fa:75:60

# captura na N3 física (lado UE)
sudo tcpdump -i enp9s0 -n udp port 2152 -c 5
```

---

## 11. Lado UE — VALIDADO FIM A FIM ✅

O pipeline completo do UE está provado no nível de pacote:
```
pod → split-CNN (forward_head, k) → serialização int8 → GTP-U/IPv6/SRH/TLV →
raw socket AF_PACKET → net1 → OVS n3br → enp9s0 → cabo Mellanox → upf:enp7s0np0
```

### O problema do Scapy e a solução (raw socket nativo)
O `ue_sender.py` original usava Scapy (`sendp`/`L2socket`) no hot-path. Isso
**travava** com `[Errno 105] No buffer space available` já no primeiro
fragmento — o Scapy reconstrói/serializa o objeto a cada `send()` e estoura o
buffer do socket. Funcionava para 1 pacote (ICMP de teste), mas não para os
~158 fragmentos de um tensor.

**Solução:** reescrita com **raw socket nativo (`socket.AF_PACKET`)**. O pacote
é montado à mão com `struct.pack` (Ethernet/IPv4/UDP/GTP-U/IPv6/SRH/TLV) e
enviado direto pelo kernel. Scapy saiu do runtime completamente. Resultado:
158 fragmentos em ~3ms, sem ENOBUFS.

### Descobertas de medição (cold vs hot)
```
frame=0  head_infer=138.9ms  tx=5.6ms   (cold start CUDA)
frame=1  head_infer=0.8ms    tx=2.9ms   (inferência quente)
frame=2  head_infer=0.7ms    tx=2.8ms
```
O custo de compute no UE cai de 138ms (cold) para ~0.7ms (hot) — relevante
para as métricas de energia/latência das baselines.

### Detalhe crítico: MTU vs tamanho do fragmento
O frame com chunk de 1400B dava 1538B total > MTU 1500 → ENOBUFS. Solução:
`--chunk 1300` (frame ~1438B, cabe com folga). Default fixado em 1300.
Quando o OFED entrar e o jumbo (9000) for consistente, pode-se subir o chunk.

### Confirmado no UPF (tcpdump)
```
192.168.70.50.2152 > 192.168.70.1.2152: UDP, length 1396
158 packets received by filter, 0 packets dropped
```

## 12. Roadmap restante

- [ ] Resolver o caminho pod→N3 (problema aberto §11)
- [ ] Instalar MLNX_OFED nas duas máquinas (latência baixa, jumbo 9000, throughput de linha)
- [ ] Adaptar BESS-UPF: PDR por TEID (10–19) + decap SRv6 + leitura do TLV
- [ ] `farLookup`/N6 aceitar payload IPv6
- [ ] Servidor de inferência na N6 reconstruindo o tensor (`forward_tail`) na GPU
- [ ] Integração ns-3 (canal) + ChaosMesh (recursos do nó)
- [ ] API `InferenceOnDemand` (intent plane)
- [ ] Campanha de baselines: all-on-UE, all-on-server, split estático, split on-demand
- [ ] Fronteira de Pareto: mAP × energia-UE × latência × bytes-na-rede, condicionada ao estado

---

## 13. Baselines e métricas (planejamento)

Quatro configurações no mesmo testbed:
1. **All-on-UE** — inferência completa no pod (borda pura).
2. **All-on-server** — UE só captura/encapsula (offload total). Nota: frame comprime bem, pode vencer o split em bytes-na-rede — comparação honesta.
3. **Split estático** — `k` fixo, sem adaptação. **Essencial** para isolar o ganho da adaptação.
4. **Split on-demand via CAMARA+SRv6** — *a contribuição*: `(k, quantização)` variam com o estado do UE.

Métricas como **fronteira de Pareto**: mAP × energia-UE × latência fim-a-fim × bytes-na-rede, condicionada ao estado simulado (bateria/fading/CPU). Eixos: ns-3 = canal, ChaosMesh = recursos do nó, CAMARA = intent, SRv6 = transporte, BESS = executor.

**Cuidado metodológico:** time-slicing não isola memória/fairness da GPU — adequado no servidor, mas distorce energia/latência *por UE* se os pods-UE compartilharem. Para medição limpa por UE, considerar MPS/MIG ou serializar.

---

## 14. Glossário

- **UE**: pod com computação simulando dispositivo de borda.
- **UPF**: User Plane Function (núcleo 5G, plano de dados).
- **N3/N6**: interfaces 3GPP — N3 liga (R)AN↔UPF; N6 liga UPF↔rede de dados.
- **GTP-U**: tunelamento do plano de usuário 3GPP (UDP/2152).
- **SRv6/SRH**: Segment Routing sobre IPv6 / o cabeçalho; o TLV carrega o split-level.
- **BESS-UPF**: UPF baseado no framework BESS (programável por pipeline).
- **Split point (`k`)**: camada onde se divide a execução UE↔rede.
- **TEID**: identificador de túnel GTP (casa o PDR no UPF).

