"""
split_model.py
--------------
CNN/YOLO-like com ponto de corte (split point `k`) parametrizável e
quantização opcional da ativação intermediária.

O UE roda head() até a camada k, serializa o tensor de ativação, e a rede
processa tail() das camadas k+1..N. Trocar a YOLO real depois é só substituir
os blocos por um nn.Module com a mesma interface (lista de camadas sequenciais).
"""
import io
import torch
import torch.nn as nn


class SimpleBackbone(nn.Module):
    """
    Backbone sequencial com pontos de corte bem definidos.
    Cada bloco reduz a resolução espacial (stride 2) -> a ativação fica
    progressivamente MENOR em HxW, o que importa para bytes-na-rede.

    Camadas (índices de corte válidos = 1..len(blocks)):
      bloco 0: 3   -> 32   (stride 2)
      bloco 1: 32  -> 64   (stride 2)
      bloco 2: 64  -> 128  (stride 2)
      bloco 3: 128 -> 256  (stride 2)
      bloco 4: 256 -> 512  (stride 2)
    head de detecção no fim.
    """

    def __init__(self, num_classes: int = 80):
        super().__init__()
        chans = [3, 32, 64, 128, 256, 512]
        self.blocks = nn.ModuleList()
        for i in range(len(chans) - 1):
            self.blocks.append(
                nn.Sequential(
                    nn.Conv2d(chans[i], chans[i + 1], 3, stride=2, padding=1),
                    nn.BatchNorm2d(chans[i + 1]),
                    nn.SiLU(inplace=True),
                )
            )
        # cabeça de detecção simplificada (predição densa)
        self.det = nn.Conv2d(chans[-1], (num_classes + 5), 1)
        self.num_blocks = len(self.blocks)

    def forward_head(self, x: torch.Tensor, k: int) -> torch.Tensor:
        """Executa blocos 0..k-1 (o que roda no UE). k em [1, num_blocks]."""
        assert 1 <= k <= self.num_blocks, f"k deve estar em [1,{self.num_blocks}]"
        for i in range(k):
            x = self.blocks[i](x)
        return x

    def forward_tail(self, x: torch.Tensor, k: int) -> torch.Tensor:
        """Executa blocos k..N + cabeça (o que roda na rede/GPU servidor)."""
        for i in range(k, self.num_blocks):
            x = self.blocks[i](x)
        return self.det(x)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """Inferência completa (baseline all-on-UE ou all-on-server)."""
        for blk in self.blocks:
            x = blk(x)
        return self.det(x)


# ----------------------- serialização da ativação -----------------------

DTYPE_CODE = {torch.float32: 0, torch.float16: 1, torch.int8: 2}
CODE_DTYPE = {v: k for k, v in DTYPE_CODE.items()}


def quantize(t: torch.Tensor, mode: str):
    """
    Quantiza a ativação. Retorna (tensor_quantizado, scale).
    mode: 'fp32' | 'fp16' | 'int8'
    scale é necessário para dequantizar int8 no servidor.
    """
    t = t.detach()
    if mode == "fp32":
        return t.contiguous(), 1.0
    if mode == "fp16":
        return t.half().contiguous(), 1.0
    if mode == "int8":
        scale = (t.abs().max() / 127.0).clamp(min=1e-8)
        q = (t / scale).round().clamp(-127, 127).to(torch.int8).contiguous()
        return q, float(scale)
    raise ValueError(f"modo de quantizacao desconhecido: {mode}")


def serialize_activation(t: torch.Tensor, mode: str = "fp32") -> tuple[bytes, dict]:
    """
    Serializa o tensor de ativação para bytes crus (sem pickle).
    Retorna (payload_bytes, meta) onde meta vai no TLV do SRH.
    """
    q, scale = quantize(t, mode)
    meta = {
        "shape": list(q.shape),
        "dtype": DTYPE_CODE[q.dtype],
        "scale": scale,
    }
    payload = q.cpu().numpy().tobytes()
    return payload, meta


def deserialize_activation(payload: bytes, meta: dict) -> torch.Tensor:
    """Reconstrói o tensor no servidor a partir dos bytes + meta do TLV."""
    import numpy as np
    dtype = CODE_DTYPE[meta["dtype"]]
    np_dtype = {0: np.float32, 1: np.float16, 2: np.int8}[meta["dtype"]]
    arr = np.frombuffer(payload, dtype=np_dtype).reshape(meta["shape"]).copy()
    t = torch.from_numpy(arr)
    if dtype == torch.int8:
        t = t.float() * meta["scale"]
    elif dtype == torch.float16:
        t = t.float()
    return t


if __name__ == "__main__":
    # demonstração rápida dos tamanhos por ponto de corte e quantização
    m = SimpleBackbone().eval()
    x = torch.randn(1, 3, 640, 640)
    print(f"{'k':>2} {'shape ativação':>22} {'fp32 KB':>9} {'fp16 KB':>9} {'int8 KB':>9}")
    with torch.no_grad():
        for k in range(1, m.num_blocks + 1):
            act = m.forward_head(x, k)
            sizes = {}
            for mode in ("fp32", "fp16", "int8"):
                p, _ = serialize_activation(act, mode)
                sizes[mode] = len(p) / 1024
            print(f"{k:>2} {str(tuple(act.shape)):>22} "
                  f"{sizes['fp32']:>9.1f} {sizes['fp16']:>9.1f} {sizes['int8']:>9.1f}")

