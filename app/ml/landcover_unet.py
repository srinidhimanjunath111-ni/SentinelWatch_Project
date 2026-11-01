"""Small single-date U-Net for per-pixel land-cover segmentation."""

from __future__ import annotations

try:
    from torch import Tensor, nn
except ImportError as error:  # pragma: no cover - exercised only without ML extras
    raise ImportError("Install requirements-ml.txt to use the land-cover U-Net.") from error

from app.ml.siamese_unet import ConvBlock, Encoder, UpFuse


class LandCoverUNet(nn.Module):
    """Input: [B, C, H, W] scene (C=6, contract band order). Output: [B, classes, H, W] logits."""

    def __init__(
        self, in_channels: int = 6, classes: int = 8, base_channels: int = 32, dropout: float = 0.1
    ):
        super().__init__()
        c = base_channels
        self.encoder = Encoder(in_channels, c)
        self.dropout = nn.Dropout2d(dropout)
        self.bottleneck = ConvBlock(c * 8, c * 8)
        self.up3 = UpFuse(c * 8, c * 4, c * 4)
        self.up2 = UpFuse(c * 4, c * 2, c * 2)
        self.up1 = UpFuse(c * 2, c, c)
        self.head = nn.Conv2d(c, classes, kernel_size=1)

    def forward(self, scene: Tensor) -> Tensor:
        features = self.encoder(scene)
        x = self.bottleneck(self.dropout(features[-1]))
        x = self.up3(x, features[-2])
        x = self.up2(x, features[-3])
        x = self.up1(x, features[-4])
        return self.head(x)

