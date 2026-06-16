"""
split_model.py  —  ResNet-18 particionado para classificação de fogo (fire/no-fire)
-----------------------------------------------------------------------------------
Compatível com o checkpoint best.pth (arch=resnet18, task=fire, fc=2 classes).
Mesma definição usada no UE (forward_head) e no MEC (forward_tail).

Pontos de corte (split point k), nos limites dos blocos ResNet-18:
  k=1: após conv1+bn1+relu+maxpool        (stem)
  k=2: após layer1                        (64 canais)
  k=3: após layer2                        (128 canais)
  k=4: após layer3                        (256 canais)
  k=5: após layer4                        (512 canais)
O MEC completa com avgpool + fc (2 classes).

O UE roda forward_head(x, k); o MEC roda forward_tail(act, k).
Carregue os pesos com load_fire_resnet18('best.pth').
"""
import io
import torch
import io
import torch.nn as nn
import io
import torchvision


NUM_CLASSES = 2          # fire / no-fire
SPLIT_POINTS = 5         # k em [1,5]


class SplitResNet18(nn.Module):
    """ResNet-18 com forward particionável nos limites de bloco."""

    def __init__(self, num_classes: int = NUM_CLASSES):
        super().__init__()
        net = torchvision.models.resnet18(weights=None)
        net.fc = nn.Linear(net.fc.in_features, num_classes)
        # estágios nomeados para corte
        self.stem = nn.Sequential(net.conv1, net.bn1, net.relu, net.maxpool)
        self.layer1 = net.layer1
        self.layer2 = net.layer2
        self.layer3 = net.layer3
        self.layer4 = net.layer4
        self.avgpool = net.avgpool
        self.fc = net.fc
        self._stages = [self.stem, self.layer1, self.layer2, self.layer3, self.layer4]
        self.num_split = len(self._stages)   # 5

    def forward_head(self, x, k):
        """UE: executa stem..stage(k-1). k em [1,5]."""
        assert 1 <= k <= self.num_split, f"k deve estar em [1,{self.num_split}]"
        for i in range(k):
            x = self._stages[i](x)
        return x

    def forward_tail(self, x, k):
        """MEC: executa stage(k)..layer4 + avgpool + fc."""
        for i in range(k, self.num_split):
            x = self._stages[i](x)
        x = self.avgpool(x)
        x = torch.flatten(x, 1)
        return self.fc(x)

    def forward(self, x):
        for s in self._stages:
            x = s(x)
        x = self.avgpool(x)
        x = torch.flatten(x, 1)
        return self.fc(x)


def load_fire_resnet18(weights_path, map_location="cpu"):
    """Carrega best.pth (checkpoint com state_dict de ResNet-18 padrão)."""
    ck = torch.load(weights_path, map_location=map_location)
    # state_dict pode estar direto ou aninhado
    if isinstance(ck, dict) and "state_dict" in ck:
        sd = ck["state_dict"]
    elif isinstance(ck, dict) and "model_state" in ck:
        sd = ck["model_state"]
    elif isinstance(ck, dict) and all(hasattr(v, "shape") for v in ck.values() if not isinstance(v, (int, str))):
        sd = ck
    else:
        # procura o primeiro sub-dict que pareça state_dict
        sd = None
        for v in ck.values():
            if isinstance(v, dict) and any(hasattr(x, "shape") for x in v.values()):
                sd = v; break
        if sd is None:
            sd = ck
    model = SplitResNet18()
    # o state_dict do best.pth tem nomes resnet padrão (conv1, bn1, layer1...).
    # nosso módulo aninha em stem.0/stem.1 — remapeamos.
    remap = {}
    for k, v in sd.items():
        if k.startswith("conv1."):
            remap["stem.0." + k[len("conv1."):]] = v
        elif k.startswith("bn1."):
            remap["stem.1." + k[len("bn1."):]] = v
        else:
            remap[k] = v
    missing, unexpected = model.load_state_dict(remap, strict=False)
    return model, missing, unexpected


# ----------------------- serialização da ativação (inalterada) -----------------------

DTYPE_CODE = {torch.float32: 0, torch.float16: 1, torch.int8: 2}
CODE_DTYPE = {v: k for k, v in DTYPE_CODE.items()}


def quantize(t, mode):
    t = t.detach()
    if mode == "fp32":
        return t.contiguous(), 1.0
    if mode == "fp16":
        return t.half().contiguous(), 1.0
    if mode == "int8":
        scale = (t.abs().max() / 127.0).clamp(min=1e-8)
        q = (t / scale).round().clamp(-127, 127).to(torch.int8).contiguous()
        return q, float(scale)
    raise ValueError(mode)


def serialize_activation(t, mode="fp32"):
    q, scale = quantize(t, mode)
    meta = {"shape": list(q.shape), "dtype": DTYPE_CODE[q.dtype], "scale": scale}
    return q.cpu().numpy().tobytes(), meta


def deserialize_activation(payload: bytes, meta=None) -> torch.Tensor:
    """
    Desserializa ativação enviada pelo UE.

    Compatível com:
      1) payload torch.save(tensor)
      2) payload bruto numpy/tobytes com meta dtype/shape

    Dtypes aceitos:
      16 ou 1 -> fp16
      32 ou 0 -> fp32
      2       -> int8
    """
    # Formato novo usado pelo UE ResNet: torch.save(tensor)
    try:
        bio = io.BytesIO(payload)
        t = torch.load(bio, map_location="cpu")
        if isinstance(t, torch.Tensor):
            return t.float()
    except Exception:
        pass

    # Fallback para formato bruto antigo
    if meta is None:
        raise ValueError("meta is required for raw activation payload")

    dtype_code = int(meta["dtype"])
    np_dtype = {
        0: np.float32,
        1: np.float16,
        2: np.int8,
        16: np.float16,
        32: np.float32,
    }[dtype_code]

    arr = np.frombuffer(payload, dtype=np_dtype).copy()

    if "shape" in meta:
        shape = tuple(meta["shape"])
    else:
        shape = (int(meta["n"]), int(meta["c"]), int(meta["h"]), int(meta["w"]))

    arr = arr.reshape(shape)

    if dtype_code == 2:
        scale = float(meta.get("scale", 1.0))
        zero_point = int(meta.get("zero_point", 0))
        arr = (arr.astype(np.float32) - zero_point) * scale
    else:
        arr = arr.astype(np.float32)

    return torch.from_numpy(arr)

