"""Compact shared-encoder Siamese U-Net for bitemporal change segmentation.

The model uses one encoder for both acquisition dates (weight sharing), fuses the
absolute feature differences at four scales, and decodes a single change logit.
It is intentionally small enough for a final-year project laptop baseline.
"""

from __future__ import annotations

try:
    import torch
    import torch.nn.functional as F
    from torch import Tensor, nn
except ImportError as error:  # pragma: no cover - exercised only without ML extras
    raise ImportError("Install requirements-ml.txt to use the Siamese U-Net.") from error


class ConvBlock(nn.Module):
    def __init__(self, in_channels: int, out_channels: int):
        super().__init__()
        self.layers = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, 3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, 3, padding=1, bias=False),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
        )

    def forward(self, x: Tensor) -> Tensor:
        return self.layers(x)


class Encoder(nn.Module):
    def __init__(self, base_channels: int = 32):
        super().__init__()
        c = base_channels
        self.blocks = nn.ModuleList(
            [ConvBlock(3, c), ConvBlock(c, c * 2), ConvBlock(c * 2, c * 4), ConvBlock(c * 4, c * 8)]
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
    def __init__(self, in_channels: int, skip_channels: int, out_channels: int):
        super().__init__()
        self.block = ConvBlock(in_channels + skip_channels, out_channels)

    def forward(self, x: Tensor, skip: Tensor) -> Tensor:
        x = F.interpolate(x, size=skip.shape[-2:], mode="bilinear", align_corners=False)
        return self.block(torch.cat([x, skip], dim=1))


class SiameseUNet(nn.Module):
    """Input: two [B, 3, H, W] images. Output: [B, 1, H, W] change logits."""

    def __init__(self, base_channels: int = 32):
        super().__init__()
        c = base_channels
        self.encoder = Encoder(c)
        self.bottleneck = ConvBlock(c * 8, c * 8)
        self.up3 = UpFuse(c * 8, c * 4, c * 4)
        self.up2 = UpFuse(c * 4, c * 2, c * 2)
        self.up1 = UpFuse(c * 2, c, c)
        self.head = nn.Conv2d(c, 1, kernel_size=1)

    def forward(self, before: Tensor, after: Tensor) -> Tensor:
        before_features = self.encoder(before)
        after_features = self.encoder(after)
        diffs = [torch.abs(a - b) for a, b in zip(before_features, after_features, strict=True)]
        x = self.bottleneck(diffs[-1])
        x = self.up3(x, diffs[-2])
        x = self.up2(x, diffs[-3])
        x = self.up1(x, diffs[-4])
        return self.head(x)
