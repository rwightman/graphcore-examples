# Copyright (c) 2021 Graphcore Ltd. All rights reserved.

import numpy as np
import pytest
import torch
from models.head.yolov4_head import Yolov4Head
from poptorch import inferenceModel, trainingModel
from utils.anchors import AnchorBoxes


class TestYolov4Head:
    """Tests for training and inference of Yolov4 head."""
    anchors = AnchorBoxes(widths=torch.tensor(np.array([4, 5, 6]), requires_grad=False),
                          heights=torch.tensor(np.array([6, 7, 8]), requires_grad=False))
    input_tensor = torch.Tensor(np.random.randn(1, 32, 13, 13))

    @pytest.mark.ipus(1)
    @pytest.mark.category1
    def test_training(self):
        model = Yolov4Head(self.anchors, num_input_channels=32,
                           num_classes=3, stride=8, calculate_loss=True, precision=torch.half)
        optimizer = torch.optim.SGD(
            model.parameters(), lr=0.01, momentum=0.9, nesterov=False)
        model = trainingModel(model.half(), optimizer=optimizer)
        loss = model(self.input_tensor)
        assert torch.numel(loss) == 1

    @pytest.mark.ipus(1)
    @pytest.mark.category1
    def test_inference(self):
        model = Yolov4Head(self.anchors, num_input_channels=32,
                           num_classes=3, stride=8, calculate_loss=False, precision=torch.half)
        model = inferenceModel(model.half().eval())
        y = model(self.input_tensor)
        assert y.shape == torch.Size(
            [1, len(self.anchors.widths) * 13 * 13, 8])
