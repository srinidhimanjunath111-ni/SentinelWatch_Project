"""Shared-encoder Siamese U-Net (FC-Siam-diff style) for bitemporal change segmentation.

One encoder processes both dates (weight sharing), the absolute feature differences at four
scales are fused, and a decoder produces a single change logit per pixel. Because |a - b| is
symmetric, the model cannot learn an ordering: swapping the dates gives the same features.
"""

from __future__ import annotations

import math

try:
    import torch
    import torch.nn.functional as F
    from torch import Tensor, nn
except ImportError as error:  # pragma: no cover - exercised only without ML extras
    raise ImportError("Install requirements-ml.txt to use the Siamese U-Net.") from error


def _norm_layer(kind: str, channels: int) -> nn.Module:
    if kind == "batch":
        return nn.BatchNorm2d(channels)
    if kind == "group":
        # Group norm has no running statistics, so eval matches training and depends on no batch.
        return nn.GroupNorm(math.gcd(8, channels), channels)
    raise ValueError(f"Unknown norm {kind!r}")


class ConvBlock(nn.Module):
    def __init__(self, in_channels: int, out_channels: int, norm: str = "batch"):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, padding=1, bias=False),
            _norm_layer(norm, out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, 3, padding=1, bias=False),
            _norm_layer(norm, out_channels),
            nn.ReLU(inplace=True),
        )

    def forward(self, x: Tensor) -> Tensor:
        return self.layers(x)


class Encoder(nn.Module):
    def __init__(self, in_channels: int = 6, base_channels: int = 32, norm: str = "batch"):
        super().__init__()
        c = base_channels
        self.blocks = nn.ModuleList(
            [
                ConvBlock(in_channels, c, norm),
                ConvBlock(c, c * 2, norm),
                ConvBlock(c * 2, c * 4, norm),
                ConvBlock(c * 4, c * 8, norm),
            ]
        )
        self.pool = nn.MaxPool2d(2)

    def forward(self, x: Tensor) -> list[Tensor]:
        features: list[Tensor] = []
        for index, block in enumerate(self.blocks):
            x = block(x)
            features.append(x)
            if index < len(self.blocks) - 1:
                x = self.pool(x)
        return features


class UpFuse(nn.Module):
    def __init__(self, in_channels: int, skip_channels: int, out_channels: int, norm: str = "batch"):
        super().__init__()
        self.block = ConvBlock(in_channels + skip_channels, out_channels, norm)

    def forward(self, x: Tensor, skip: Tensor) -> Tensor:
        x = F.interpolate(x, size=skip.shape[-2:], mode="bilinear", align_corners=False)
        return self.block(torch.cat([x, skip], dim=1))


class SiameseUNet(nn.Module):
    """Input: two [B, C, H, W] images (C=6, contract band order). Output: [B, 1, H, W] logits."""

    def __init__(
        self,
        in_channels: int = 6,
        base_channels: int = 32,
        dropout: float = 0.0,
        norm: str = "batch",
    ):
        super().__init__()
        c = base_channels
        self.encoder = Encoder(in_channels, c, norm)
        self.dropout = nn.Dropout2d(dropout)
        self.bottleneck = ConvBlock(c * 8, c * 8, norm)
        self.up3 = UpFuse(c * 8, c * 4, c * 4, norm)
        self.up2 = UpFuse(c * 4, c * 2, c * 2, norm)
        self.up1 = UpFuse(c * 2, c, c, norm)
        self.head = nn.Conv2d(c, 1, kernel_size=1)

    def forward(self, before: Tensor, after: Tensor) -> Tensor:
        before_features = self.encoder(before)
        after_features = self.encoder(after)
        diffs = [torch.abs(a - b) for a, b in zip(before_features, after_features, strict=True)]
        x = self.bottleneck(self.dropout(diffs[-1]))
        x = self.up3(x, diffs[-2])
        x = self.up2(x, diffs[-3])
        x = self.up1(x, diffs[-4])
        return self.head(x)
