import torch
import torchvision

import torch.nn as nn
import torch.nn.functional as F

from collections import OrderedDict
from torchvision.models._utils import IntermediateLayerGetter
from .feature_attention import ScaleFeatureSelection


class BackboneBase(nn.Module):
    def __init__(self, backbone: nn.Module, cfg):
        super().__init__()
        return_layers = {
            "layer1": "layer1",
            "layer2": "layer2",
            "layer3": "layer3",
            "layer4": "layer4",
        }

        self.body = IntermediateLayerGetter(backbone, return_layers=return_layers)

    def forward(self, tensor):
        xs = self.body(tensor)
        return xs


class Backbone(BackboneBase):
    """ResNet backbone with frozen BatchNorm."""

    def __init__(
        self,
        cfg,
    ):
        backbone = getattr(torchvision.models, cfg.NAME)(
            replace_stride_with_dilation=[False, False, cfg.DILATION],
            pretrained=False,
        )
        super().__init__(backbone, cfg)


class DBNetDecoder(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.d_model = cfg.HIDDEN_DIM
        self.n_layers = len(cfg.IN_CHANNELS)
        self.k = cfg.K
        self.training = True
        self.input_proj = nn.ModuleDict(
            {
                "layer1": nn.Conv2d(cfg.IN_CHANNELS[0], self.d_model, 1, bias=False),
                "layer2": nn.Conv2d(cfg.IN_CHANNELS[1], self.d_model, 1, bias=False),
                "layer3": nn.Conv2d(cfg.IN_CHANNELS[2], self.d_model, 1, bias=False),
                "layer4": nn.Conv2d(cfg.IN_CHANNELS[3], self.d_model, 1, bias=False),
            }
        )

        self.upsample_2x = nn.Upsample(
            scale_factor=2, mode="bilinear", align_corners=False
        )

        self.out_proj = nn.ModuleDict(
            {
                "layer1": nn.Conv2d(
                    self.d_model, self.d_model // 4, 3, padding=1, bias=False
                ),
                "layer2": nn.Sequential(
                    nn.Conv2d(
                        self.d_model, self.d_model // 4, 3, padding=1, bias=False
                    ),
                    nn.Upsample(scale_factor=2, mode="bilinear", align_corners=False),
                ),
                "layer3": nn.Sequential(
                    nn.Conv2d(
                        self.d_model, self.d_model // 4, 3, padding=1, bias=False
                    ),
                    nn.Upsample(scale_factor=4, mode="bilinear", align_corners=False),
                ),
                "layer4": nn.Sequential(
                    nn.Conv2d(
                        self.d_model, self.d_model // 4, 3, padding=1, bias=False
                    ),
                    nn.Upsample(scale_factor=4, mode="bilinear", align_corners=False),
                ),
            }
        )

        self.binarize = nn.Sequential(
            nn.Conv2d(self.d_model, self.d_model // 4, 3, padding=1, bias=False),
            nn.BatchNorm2d(self.d_model // 4),
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(self.d_model // 4, self.d_model // 4, 2, 2),
            nn.BatchNorm2d(self.d_model // 4),
            nn.ReLU(inplace=True),
            nn.ConvTranspose2d(self.d_model // 4, 1, 2, 2),
            nn.Sigmoid(),
        )

        self.adaptive = self.cfg.ADAPTIVE
        self.serial = self.cfg.SERIAL
        if self.adaptive:
            self.thresh = self._init_thresh(
                self.d_model, serial=self.cfg.SERIAL, smooth=self.cfg.SMOOTH, bias=False
            )
            self.thresh.apply(self.weights_init)

        self.binarize.apply(self.weights_init)

        for layer in self.input_proj.values():
            layer.apply(self.weights_init)

        for layer in self.out_proj.values():
            layer.apply(self.weights_init)

        self.concat_attention = ScaleFeatureSelection(
            self.d_model,
            self.d_model // 4,
            attention_type="scale_channel_spatial",
        )

    def weights_init(self, m):
        classname = m.__class__.__name__
        if classname.find("Conv") != -1:
            nn.init.kaiming_normal_(m.weight.data)
        elif classname.find("BatchNorm") != -1:
            m.weight.data.fill_(1.0)
            m.bias.data.fill_(1e-4)

    def _init_thresh(self, inner_channels, serial=False, smooth=False, bias=False):
        in_channels = inner_channels
        if serial:
            in_channels += 1
        self.thresh = nn.Sequential(
            nn.Conv2d(in_channels, inner_channels // 4, 3, padding=1, bias=bias),
            nn.BatchNorm2d(inner_channels // 4),
            nn.ReLU(inplace=True),
            self._init_upsample(
                inner_channels // 4, inner_channels // 4, smooth=smooth, bias=bias
            ),
            nn.BatchNorm2d(inner_channels // 4),
            nn.ReLU(inplace=True),
            self._init_upsample(inner_channels // 4, 1, smooth=smooth, bias=bias),
            nn.Sigmoid(),
        )
        return self.thresh

    def _init_upsample(self, in_channels, out_channels, smooth=False, bias=False):
        if smooth:
            inter_out_channels = out_channels
            if out_channels == 1:
                inter_out_channels = in_channels
            module_list = [
                nn.Upsample(scale_factor=2, mode="nearest"),
                nn.Conv2d(in_channels, inter_out_channels, 3, 1, 1, bias=bias),
            ]
            if out_channels == 1:
                module_list.append(
                    nn.Conv2d(
                        in_channels,
                        out_channels,
                        kernel_size=1,
                        stride=1,
                        padding=0,
                        bias=False,
                    )
                )

            return nn.Sequential(*module_list)
        else:
            return nn.ConvTranspose2d(in_channels, out_channels, 2, 2)

    def step_function(self, x, y):
        return torch.reciprocal(1 + torch.exp(-self.k * (x - y)))

    def forward(self, features):
        for layer, feature in features.items():
            features[layer] = self.input_proj[layer](feature)

        layers = ["layer4", "layer3", "layer2", "layer1"]
        for i in range(self.n_layers - 1):
            feature_bottom = features[layers[i]]
            feature_top = features[layers[i + 1]]

            bh, bw = feature_bottom.shape[-2:]
            th, tw = feature_top.shape[-2:]

            if bh != th or bw != tw:
                feature_bottom = F.interpolate(
                    feature_bottom, size=(th, tw), mode="bilinear", align_corners=False
                )

            features[layers[i + 1]] = feature_bottom + feature_top

        fp = []
        for layer, feature in features.items():
            fp.append(self.out_proj[layer](feature))
        fuse = torch.cat(fp[::-1], dim=1)

        binary = self.binarize(fuse)
        result = OrderedDict(binary=binary)

        if self.adaptive:
            if self.serial:
                fuse = torch.cat(
                    (fuse, nn.functional.interpolate(binary, fuse.shape[2:])), 1
                )
            thresh = self.thresh(fuse)
            thresh_binary = self.step_function(binary, thresh)
            result.update(thresh=thresh, thresh_binary=thresh_binary)

        return result


class DBNet(nn.Module):
    def __init__(self, cfg):
        super().__init__()
        self.cfg = cfg
        self.backbone = Backbone(cfg.BACKBONE)
        self.decoder = DBNetDecoder(cfg.DECODER)

    def forward(self, tensor):
        features = self.backbone(tensor)
        xs = self.decoder(features)
        return xs