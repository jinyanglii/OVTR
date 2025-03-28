# Copyright (c) Jinyang Li. All Rights Reserved.
# ------------------------------------------------------------------------
"""
LVIS dataset which returns image_id for evaluation.

Mostly copy-paste from https://github.com/pytorch/vision/blob/13b35ff/references/detection/coco_utils.py
"""
#from mmdet

from pathlib import Path

import torch
import torch.utils.data
from pycocotools import mask as coco_mask

import datasets.transforms as T

from .torchvision_datasets import LvisDetection as TvLvisDetection
import numpy as np
import math
from collections import defaultdict
from operator import itemgetter
from util.list_LVIS import CLASSES


class LvisDetection(TvLvisDetection):
    def __init__(self, img_folder, ann_file, transforms, return_masks, label_map):
        super(LvisDetection, self).__init__(img_folder, ann_file)
        self.CLASSES = CLASSES
        self.remove_rare_cat()
        self.get_repeat_factors()
        self._transforms = transforms
        self.cat_ids = self.lvis.get_cat_ids()
        self.cat2label = {cat_id: i for i, cat_id in enumerate(self.cat_ids)}
        self.prepare = ConvertCocoPolysToMask(return_masks, self.cat2label, label_map)
        self.num_samples = len(self)

    def __getitem__(self, idx):
        img, target = super(LvisDetection, self).__getitem__(idx)
        image_id = self.ids[idx]
        target = {"image_id": image_id, "annotations": target}
        img, target = self.prepare(img, target)
        if self._transforms is not None:
            img, target = self._transforms(img, target)
        if len(target["labels"]) == 0:
            return self[(idx + 1) % len(self)]
        else:
            return img, target
    def remove_rare_cat(self):
        cats = {}
        self.ignore_ids = []
        self.img_ids = self.lvis.get_img_ids()
        for cat in self.lvis.cats:
            cat_info = self.lvis.cats[cat]
            if cat_info['frequency'] == 'r':
                self.ignore_ids.append(cat)
            else:
                cats[cat] = self.lvis.cats[cat]

        self.id_idx = {}
        for idx,img_id in enumerate(self.img_ids):
            self.id_idx[img_id] = idx
        rare_cls_img_ids = []
        cat_ids = []
        cnt = 0
        for cat in self.lvis.cats:
            cat_info = self.lvis.cats[cat]
            if cat_info['frequency'] == 'r':
                continue
            cat_ids.append(cat)
            cnt += 1
            rare_cls_img_ids.extend(self.lvis.cat_img_map[cat])
        self.img_ids = np.unique(rare_cls_img_ids).tolist()

    def get_cat_ids(self, idx):
        img_id = self.img_ids[idx]
        ann_info = self.lvis.img_ann_map[img_id]
        return [ann['category_id'] for ann in ann_info]

    def get_repeat_factors(self):
        """Get repeat factor for each images in the dataset.

        Args:
            dataset (:obj:`CustomDataset`): The dataset
            repeat_thr (float): The threshold of frequency. If an image
                contains the categories whose frequency below the threshold,
                it would be repeated.

        Returns:
            list[float]: The repeat factors for each images in the dataset.
        """
        # 1. For each category c, compute the fraction # of images
        #   that contain it: f(c)
        category_freq = defaultdict(int)
        num_images = len(self.img_ids)
        filter_empty_gt = True
        repeat_thr = 0.006
        for idx in range(num_images):
            cat_ids = set(self.get_cat_ids(idx))
            if len(cat_ids) == 0 and not filter_empty_gt:
                cat_ids = set([len(self.CLASSES)])
            for cat_id in cat_ids:
                category_freq[cat_id] += 1
        for k, v in category_freq.items():
            category_freq[k] = v / num_images

        # 2. For each category c, compute the category-level repeat factor:
        #    r(c) = max(1, sqrt(t/f(c)))
        category_repeat = {
            cat_id: max(1.0, math.sqrt(repeat_thr / cat_freq))
            for cat_id, cat_freq in category_freq.items()
        }

        # 3. For each image I, compute the image-level repeat factor:
        #    r(I) = max_{c in I} r(c)
        repeat_factors = []
        for idx in range(num_images):
            cat_ids = set(self.get_cat_ids(idx))
            if len(cat_ids) == 0 and not filter_empty_gt:
                cat_ids = set([len(self.CLASSES)])
            repeat_factor = 1
            if len(cat_ids) > 0:
                repeat_factor = max(
                    {category_repeat[cat_id]
                     for cat_id in cat_ids})
            repeat_factors.append(repeat_factor)

        repeat_indices = []
        for dataset_idx, repeat_factor in enumerate(repeat_factors):
            repeat_indices.extend([dataset_idx] * math.floor(repeat_factor))
        self.ids = list(itemgetter(*repeat_indices)(self.img_ids))

def convert_coco_poly_to_mask(segmentations, height, width):
    masks = []
    for polygons in segmentations:
        rles = coco_mask.frPyObjects(polygons, height, width)
        mask = coco_mask.decode(rles)
        if len(mask.shape) < 3:
            mask = mask[..., None]
        mask = torch.as_tensor(mask, dtype=torch.uint8)
        mask = mask.any(dim=2)
        masks.append(mask)
    if masks:
        masks = torch.stack(masks, dim=0)
    else:
        masks = torch.zeros((0, height, width), dtype=torch.uint8)
    return masks


class ConvertCocoPolysToMask(object):
    def __init__(self, return_masks=False, cat2label=None, label_map=False):
        self.return_masks = return_masks
        self.cat2label = cat2label
        self.label_map = label_map

    def __call__(self, image, target):
        w, h = image.size

        image_id = target["image_id"]
        image_id = torch.tensor([image_id])

        anno = target["annotations"]

        anno = [obj for obj in anno if "iscrowd" not in obj or obj["iscrowd"] == 0]

        boxes = [obj["bbox"] for obj in anno]
        # guard against no boxes via resizing
        boxes = torch.as_tensor(boxes, dtype=torch.float32).reshape(-1, 4)
        boxes[:, 2:] += boxes[:, :2]
        boxes[:, 0::2].clamp_(min=0, max=w)
        boxes[:, 1::2].clamp_(min=0, max=h)

        if self.label_map:
            classes = [self.cat2label[obj["category_id"]] for obj in anno]
        else:
            classes = [obj["category_id"] for obj in anno]
        classes = torch.tensor(classes, dtype=torch.int64)

        if self.return_masks:
            segmentations = [obj["segmentation"] for obj in anno]
            masks = convert_coco_poly_to_mask(segmentations, h, w)

        keypoints = None
        if anno and "keypoints" in anno[0]:
            keypoints = [obj["keypoints"] for obj in anno]
            keypoints = torch.as_tensor(keypoints, dtype=torch.float32)
            num_keypoints = keypoints.shape[0]
            if num_keypoints:
                keypoints = keypoints.view(num_keypoints, -1, 3)

        keep = (boxes[:, 3] > boxes[:, 1]) & (boxes[:, 2] > boxes[:, 0])
        boxes = boxes[keep]
        classes = classes[keep]
        if self.return_masks:
            masks = masks[keep]
        if keypoints is not None:
            keypoints = keypoints[keep]

        target = {}
        target["boxes"] = boxes
        target["labels"] = classes
        if self.return_masks:
            target["masks"] = masks
        target["image_id"] = image_id
        if keypoints is not None:
            target["keypoints"] = keypoints

        # for conversion to coco api
        area = torch.tensor([obj["area"] for obj in anno])
        iscrowd = torch.tensor([obj["iscrowd"] if "iscrowd" in obj else 0 for obj in anno])
        target["area"] = area[keep]
        target["iscrowd"] = iscrowd[keep]

        target["orig_size"] = torch.as_tensor([int(h), int(w)])
        target["size"] = torch.as_tensor([int(h), int(w)])

        return image, target


def make_coco_transforms(image_set):

    normalize = T.Compose([T.ToTensor(), T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])])

    scales = [480, 512, 544, 576, 608, 640, 672, 704, 736, 768, 800]

    if image_set == "train":
        return T.Compose(
            [
                T.RandomHorizontalFlip(),
                T.RandomSelect(
                    T.RandomResize(scales, max_size=1333),
                    T.Compose(
                        [
                            T.RandomResize([400, 500, 600]),
                            T.RandomSizeCrop(384, 600),
                            T.RandomResize(scales, max_size=1333),
                        ]
                    ),
                ),
                normalize,
            ]
        )

    if image_set == "val":
        return T.Compose(
            [
                T.RandomResize([800], max_size=1333),
                normalize,
            ]
        )

    raise ValueError(f"unknown {image_set}")


def build(image_set, args, cfg):
    root = Path(cfg.lvis_path)
    assert root.exists(), f"provided LVIS path {root} does not exist"
    PATHS = {
        "train": (root),
    }
    
    img_folder = PATHS[image_set]
    ann_file = cfg.lvis_anno
    dataset = LvisDetection(
        img_folder,
        ann_file,
        transforms=make_coco_transforms(image_set),
        return_masks=args.masks,
        label_map=args.label_map,
    )
    return dataset
