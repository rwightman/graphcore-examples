# Copyright (c) 2021 Graphcore Ltd. All rights reserved.

import pytest

import numpy as np
import torch
import torch.nn as nn

from models.neck.yolov4_p5 import Yolov4P5Neck
from poptorch import inferenceModel, trainingModel


class TestYolov4P5Neck:
    """Tests for training and inference of Yolov4 P5 neck."""
    @pytest.mark.category2
    @pytest.mark.ipus(1)
    def test_training(self):
        model = Yolov4P5Neck(nn.ReLU(), calculate_loss=True)
        optimizer = torch.optim.SGD(
            model.parameters(), lr=0.01, momentum=0.9, nesterov=False)
        model = trainingModel(model.half(), optimizer=optimizer)
        x = (torch.Tensor(np.random.randn(1, 1024, 2, 2)), torch.Tensor(np.random.randn(1, 512, 4, 4)), torch.Tensor(np.random.randn(1, 256, 8, 8)))
        _, _, _, loss = model(x)
        assert torch.numel(loss) == 1

    @pytest.mark.category1
    @pytest.mark.ipus(1)
    def test_inference(self):
        model = Yolov4P5Neck(nn.ReLU())
        model = inferenceModel(model.half())
        x = (torch.Tensor(np.random.randn(1, 1024, 2, 2)), torch.Tensor(np.random.randn(1, 512, 4, 4)), torch.Tensor(np.random.randn(1, 256, 8, 8)))
        y = model(x)
        assert y[0].shape == torch.Size([1, 1024, 2, 2])
        assert y[1].shape == torch.Size([1, 512, 4, 4])
        assert y[2].shape == torch.Size([1, 256, 8, 8])
