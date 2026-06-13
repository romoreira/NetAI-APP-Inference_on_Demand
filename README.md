# Inferência de IA como Serviço de Rede — Split Inference Adaptativo sobre 5G Core

## Resumo em uma frase

Estamos construindo um testbed onde a **rede móvel vende capacidade de inferência de IA** (não apenas conectividade): dispositivos fracos (UEs) executam *parte* de uma CNN/YOLO localmente e delegam o restante ao núcleo da rede, que processa as camadas finais em GPU. O ponto de corte ("split point") é **negociado por uma API estilo CAMARA** e **transportado in-band via SRv6**, sendo lido e executado pelo UPF.

---

## 1. Motivação

As operadoras (Vodafone e outras) já expõem capacidades de rede via **APIs CAMARA**: Quality-on-Demand (QoD), DeviceStatus, Location, SimSwap. Todas elas expõem **conectividade** — latência, banda, localização. São commodity e já estão "batidas".

A proposta aqui é diferente e mais disruptiva: **expor a capacidade de computação de IA da própria rede como um produto**. Em vez de vender "banda garantida", a operadora vende "inferência adaptativa" — por exemplo, um serviço de **vigilância (surveillance) adaptativa** em que câmeras/dispositivos baratos conseguem rodar detecção de objetos em tempo real porque a rede assume parte do trabalho pesado conforme o estado do dispositivo.

A analogia retórica central:
> QoD vende **conectividade** sob demanda. A nossa API vende **cognição** (inferência) sob demanda.

---

## 2. Conceito central: Split Inference (Inferência Particionada)

Uma CNN/YOLO é uma sequência de camadas `L0, L1, ..., LN`. Em **split inference**:

- O UE executa as camadas `L0 ... Lk` (até o **ponto de corte** `k`).
- O UE serializa a **ativação intermediária** (o tensor de saída da camada `k`) — **não** o frame de vídeo cru.
- Esse tensor é enviado pela rede ao servidor de inferência, que executa `Lk+1 ... LN` em GPU e produz a detecção final.

O valor: dispositivos com pouca bateria/CPU/GPU não precisam rodar o modelo inteiro. Quando o dispositivo está fraco (bateria baixa, sinal ruim, CPU saturada), corta-se mais cedo (`k` menor → menos compute no UE, mais tráfego na rede). Quando o dispositivo tem folga, corta-se mais tarde (`k` maior → mais compute local, menos tráfego).

### Atenção crítica — o tamanho do tensor de ativação
A ativação intermediária **pode ser maior que o frame original** dependendo de onde se corta (camadas iniciais expandem canais antes do pooling reduzir a resolução espacial). Por isso:
- O ponto de corte deve preferir posições após pooling/downsampling.
- A **quantização da ativação** (FP32 → FP16 / INT8 / feature codecs) é um parâmetro experimental, não um detalhe — vira eixo de comparação.

---

## 3. A API proposta: `InferenceOnDemand`

Uma nova família de API CAMARA, espelhando a estrutura do `QualityOnDemand`:

- **Quem decide o split**: a API. Ou seja, **quem vende o serviço** (a operadora / o provedor de surveillance adaptativa) define o envelope de inferência contratado.
- **Granularidade**: por **sessão/intent** (escala de segundos), não por frame. O cliente compra "surveillance adaptativa com detecção em tempo real"; a API negocia o envelope `(modelo, SLA de latência, faixa de split point, política de quantização, gatilhos por estado do dispositivo)`.
- **Plano de controle vs. plano de dados**: a CAMARA estabelece o *contrato* (intent plane). A decisão fina dentro do envelope contratado é modulada pelo estado do UE e **transportada in-band** no plano de dados.

Campos conceituais do request (a definir/refinar): `model_id`, `latency_SLA`, `split_envelope` (faixa de `k` permitida), `quantization_policy`, `device_state_triggers`.

---

## 4. Transporte in-band via SRv6

A decisão de split (o valor de `k` e os metadados do tensor) viaja **dentro de um TLV do Segment Routing Header (SRH)** do SRv6.

### Encapsulamento (decisão de design já tomada)
- O UE (pod) gera **GTP-U** em direção ao UPF pela interface **N3** (padrão 3GPP, UDP porta 2152).
- **Dentro** do payload GTP-U vai o pacote **SRv6** (IPv6 + SRH + TLV com o split-level e metadados do tensor).
- O **BESS-UPF** desencapsula o GTP-U e, do lado da **N6**, o pacote SRv6 emerge e é processado.
- **O BESS-UPF lê o TLV** (decisão consciente: o UPF fica "ciente de inferência" — contribuição mais forte do que deixar isso para o servidor final). Em seguida roteia/sinaliza o split-level para a máquina com GPU.

Stack do pacote, de fora para dentro:
```
Ethernet / IP / UDP(2152) / GTP-U(TEID) / IPv6 / SRH(+TLV split) / Raw(tensor de ativação)
```

### Caracterização honesta para o paper
Como a CAMARA decide por sessão mas o TLV viaja por pacote, o TLV carrega uma decisão **estável** durante a sessão (muda só quando o estado do UE cruza um threshold). Isso é **"stateful in-band signaling"**, não "decisão in-band por frame". Nomear corretamente evita crítica de revisor.

---

## 5. Topologia física

Duas máquinas conectadas por **Mellanox ConnectX-6** (VFs SR-IOV, driver `mlx5_core`), ambas com GPU:

- **Máquina UE**: cluster Kubernetes single-node. Roda **pods que atuam como UEs** — cada pod tem poder de computação (acesso a GPU compartilhada via time-slicing) e executa as camadas iniciais do split-CNN. Os pods geram GTP-U/SRv6 e injetam na N3 via Scapy.
- **Máquina UPF/servidor**: roda o **BESS-UPF** (com container PFCP já existente), desencapsula GTP-U, lê o TLV SRv6 e executa as camadas finais da CNN/YOLO em GPU.

Interfaces na máquina UE:
- `enp7s0` (MAC `02:19:1a:56:71:44`) → **N3** (ligada ao UPF), colocada na bridge OVS `n3br`.
- `enp9s0` → controle/outra perna.
- `enp3s0` → gerência (10.30.6.123).

---

## 6. Simulação de condições realistas nos UEs

Os pods-UE precisam sofrer condições de borda realistas enquanto rodam inferência em loop:

- **ChaosMesh** → estressores de **sistema**: degradação de CPU, pressão de memória, simulação de queda de bateria, throttling. É o *atuador* sobre os recursos do nó.
- **ns-3** → modelo de **canal/rádio** de alta fidelidade (fading, mobilidade, propagação LTE/NR). O ns-3 **não** modela recursos de compute; o ChaosMesh **não** modela propagação de rádio. Eles são complementares.
  - Integração pretendida: ns-3 gera o trace de canal e o injeta nos pods reais via emulação (FdNetDevice/TapBridge) ou via NetworkChaos do ChaosMesh atuando como injetor do trace.
- Divisão de responsabilidades clara: **ns-3 = canal**, **ChaosMesh = recursos do nó**, **CAMARA = intent de sessão**, **SRv6 = transporte da decisão fina**, **BESS-UPF = executor**.

---

## 7. Baselines e grau de comparação

Quatro configurações comparadas no **mesmo** testbed (todas com inferência real em PyTorch):

1. **All-on-UE** — inferência completa no pod. Baseline de borda pura. Zero ativação na rede, máximo consumo de energia/CPU no UE.
2. **All-on-server** — UE só captura e encapsula; UPF/GPU faz tudo. Baseline de offload total. Observação importante: o frame de vídeo comprime bem (codecs maduros), então esta baseline **pode vencer o split em bytes-na-rede** — a comparação precisa ser honesta quanto a isso.
3. **Split estático** — ponto de corte `k` fixo, sem adaptação. **Baseline essencial** para isolar o ganho da *adaptação* (sem ela, não se distingue o ganho do split do ganho de adaptar).
4. **Split on-demand via CAMARA + SRv6** — *a contribuição*. `k` (e quantização) variam conforme o estado do UE, dentro do envelope vendido pela API.

### Métricas — fronteira de Pareto multi-dimensional
Não medir energia/compute isoladamente. Reportar o **trade-off** como fronteira de Pareto:
- **mAP / acurácia** da inferência
- **energia no UE**
- **latência fim-a-fim**
- **bytes na rede**

…tudo **condicionado ao estado simulado** (bateria, fading, CPU). A tese é que o split-point ótimo **não é fixo**: depende do estado do dispositivo e do canal, e expor esse controle como serviço de rede é a contribuição.

Eixo adicional de ablation: política de quantização (FP32 / FP16 / INT8 / feature codec) cruzada com `k`.

### Cuidado metodológico sobre a GPU compartilhada
O time-slicing **não isola memória nem garante fairness**. É adequado no servidor (várias sessões competindo é realista). Mas se os pods-UE também usam GPU compartilhada, a competição **distorce a medição de energia/latência por UE**. Para medir consumo por UE de forma limpa, considerar MPS/MIG ou serializar os experimentos de UE.

---

## 8. Stack de implementação

| Componente | Tecnologia |
|---|---|
| Orquestração | Kubernetes single-node (kubeadm, v1.28) |
| CNI primária | Flannel |
| CNI meta | Multus |
| CNI secundária (N3) | OVS-CNI (bridges `n2br`/`n3br`/`n4br`) |
| GPU | NVIDIA device plugin com time-slicing (Quadro RTX 6000) |
| Geração de pacotes no UE | Scapy (`scapy.contrib.gtp`, `IPv6ExtHdrSegmentRouting`) |
| UPF | BESS-UPF + container PFCP |
| Transporte data-plane | GTP-U (N3) encapsulando SRv6 (visto na N6) |
| Estressores de sistema | ChaosMesh |
| Modelo de canal | ns-3 |
| Modelo de IA | CNN/YOLO particionada para surveillance |

---

## 9. Estado atual do bring-up

- [x] Cluster Kubernetes single-node de pé (node `ue` Ready).
- [x] Driver NVIDIA (595.80) + nvidia-container-toolkit + runtime no containerd.
- [x] Bridge OVS `n3br` com `enp7s0` (N3), MTU 9000 (jumbo frames).
- [x] NetworkAttachmentDefinition `n3-net` para Multus.
- [ ] NVIDIA device plugin anunciando `nvidia.com/gpu` — **bloqueado**: o Multus está anexando o pod do plugin à rede secundária, causando `network unreachable` ao API server. Correção: garantir que o plugin use apenas a rede primária (Flannel), não o Multus default.
- [ ] Imagem do pod-UE (PyTorch + Scapy + split-CNN).
- [ ] BESS-UPF lendo o TLV SRv6 na outra máquina.
- [ ] Integração ns-3 + ChaosMesh.

---

## 10. Glossário rápido

- **UE**: User Equipment — aqui, um pod com poder de computação simulando um dispositivo de borda.
- **UPF**: User Plane Function — elemento do núcleo 5G que processa o plano de dados.
- **N3 / N6**: interfaces 3GPP — N3 liga (R)AN↔UPF; N6 liga UPF↔rede de dados.
- **GTP-U**: protocolo de tunelamento do plano de usuário 3GPP (UDP/2152).
- **SRv6 / SRH**: Segment Routing sobre IPv6 / o cabeçalho de roteamento; o TLV é onde embutimos o split-level.
- **CAMARA**: conjunto de APIs abertas que expõem capacidades de rede móvel a desenvolvedores.
- **BESS-UPF**: implementação de UPF baseada no framework BESS (programável por pipeline).
- **Split point (`k`)**: a camada da CNN onde se divide a execução entre UE e rede.
