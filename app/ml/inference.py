"""Optional PyTorch inference with a safe non-ML fallback handled by analysis.py."""

from __future__ import annotations

from pathlib import Path

import numpy as np


class SiameseInference:
    def __init__(self, checkpoint: Path, threshold: float = 0.50):
        self.checkpoint = checkpoint
        self.threshold = threshold
        self.available = False
        self.device = "cpu"
        self.model = None
        if not checkpoint.exists():
            return
        try:
            import torch

            from app.ml.siamese_unet import SiameseUNet

            device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
            payload = torch.load(checkpoint, map_location=device, weights_only=False)
            config = payload.get("config", {}) if isinstance(payload, dict) else {}
            state = payload.get("model_state", payload) if isinstance(payload, dict) else payload
            self.model = SiameseUNet(base_channels=int(config.get("base_channels", 32))).to(device)
            self.model.load_state_dict(state)
            self.model.eval()
            self.device = str(device)
            self.available = True
        except (ImportError, OSError, RuntimeError, KeyError, ValueError):
            # Invalid/mismatched checkpoints must not stop evidence capture; the caller
            # records the explicit analysis method used.
            self.available = False
            self.model = None

    def predict(self, before: np.ndarray, after: np.ndarray) -> np.ndarray:
        if not self.available or self.model is None:
            raise RuntimeError("A compatible Siamese checkpoint is not available.")
        import torch
        import torch.nn.functional as F

        height, width = before.shape[:2]
        # Models are trained on square crops. Resize up to a minimum meaningful size,
        # then restore probabilities to original spatial coordinates.
        side = max(64, min(1024, max(height, width)))
        batch_a = torch.from_numpy(before.transpose(2, 0, 1)).float().unsqueeze(0).to(self.device)
        batch_b = torch.from_numpy(after.transpose(2, 0, 1)).float().unsqueeze(0).to(self.device)
        batch_a = F.interpolate(batch_a, size=(side, side), mode="bilinear", align_corners=False)
        batch_b = F.interpolate(batch_b, size=(side, side), mode="bilinear", align_corners=False)
        with torch.inference_mode():
            logits = self.model(batch_a, batch_b)
            probabilities = torch.sigmoid(logits)
            probabilities = F.interpolate(
                probabilities, size=(height, width), mode="bilinear", align_corners=False
            )
        return probabilities.squeeze().cpu().numpy().astype(np.float32)
