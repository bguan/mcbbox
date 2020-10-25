# AUTOGENERATED! DO NOT EDIT! File to edit: nbs/10_subcoco_utils.ipynb (unless otherwise specified).

__all__ = ['fetch_data', 'CocoDatasetStats', 'empty_list', 'load_stats', 'box_within_bounds', 'SubCocoParser',
           'is_notebook', 'overlay_img_bbox', 'bbox_to_rect', 'label_for_bbox', 'SubCocoDataset', 'TargetResize',
           'SubCocoDataModule', 'SubCocoWrapper', 'iou_calc', 'accuracy_1img', 'FRCNN', 'digest_pred']

# Cell
import albumentations as A
import fastai
import icevision
import json
import matplotlib.pyplot as plt
import matplotlib.colors as mcolors
import numpy as np
import os
import pickle
import PIL
import pytorch_lightning as pl
import re
import requests
import sys
import tarfile
import torch
import torchvision

from collections import defaultdict
from functools import reduce
from icevision.core import BBox
from icevision.parsers import Parser
from icevision.parsers.mixins import LabelsMixin, BBoxesMixin, FilepathMixin, SizeMixin
from IPython.utils import io
from pathlib import Path
from PIL import Image, ImageStat
from pycocotools.coco import COCO
from pycocotools.cocoeval import COCOeval
from pytorch_lightning.callbacks import ModelCheckpoint
from pytorch_lightning import LightningDataModule, LightningModule, Trainer
from pytorch_lightning.core.step_result import TrainResult
from torch.utils.data import DataLoader, random_split
from torchvision import transforms
from torchvision.models.detection.faster_rcnn import FastRCNNPredictor
from tqdm import tqdm
from typing import Hashable, List, Tuple, Union

# Cell
def fetch_data(url:str, datadir: Path, tgt_fname:str, chunk_size:int=8*1024, quiet=False):
    dest = datadir/tgt_fname
    if not quiet: print(f"Downloading from {url} to {dest}...")
    with requests.get(url, stream=True, timeout=10) as response:
        content_len = int(response.headers['content-length'])
        with open(dest, 'wb') as f:
            with tqdm(total=content_len) as pbar:
                nbytes = 0
                num_chunks = 0
                for chunk in response.iter_content(chunk_size=chunk_size):
                    chunk_len = len(chunk)
                    nbytes += chunk_len
                    num_chunks += 1
                    f.write(chunk)
                    pbar.update(chunk_len)

    with tarfile.open(dest, 'r') as tar:
        extracted = []
        for item in tar:
            tar.extract(item, datadir)
            extracted.append(item.name)

    if not quiet: print(f"Downloaded {nbytes} from {url} to {dest}, extracted in {datadir}: {extracted[:3]},...,{extracted[-3:]}")

# Cell
class CocoDatasetStats():
    # num_cats
    # num_imgs
    # num_bboxs
    # cat2name
    # class_map
    # lbl2cat
    # cat2lbl
    # img2fname
    # imgs
    # img2l2bs
    # img2lbs
    # l2ibs
    # avg_ncats_per_img
    # avg_nboxs_per_img
    # avg_nboxs_per_cat
    # img2sz
    # chn_means
    # chn_stds
    # avg_width
    # avg_height
    def __init__(self, ann:dict, img_dir:Path):

        self.img_dir = img_dir
        self.num_cats = len(ann['categories'])
        self.num_imgs = len(ann['images'])
        self.num_bboxs = len(ann['annotations'])

        # build cat id to name, assign FRCNN
        self.cat2name = { c['id']: c['name'] for c in ann['categories'] }

        # need to translate coco subset category id to indexable label id
        # expected labels w 0 = background
        self.lbl2cat = { i: cid for i, cid in enumerate(self.cat2name.keys(),1) }
        self.cat2lbl = { cid: l for l, cid in self.lbl2cat.items() }
        self.lbl2name = { l:self.cat2name[cid] for l, cid in self.lbl2cat.items() }
        self.lbl2cat[0] = 0 # background
        self.cat2lbl[0] = 0 # background

        # img_id to file map
        self.img2fname = { img['id']:img['file_name'] for img in ann['images'] }
        self.imgs = [ { 'id':img_id, 'file_name':img_fname } for (img_id, img_fname) in self.img2fname.items() ]

        # compute Images per channel means and std deviation using PIL.ImageStat.Stat()

        self.img2sz = {}
        n = 0
        mean = np.zeros((3,))
        stddev = np.zeros((3,))
        avgw = 0
        avgh = 0
        for img in tqdm(self.imgs):
            img_id = img['id']
            fname = img_dir/img['file_name']
            n = n + 1
            img = Image.open(fname)
            istat = ImageStat.Stat(img)
            width, height = img.size
            avgw = (width + (n-1)*avgw)/n
            avgh = (height + (n-1)*avgh)/n
            mean = (istat.mean + (n-1)*mean)/n
            stddev = (istat.stddev + (n-1)*stddev)/n
            self.img2sz[img_id] = (width, height)

        self.chn_means = mean
        self.chn_stds = stddev
        self.avg_width = avgw
        self.avg_height = avgh

        # build up some maps for later analysis
        self.img2l2bs = {}
        self.img2lbs = defaultdict(empty_list)
        self.l2ibs = defaultdict(empty_list)
        anno_id = 0
        for a in ann['annotations']:
            img_id = a['image_id']
            cat_id = a['category_id']
            lbl_id = self.cat2lbl[cat_id]
            l2bs_for_img = self.img2l2bs.get(img_id, { l:[] for l in range(1+len(self.cat2name))})
            (x, y, w, h) = a['bbox']
            b = (x, y, w, h)
            ib = (img_id, *b)
            lb = (lbl_id, *b)
            l2bs_for_img[lbl_id].append(b)
            self.l2ibs[lbl_id].append(ib)
            self.img2lbs[img_id].append(lb)
            self.img2l2bs[img_id] = l2bs_for_img

        acc_ncats_per_img = 0.0
        acc_nboxs_per_img = 0.0
        for img_id, l2bs in self.img2l2bs.items():
            acc_ncats_per_img += len(l2bs)
            for lbl_id, bs in l2bs.items():
                acc_nboxs_per_img += len(bs)

        self.avg_ncats_per_img = acc_ncats_per_img/self.num_imgs
        self.avg_nboxs_per_img = acc_nboxs_per_img/self.num_imgs

        acc_nboxs_per_cat = 0.0
        for lbl_id, ibs in self.l2ibs.items():
            acc_nboxs_per_cat += len(ibs)

        self.avg_nboxs_per_cat = acc_nboxs_per_cat/self.num_cats

def empty_list()->list: return [] # cannot use lambda as pickling will fail when saving models

# Cell
def load_stats(ann:dict, img_dir:Path, force_reload:bool=False)->CocoDatasetStats:
    stats_fpath = img_dir/'stats.pkl'
    stats = None
    if os.path.isfile(stats_fpath) and not force_reload:
        try:
            stats = pickle.load( open(stats_fpath, "rb" ) )
        except Exception as e:
            print(f"Failed to read precomputed stats: {e}")

    if stats == None:
        stats = CocoDatasetStats(ann, img_dir)
        pickle.dump(stats, open(stats_fpath, "wb" ) )

    return stats

# Cell
def box_within_bounds(x, y, w, h, width, height, min_margin_ratio, min_width_height_ratio):
    min_width = min_width_height_ratio*width
    min_height = min_width_height_ratio*height
    if w < min_width or h < min_height:
        return False
    top_margin = min_margin_ratio*height
    bottom_margin = height - top_margin
    left_margin = min_margin_ratio*width
    right_margin = width - left_margin
    if x < left_margin or x > right_margin:
        return False
    if y < top_margin or y > bottom_margin:
        return False
    return True

class SubCocoParser(Parser, LabelsMixin, BBoxesMixin, FilepathMixin, SizeMixin):
    def __init__(self, stats:CocoDatasetStats, min_margin_ratio = 0, min_width_height_ratio = 0, quiet = True):
        self.stats = stats
        self.data = [] # list of tuple of form (img_id, wth, ht, bbox, label_id, img_path)
        skipped = 0
        for img_id, imgfname in stats.img2fname.items():
            imgf = stats.img_dir/imgfname
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
def is_notebook():
    try:
        shell = get_ipython().__class__.__name__
        if shell == 'ZMQInteractiveShell':
            return True   # Jupyter notebook or qtconsole
        elif shell == 'TerminalInteractiveShell':
            return False  # Terminal running IPython
        else:
            return False  # Other type (?)
    except NameError:
        return False      # Probably standard Python interpreter

# Cell
def overlay_img_bbox(img:Image, l2bs: dict, l2name: dict):
    l2color = { l: colname for (l, colname) in zip(l2bs.keys(), mcolors.TABLEAU_COLORS.keys()) }
    fig = plt.figure(figsize=(16,10))
    fig = plt.imshow(img)
    for l, bs in l2bs.items():
        for b in bs:
            label_for_bbox(b, l2name[l])
            fig.axes.add_patch(bbox_to_rect(b, l2color[l]))

def bbox_to_rect(bbox:Tuple[int, int, int, int], color:str):
    return plt.Rectangle(
        xy=(bbox[0], bbox[1]), width=bbox[2], height=bbox[3],
        fill=False, edgecolor=color, linewidth=2)

def label_for_bbox(bbox:Tuple[int, int, int, int], label:str):
    return plt.text(bbox[0], bbox[1], f"{label}", color='#ffffff', fontsize=12)

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

    def __init__(self, root, stats, resize=(384,384), bs=32, workers=4, split_ratio=0.8):
        super().__init__()
        self.dir = root
        self.bs = bs
        self.workers = workers
        self.stats = stats
        self.split_ratio = split_ratio

        # transforms for images
        transform=transforms.Compose([
            transforms.Resize(resize),
            transforms.ToTensor(),
            transforms.Normalize(stats.chn_means/255, stats.chn_stds/255) # need to divide by 255
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
        return DataLoader(self.train, batch_size=self.bs, num_workers=self.workers, collate_fn=self.collate_fn)

    def val_dataloader(self):
        return DataLoader(self.val, batch_size=self.bs, num_workers=self.workers, collate_fn=self.collate_fn)

# Cell
class SubCocoWrapper():
    def __init__(self, categories, p, t):
        # turn tgt: { "boxes": [...], "labels": [...], "image_id": "xxx", "area": [...], "iscrowd": 0 }
        # into COCO with dataset dict of this form:
        # { images: [], categories: [], annotations: [{"image_id": int, "category_id": int, "bbox": (x,y,width,height)}, ...] }
        # see https://github.com/cocodataset/cocoapi/blob/master/PythonAPI/pycocotools/coco.py
        with io.capture_output() as captured:
            self.target = COCO()
            img_id = int(t["image_id"]) # could be tensor, cast to int
            images = [ {'id': img_id, 'file_name': f"{img_id:012d}.jpg"} ]
            self.target.dataset["images"] = images
            self.target.dataset["categories"] = categories
            self.target.dataset["annotations"] = []
            for bi, b in enumerate(t["boxes"]):
                x, y, w, h = b
                cat_id = t["labels"][bi]
                anno_id = t["ids"][bi]
                self.target.dataset["annotations"].append({'id': anno_id, 'image_id': img_id, 'category_id': cat_id, 'bbox': b})
            self.target.createIndex()

            # [ {'boxes': tensor([[100.5,  39.7, 109.1,  52.7], [110.9,  41.1, 120.4,  54.4], [ 36.6,  56.1,  46.9,  74.0]], device='cuda:0'),
            #    'labels': tensor([1, 1, 1], device='cuda:0'),
            #    'scores': tensor([0.7800, 0.7725, 0.7648], device='cuda:0')}, ...]
            # numpy array [Nx7] of {imageID,x1,y1,w,h,score,class}
            pna = np.zeros((len(p["boxes"]), 7))
            for bi, b in enumerate(p["boxes"]):
                pna[bi]=(img_id, *b, p["scores"][bi], p["labels"][bi])

            anns = self.target.loadNumpyAnnotations(pna)
            self.prediction = COCO()
            self.prediction.dataset["images"] = images
            self.prediction.dataset["categories"] = categories
            self.prediction.dataset["annotations"] = anns

    def targetCoco(self):
        return self.target

    def predictionCoco(self):
        return self.prediction

# Cell
def iou_calc(x1,y1,w1,h1, x2,y2,w2,h2):
    r1 = x1+w1 # right of box1
    b1 = y1+h1 # bottom of box1
    r2 = x2+w2 # right of box2
    b2 = y2+h2 # bottom of box2
    a1 = 1.0*w1*h1
    a2 = 1.0*w2*h2
    ia = 0.0 # intercept
    if x1 <= x2 <= r1:
        if y1 <= y2 <= b1:
            ia = (r1-x2)*(b1-y2)
        elif y1 <= b2 <= b1:
            ia = (r1-x2)*(b1-b2)
    elif x1 <= r2 <= r1:
        if y1 <= y2 <= b1:
            ia = (r1-r2)*(b1-y2)
        elif y1 <= b2 <= b1:
            ia = (r1-r2)*(b1-b2)
    #print(a1, a2, ia)
    iou = ia/(a1+a2-ia)
    return iou

def accuracy_1img(pred, tgt, scut=0.5, ithr=0.5):
    scut = 0.6
    ithr = 0.1
    pscores = pred['scores']
    pidxs = (pscores > scut).nonzero(as_tuple=True)
    pboxs = pred['boxes'][pidxs]
    tboxs = tgt['boxes']
    tls = tgt['labels']
    pls = pred['labels']
    tlset = {int(l) for l in tls}

    tl2num = defaultdict(lambda:0)
    for tl in tls: tl2num[int(tl)]+=1
    # print(f"Target Labels -> num of boxes {tl2num}")

    tlpls = []
    for tl,tb in zip(tls,tboxs):
        x1,y1,w1,h1 = float(tb[0]), float(tb[1]), float(tb[2]), float(tb[3])
        for pl,pb in zip(pls,pboxs):
            x2,y2,w2,h2 = float(pb[0]), float(pb[1]), float(pb[2]), float(pb[3])
            iou = iou_calc(x1,y1,w1,h1, x2,y2,w2,h2)
            # print(iou)
            if iou > ithr: tlpls.append((int(tl),int(pl)))

    tl2tpfp = defaultdict(lambda: (0,0))
    for tl in tlset:
        for tl, pl in tlpls:
            tp, fp = tl2tpfp[tl]
            if tl == pl:
                tp+=1
            else:
                fp+=1
            tl2tpfp[tl] = (tp, fp)
    # print(f"Target Labels -> (True+, False+): {tl2tpfp}")

    tl2f1 = {}
    for tl in tlset:
        tp, fp = tl2tpfp[tl]
        tlnum = tl2num[tl]
        precision = 0 if tp == 0 else tp/(tp+fp)
        recall = 0 if tp == 0 else tp/tlnum
        f1 = 0 if precision*recall == 0 else 2/(1/precision + 1/recall)
        tl2f1[tl] = f1

    acc = 0.
    tnum = len(tgt['boxes'])
    for tl, f1 in tl2f1.items():
        tlnum = tl2num[tl]
        f1 = tl2f1[tl]
        acc += f1*(tlnum/tnum)

    return acc

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
            accu[i] = accuracy_1img(p, t, .3, .3)
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
def digest_pred(l2name, pred, cutoff=0.5):
    scores = pred['scores']
    pass_idxs = (scores > cutoff).nonzero(as_tuple=False)
    lbls = pred['labels'][pass_idxs]
    bboxs = pred['boxes'][pass_idxs]
    l2bs = defaultdict(lambda: [])
    for l, b in zip(lbls, bboxs):
        x,y,w,h = b[0]
        n = l2name[l.item()]
        bs = l2bs[l.item()]
        bs.append((x.item(),y.item(),w.item(),h.item()))
    return l2bs