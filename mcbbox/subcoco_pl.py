# AUTOGENERATED! DO NOT EDIT! File to edit: 30_subcoco_pl.ipynb (unless otherwise specified).

__all__ = ['SubCocoDataset', 'TargetResize', 'SubCocoDataModule', 'FRCNN', 'run_training', 'save_final']

# Cell
import json, os, requests, sys, tarfile, torch, torchvision
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
import pickle
import torch.nn.functional as F
import torch.multiprocessing

from collections import defaultdict
from IPython.utils import io
from pathlib import Path
from PIL import Image
from PIL import ImageStat

from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval

from torch import nn
from torch import optim
from torch.utils.data import DataLoader, random_split

from torchvision import transforms
from torchvision.datasets import CocoDetection
from torchvision.models.detection import FasterRCNN
from torchvision.models.detection.rpn import AnchorGenerator
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor

from tqdm import tqdm
from typing import Hashable, List, Tuple, Union

torch.multiprocessing.set_sharing_strategy('file_system')

# Cell
import pytorch_lightning as pl
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning import LightningDataModule, LightningModule, Trainer
from pytorch_lightning.core.step_result import TrainResult
from .subcoco_utils import *

print(f"Python ver {sys.version}, torch {torch.__version__}, torchvision {torchvision.__version__}, pytorch_lightning {pl.__version__}")

# Cell
class SubCocoDataset(torchvision.datasets.VisionDataset):
    """
    Simulate what torchvision.CocoDetect() returns for target given fastai's coco subsets
    Args:
        root (string): Root directory where images are downloaded to.
        stats (CocoDatasetStats):
        transform (callable, optional): A function/transform that  takes in an PIL image
            and returns a transformed version. E.g, ``transforms.ToTensor``
        target_transform (callable, optional): A function/transform that takes in the
            target and transforms it.
        transforms (callable, optional): A function/transform that takes input sample and its target as entry
            and returns a transformed version.
    """

    def __init__(self, root, stats, transform=None, target_transform=None, transforms=None):
        super(SubCocoDataset, self).__init__(root, transforms, transform, target_transform)
        self.stats = stats
        self.img_ids = list(stats.img2fname.keys())

    def __getitem__(self, index):
        """
        Args:
            index (int): Index
        Returns:
            tuple: Tuple (image, target). target is the object returned by ``coco.loadAnns``.
        """
        img_id = self.img_ids[index] if index < len(self.img_ids) else 0
        img_fname = self.stats.img2fname.get(img_id, None)
        if img_id == None or img_fname ==None:
            return (None, None)

        img = Image.open(os.path.join(self.root, img_fname)).convert('RGB')
        target = { "boxes": [], "labels": [], "image_id": None, "area": [], "iscrowd": 0, "ids": [] }
        count = 0
        lbs = self.stats.img2lbs.get(img_id,[])
        for l, x, y, w, h in lbs:
            count += 1
            target["boxes"].append([x, y, x+w, y+h]) # FRCNN wants x1,y1,x2,y2 format!
            target["labels"].append(l)
            target["image_id"] = img_id
            target["area"].append(w*h)
            anno_id = img_id*1000 + count
            target["ids"].append(anno_id)

        for k, v in target.items():
            target[k] = torch.tensor(v)

        if self.transforms is not None:
            img, target = self.transforms(img, target)
        else:
            if self.transform is not None: img = self.transform(img)
            if self.target_transform is not None: target = self.target_transform(target)

        return img, target

    def __len__(self):
        return self.stats.num_imgs

# Cell
class TargetResize():

    def __init__(self, stats:CocoDatasetStats , to_size:Tuple[int, int]):
        self.stats = stats
        self.to_width, self.to_height = to_size

    def __call__(self, tgt:dict):
        img_id = tgt['image_id']
        img_w, img_h = self.stats.img2sz[img_id.item()]
        tfm_boxes = []
        x_ratio = self.to_width/img_w
        y_ratio = self.to_height/img_h
        tgt['orig_boxes'] = tgt['boxes'] # save to preserve info
        for (bx1, by1, bx2, by2) in tgt['boxes']:
            tx1 = bx1 * x_ratio
            ty1 = by1 * y_ratio
            tx2 = bx2 * x_ratio
            ty2 = by2 * y_ratio
            tfm_boxes.append((tx1,ty1,tx2,ty2))
        tgt['boxes'] = torch.tensor(tfm_boxes)
        return tgt

# Cell
class SubCocoDataModule(LightningDataModule):

    def __init__(self, root, stats, resize=(128,128), bs=32, workers=4, split_ratio=0.8, shuffle=True):
        super().__init__()
        self.dir = root
        self.bs = bs
        self.workers = workers
        self.stats = stats
        self.split_ratio = split_ratio
        self.shuffle = shuffle

        # transforms for images
        transform=transforms.Compose([
            transforms.Resize(resize),
            transforms.ToTensor(),
            # transforms.Normalize(stats.chn_means/255, stats.chn_stds/255) # need to divide by 255
        ])

        tgt_tfm = transforms.Compose([ TargetResize(stats, resize) ])

        # prepare transforms for coco object detection
        dataset = SubCocoDataset(self.dir, self.stats, transform=transform, target_transform=tgt_tfm)
        num_items = len(dataset)
        num_train = int(self.split_ratio*num_items)
        self.train, self.val = random_split(dataset, (num_train, num_items-num_train), generator=torch.Generator().manual_seed(42))
        # print(self.train, self.val)

    def collate_fn(self, batch):
        return tuple(zip(*batch))

    def train_dataloader(self):
        return DataLoader(self.train, batch_size=self.bs, num_workers=self.workers, collate_fn=self.collate_fn, shuffle=self.shuffle)

    def val_dataloader(self):
        return DataLoader(self.val, batch_size=self.bs, num_workers=self.workers, collate_fn=self.collate_fn, shuffle=False)

# Cell
class FRCNN(LightningModule):
    def __init__(self, lbl2name:dict={}, lr:float=1e-3):
        LightningModule.__init__(self)
        self.model = torchvision.models.detection.fasterrcnn_resnet50_fpn(pretrained=True)
        # lock the pretrained model body
        for param in self.model.parameters():
            param.requires_grad = False

        # get number of input features of classifier
        self.in_features = self.model.roi_heads.box_predictor.cls_score.in_features

        #refit another head
        self.categories = [ {'id': l, 'name': n } for l, n in lbl2name.items() ]
        self.num_classes = len(self.categories)
        # replace the pre-trained head with a new one, which is trainable
        self.model.roi_heads.box_predictor = FastRCNNPredictor(self.in_features, self.num_classes+1)

        self.lr = lr

    def unfreeze(self):
        for param in self.model.parameters():
            param.requires_grad = True
        self.lr = self.lr / 10

    def training_step(self, train_batch, batch_idx):
        x, y = train_batch
        losses = self.model(x, y)
        loss = sum(losses.values())
        result = {'loss':loss, 'train_loss':loss}
        return result

    def metrics(self, preds, targets):
        accu = torch.zeros((len(preds), 1))
        for i, (p,t) in enumerate(zip(preds, targets)):
            accu[i] = calc_wavg_F1(p, t, .3, .3)
        return accu

    def validation_step(self, val_batch, batch_idx):
        # validation runs the model in eval mode, so Y is prediction, not losses
        xs, ys = val_batch
        preds = self.model(xs, ys)
        accu = self.metrics(preds, ys)
        return {'val_acc': accu} # should add 'val_acc' accuracy e.g. MAP, MAR etc

    def configure_optimizers(self):
        optimizer = torch.optim.Adam(self.parameters(), lr=self.lr)
        return optimizer

    def validation_epoch_end(self, outputs):
        # called at the end of the validation epoch, but gradient accumulation may result in last row being different size
        val_accs = np.concatenate([ (o['val_acc']).numpy() for o in outputs ])
        avg_acc = val_accs.mean()
        tensorboard_logs = {'val_acc': avg_acc}
        self.log_dict({'val_acc': avg_acc, 'logs': tensorboard_logs})

    def forward(self, x):
        self.model.eval()
        pred = self.model(x)
        return pred

# Cell
def run_training(stats:CocoDatasetStats, modeldir:str, img_dir:str, resume_saved_model_file:str='last.ckpt',
                 img_sz=384, bs=12, acc=4, workers=4, head_runs=50, full_runs=200):

    frcnn_model = FRCNN(lbl2name=stats.lbl2name)

    print(f"Training with image size {img_sz}, auto learning rate, for {head_runs}+{full_runs} epochs.")
    chkpt_cb = ModelCheckpoint(
        filename='FRCNN-subcoco-'+str(img_sz)+'-{epoch:03d}-{val_loss:.2f}-{val_acc:.2f}.ckpt',
        dirpath=modeldir,
        save_last=True,
        monitor='val_acc',
        mode='max',
        save_top_k=-1,
        verbose=True,
    )

    if resume_saved_model_file and os.path.isfile(f'{modeldir}/{resume_saved_model_file}'):
        try:
            frcnn_model.model.load_state_dict(torch.load(resume_saved_model_file))
        except Exception as e:
            print(f'Unexpected error loading previously saved model: {e}')

    # train head only, since using less params, double the bs and half the grad accumulation cycle to use more GPU VRAM
    if head_runs > 0:
        head_dm = SubCocoDataModule(img_dir, stats, resize=(img_sz,img_sz), bs=bs*2, workers=workers)
        trainer = Trainer(gpus=1, auto_lr_find=True, max_epochs=head_runs, default_root_dir = 'models',
                          checkpoint_callback=chkpt_cb, accumulate_grad_batches=max(1,int(acc//2)))
        trainer.fit(frcnn_model, head_dm)

    if full_runs > 0:
        frcnn_model.unfreeze() # allow finetuning of the backbone
        # finetune head and backbone
        full_dm = SubCocoDataModule(img_dir, stats, resize=(img_sz,img_sz), bs=bs, workers=workers)
        trainer = Trainer(gpus=1, auto_lr_find=True, max_epochs=full_runs, default_root_dir = 'models',
                          checkpoint_callback=chkpt_cb, accumulate_grad_batches=max(1,acc))
        trainer.fit(frcnn_model, full_dm)

    return frcnn_model, chkpt_cb.last_model_path

# Cell
def save_final(frcnn_model, model_save_path):
    torch.save(frcnn_model.model.state_dict(), model_save_path)