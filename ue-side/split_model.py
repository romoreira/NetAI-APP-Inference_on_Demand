"""
split_model.py — ResNet-18 particionado para split inference.

Compatível com UE e MEC:
  UE:  forward_head(x, k)
  MEC: forward_tail(act, k)

Pontos de corte:
  k=1: após stem
  k=2: após layer1
  k=3: após layer2
  k=4: após layer3
  k=5: após layer4
"""

import io
import torch
import torch.nn as nn
import torchvision


NUM_CLASSES = 2
SPLIT_POINTS = 5


class SplitResNet18(nn.Module):
    def __init__(self, num_classes: int = NUM_CLASSES):
        super().__init__()
        net = torchvision.models.resnet18(weights=None)
        net.fc = nn.Linear(net.fc.in_features, num_classes)

        self.stem = nn.Sequential(net.conv1, net.bn1, net.relu, net.maxpool)
        self.layer1 = net.layer1
        self.layer2 = net.layer2
        self.layer3 = net.layer3
        self.layer4 = net.layer4
        self.avgpool = net.avgpool
        self.fc = net.fc

        self._stages = [
            self.stem,
            self.layer1,
            self.layer2,
            self.layer3,
            self.layer4,
        ]

    def forward_head(self, x: torch.Tensor, k: int) -> torch.Tensor:
        if not 1 <= k <= len(self._stages):
            raise ValueError(f"k must be in [1,{len(self._stages)}], got {k}")
        for i in range(k):
            x = self._stages[i](x)
        return x

    def forward_tail(self, x: torch.Tensor, k: int) -> torch.Tensor:
        if not 1 <= k <= len(self._stages):
            raise ValueError(f"k must be in [1,{len(self._stages)}], got {k}")
        for i in range(k, len(self._stages)):
            x = self._stages[i](x)
        x = self.avgpool(x)
        x = torch.flatten(x, 1)
        x = self.fc(x)
        return x

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = self.forward_head(x, SPLIT_POINTS)
        return self.forward_tail(x, SPLIT_POINTS)


# Compatibilidade com ue_sender.py antigo
SimpleBackbone = SplitResNet18


def load_fire_resnet18(weights_path, map_location="cpu"):
    model = SplitResNet18(num_classes=NUM_CLASSES)
    ckpt = torch.load(weights_path, map_location=map_location)

    if isinstance(ckpt, dict) and "model_state_dict" in ckpt:
        ckpt = ckpt["model_state_dict"]
    elif isinstance(ckpt, dict) and "state_dict" in ckpt:
        ckpt = ckpt["state_dict"]

    if isinstance(ckpt, dict):
        ckpt = {k.replace("module.", ""): v for k, v in ckpt.items()}
        model.load_state_dict(ckpt, strict=False)

    return model


def serialize_activation(t: torch.Tensor, quant: str = "fp16"):
    """
    Serializa a ativação no mesmo contrato esperado pelo ue_sender.py:
      payload, meta = serialize_activation(act, quant)

    payload: bytes com torch.save(tensor)
    meta: dicionário simples com dtype, shape e bytes.
    """
    if quant == "fp16":
        t = t.detach().half().cpu()
    elif quant == "fp32":
        t = t.detach().float().cpu()
    else:
        raise ValueError(f"unsupported quant={quant}")

    bio = io.BytesIO()
    torch.save(t, bio)
    payload = bio.getvalue()

    meta = {
        "dtype": 16 if quant == "fp16" else 32,
        "shape": tuple(t.shape),
        "nbytes": len(payload),
        "quant": quant,
        "scale": 1.0,
        "zero_point": 0,
    }
    return payload, meta


def deserialize_activation(buf: bytes) -> torch.Tensor:
    bio = io.BytesIO(buf)
    return torch.load(bio, map_location="cpu")


def quantize_activation(t: torch.Tensor, quant: str = "fp16") -> torch.Tensor:
    if quant == "fp16":
        return t.half()
    if quant == "fp32":
        return t.float()
    raise ValueError(f"unsupported quant={quant}")


if __name__ == "__main__":
    m = SplitResNet18().eval()
    x = torch.randn(1, 3, 224, 224)
    for k in range(1, 6):
        a = m.forward_head(x, k)
        y = m.forward_tail(a, k)
        print(k, tuple(a.shape), tuple(y.shape))
