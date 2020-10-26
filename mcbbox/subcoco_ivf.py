# AUTOGENERATED! DO NOT EDIT! File to edit: 20_subcoco_ivf.ipynb (unless otherwise specified).

__all__ = ['parse_subcoco', 'gen_transforms_and_learner', 'run_training', 'save_final']

# Cell
import fastai
import glob
import icevision
import icevision.backbones as backbones
import icevision.models
import icevision.models.efficientdet as efficientdet
import icevision.models.rcnn.faster_rcnn as faster_rcnn
import icevision.tfms as tfms
import json
import matplotlib.pyplot as plt
import numpy as np
import os
import pickle
import PIL
import pytorch_lightning as pl
import re
import requests
import tarfile
import sys
import torch
import torch.multiprocessing
import torchvision
import xml.etree.ElementTree

from albumentations import ShiftScaleRotate
from collections import defaultdict
from functools import reduce
from fastai.learner import Learner
from fastai.callback.training import GradientAccumulation
from fastai.callback.tracker import EarlyStoppingCallback, SaveModelCallback
from icevision.core import ClassMap, BaseRecord
from icevision.data import Dataset
from icevision.metrics.coco_metric import COCOMetricType, COCOMetric

from icevision.utils import denormalize_imagenet
from icevision.visualize.show_data import *
from IPython.utils import io
from pathlib import Path
from PIL import Image, ImageStat
from tqdm import tqdm
from typing import Hashable, List, Tuple, Union

from .subcoco_utils import *

torch.multiprocessing.set_sharing_strategy('file_system')

# Cell
def parse_subcoco(stats:CocoDatasetStats)->List[List[BaseRecord]]:
    parser = SubCocoParser(stats, min_margin_ratio = 0.05, min_width_height_ratio = 0.05)
    train_records, valid_records = parser.parse(autofix=False)
    return train_records, valid_records

# Cell
def gen_transforms_and_learner(stats:CocoDatasetStats,
                               train_records:List[BaseRecord],
                               valid_records:List[BaseRecord],
                               img_sz=128,
                               bs=4,
                               acc_cycs=8,
                               num_workers=2):
    train_tfms = tfms.A.Adapter([
        *tfms.A.aug_tfms(
            size=img_sz,
            presize=img_sz+128,
            shift_scale_rotate = tfms.A.ShiftScaleRotate(shift_limit=.025, scale_limit=0.025, rotate_limit=9)
        ),
        tfms.A.Normalize()
    ]) # mean=stats.chn_means, std=stats.chn_stds
    valid_tfms = tfms.A.Adapter([*tfms.A.resize_and_pad(img_sz), tfms.A.Normalize()])
    train_ds = Dataset(train_records, train_tfms)
    valid_ds = Dataset(valid_records, valid_tfms)
    # Using gradient accumulation to process minibatch of 32 images in 8 loops, i.e. 8 images per loop.
    # I ran this model w img 512x512x3 on my Dell XPS15 w GTX-1050 with 4GB VRAM, 16GM RAM, ~20min/epoch.
    backbone_name = "tf_efficientdet_lite0"
    model = efficientdet.model(model_name=backbone_name, img_size=img_sz, num_classes=len(stats.lbl2name))
    train_dl = efficientdet.train_dl(train_ds, batch_size=bs, num_workers=num_workers, shuffle=True)
    valid_dl = efficientdet.valid_dl(valid_ds, batch_size=bs, num_workers=max(1,num_workers//2), shuffle=False)

    monitor_metric = 'COCOMetric'
    metrics = [ COCOMetric(metric_type=COCOMetricType.bbox)]

    save_model_fname=f'{backbone_name}-{img_sz}'
    callbacks=[
        GradientAccumulation(bs*acc_cycs),
        SaveModelCallback(fname=save_model_fname, monitor=monitor_metric),
        EarlyStoppingCallback(monitor=monitor_metric, min_delta=0.001, patience=10)
    ]

    learn = efficientdet.fastai.learner(dls=[train_dl, valid_dl], model=model, metrics=metrics, cbs=callbacks)
    learn.freeze()

    return valid_tfms, learn, backbone_name

# Cell
# Wrap in function this doesn't run upon import or when generating docs
def run_training(learn:Learner, min_lr=0.05, head_runs=1, full_runs=1):
    print(f"Training for {head_runs}+{full_runs} epochs at min LR {min_lr}")
    learn.fine_tune(full_runs, min_lr, freeze_epochs=head_runs)

# Cell
def save_final(learn:Learner, save_model_fpath:str):
    torch.save(learn.model.state_dict(), save_model_fpath)