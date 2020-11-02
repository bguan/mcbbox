# AUTOGENERATED! DO NOT EDIT! File to edit: 50_subcoco_effdet_icevision_fastai.ipynb (unless otherwise specified).

__all__ = ['SubCocoParser', 'parse_subcoco', 'SaveModelDupBestCallback', 'FastGPUMonitorCallback',
           'gen_transforms_and_learner', 'run_training', 'save_final']

# Cell
import fastai
import glob
import json
import matplotlib.pyplot as plt
import numpy as np
import os
import pickle
import PIL
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
from fastai.test_utils import synth_learner
from fastai.learner import Learner
from fastai.callback.training import GradientAccumulation
from fastai.callback.tracker import Callback, EarlyStoppingCallback, SaveModelCallback
from IPython.utils import io
from pathlib import Path
from PIL import Image, ImageStat
from shutil import copyfile, rmtree
from tqdm import tqdm
from typing import Hashable, List, Tuple, Union

# Cell
import icevision
import icevision.backbones as backbones
import icevision.models
import icevision.models.efficientdet as efficientdet
import icevision.models.rcnn.faster_rcnn as faster_rcnn
import icevision.tfms as tfms

from albumentations import ShiftScaleRotate
from gpumonitor.monitor import GPUStatMonitor
from gpumonitor.callbacks.lightning import PyTorchGpuMonitorCallback
from icevision.core import BBox, ClassMap, BaseRecord
from icevision.parsers import Parser
from icevision.parsers.mixins import LabelsMixin, BBoxesMixin, FilepathMixin, SizeMixin
from icevision.data import Dataset
from icevision.metrics.coco_metric import COCOMetricType, COCOMetric
from icevision.utils import denormalize_imagenet
from icevision.visualize.show_data import *

from .subcoco_utils import *

if torch.cuda.is_available():
    monitor = GPUStatMonitor(delay=1)

print(f"Python ver {sys.version}, torch {torch.__version__}, torchvision {torchvision.__version__}, fastai {fastai.__version__}, icevision {icevision.__version__}")

# Cell
class SubCocoParser(Parser, LabelsMixin, BBoxesMixin, FilepathMixin, SizeMixin):
    def __init__(self, stats:CocoDatasetStats, min_margin_ratio = 0, min_width_height_ratio = 0, quiet = True):
        self.stats = stats
        self.data = [] # list of tuple of form (img_id, wth, ht, bbox, label_id, img_path)
        skipped = 0
        for img_id, imgfname in stats.img2fname.items():
            imgf = stats.img_dir/imgfname
            if not os.path.isfile(imgf):
                skipped += 1
                continue
            width, height = stats.img2sz[img_id]
            bboxs = []
            lids = []
            for lid, x, y, w, h in stats.img2lbs[img_id]:
                if lid != None and box_within_bounds(x, y, w, h, width, height, min_margin_ratio, min_width_height_ratio):
                    b = [int(x), int(y), int(w), int(h)]
                    l = int(lid)
                    bboxs.append(b)
                    lids.append(l)
                else:
                    if not quiet: print(f"warning: skipping lxywh of {lid, x, y, w, h}")

            if len(bboxs) > 0:
                self.data.append( (img_id, width, height, bboxs, lids, imgf, ) )
            else:
                skipped += 1

        print(f"Skipped {skipped} out of {stats.num_imgs} images")

    def __iter__(self):
        yield from iter(self.data)

    def __len__(self):
        return len(self.data)

    def imageid(self, o) -> Hashable:
        return o[0]

    def filepath(self, o) -> Union[str, Path]:
        return o[5]

    def height(self, o) -> int:
        return o[2]

    def width(self, o) -> int:
        return o[1]

    def labels(self, o) -> List[int]:
        return o[4]

    def bboxes(self, o) -> List[BBox]:
        return [BBox.from_xywh(x,y,w,h) for x,y,w,h in o[3]]

    def image_width_height(self, o) -> Tuple[int, int]:
        img_id = o[0]
        return self.stats.img2sz[img_id]

# Cell
def parse_subcoco(stats:CocoDatasetStats)->List[List[BaseRecord]]:
    parser = SubCocoParser(stats, min_margin_ratio = 0.05, min_width_height_ratio = 0.05)
    train_records, valid_records = parser.parse(autofix=False)
    return train_records, valid_records

# Cell
class SaveModelDupBestCallback(SaveModelCallback):
    "Extend SaveModelCallback to save a duplicate with metric added to end of filename"
    def __init__(self, monitor='valid_loss', comp=None, min_delta=0., fname='model', every_epoch=False, with_opt=False, reset_on_fit=True):
        super().__init__(
            monitor=monitor, comp=comp, min_delta=min_delta, reset_on_fit=reset_on_fit,
            fname=fname, every_epoch=every_epoch, with_opt=with_opt,
        )

    def after_epoch(self):
        "Compare the value monitored to its best score and save if best."
        super().after_epoch()
        if self.new_best or self.epoch==0:
            last_saved = self.last_saved_path
            saved_stem = last_saved.stem
            backup_stem = f'{saved_stem}@{self.epoch:03d}_{self.monitor}={self.best:.3f}'
            backup_file = backup_stem+(last_saved.suffix)
            backup_path = last_saved.parent / backup_file
            print(f'Backup {last_saved} as {backup_path}')
            if last_saved != backup_path: copyfile(last_saved, backup_path)

# Cell
class FastGPUMonitorCallback(Callback):
    def __init__(self, delay=1, display_options=None):
        super(FastGPUMonitorCallback, self).__init__()
        self.delay = delay
        self.display_options = display_options if display_options else {}

    def before_epoch(self):
        self.monitor = GPUStatMonitor(self.delay, self.display_options)

    def after_epoch(self):
        self.monitor.stop()
        print("")
        self.monitor.display_average_stats_per_gpu()

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
        tfms.A.Normalize(mean=stats.chn_means/255, std=stats.chn_stds/255)
    ])
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
        SaveModelDupBestCallback(fname=save_model_fname, monitor=monitor_metric),
        EarlyStoppingCallback(monitor=monitor_metric, min_delta=0.001, patience=10),
        FastGPUMonitorCallback(delay=1)
    ]

    learn = efficientdet.fastai.learner(dls=[train_dl, valid_dl], model=model, metrics=metrics, cbs=callbacks)
    learn.freeze()

    return valid_tfms, learn, backbone_name

# Cell
# Wrap in function this doesn't run upon import or when generating docs
def run_training(learn:Learner, min_lr=0.05, head_runs=1, full_runs=1):
    monitor.display_average_stats_per_gpu()
    print(f"Training for {head_runs}+{full_runs} epochs at min LR {min_lr}")
    learn.fine_tune(full_runs, min_lr, freeze_epochs=head_runs)

# Cell
def save_final(learn:Learner, save_model_fpath:str):
    torch.save(learn.model.state_dict(), save_model_fpath)