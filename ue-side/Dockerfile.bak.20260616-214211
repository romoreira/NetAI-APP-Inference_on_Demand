# Pod-UE: base CUDA + PyTorch + Scapy para split inference e injeção GTP-U/SRv6.
# Build na própria máquina (single-node); a imagem é importada pro containerd
# do k8s com `ctr -n k8s.io images import`.
FROM nvidia/cuda:12.4.0-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_DEFAULT_TIMEOUT=300
ENV PIP_RETRIES=10

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip iproute2 tcpdump iputils-ping \
    && rm -rf /var/lib/apt/lists/*

# PyTorch com CUDA 12.1 (roda em driver 12.x). Versões fixas p/ reprodutibilidade.
# timeout/retries altos porque a saída de rede do host é IPv6 lento.
RUN pip3 install --no-cache-dir \
        --index-url https://download.pytorch.org/whl/cu121 \
        torch==2.4.1 \
    && pip3 install --no-cache-dir scapy==2.5.0 numpy==1.26.4 pillow==10.4.0

WORKDIR /app
COPY split_model.py ue_sender.py /app/

# Sobe em sleep: permite exec, ajuste de MTU e disparo manual do ue_sender.
# Trocar depois para rodar ue_sender.py em loop como entrypoint, se desejado.
CMD ["sleep", "infinity"]

