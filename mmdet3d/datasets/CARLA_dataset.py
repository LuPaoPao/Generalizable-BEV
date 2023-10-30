# Copyright (c) OpenMMLab. All rights reserved.
import os
import tempfile
from os import path as osp
import math
from typing import Callable
from scipy.spatial.transform import Rotation as R

import mmcv
import numpy as np
import pandas as pd
from lyft_dataset_sdk.lyftdataset import LyftDataset as Lyft
from lyft_dataset_sdk.utils.data_classes import Box as LyftBox
from pyquaternion import Quaternion
from mmcv.utils import print_log
from nuscenes.utils.geometry_utils import view_points
from pyquaternion import Quaternion
from nuscenes import NuScenes
from mmcv.utils import print_log
from terminaltables import AsciiTable
import mmcv
import numpy as np
import pyquaternion
from nuscenes.utils.data_classes import Box as NuScenesBox
from nuscenes import NuScenes
from nuscenes.eval.detection.evaluate import NuScenesEval
from lyft_dataset_sdk.lyftdataset import LyftDataset as Lyft
from lyft_dataset_sdk.utils.data_classes import Box as LyftBox
from nuscenes.eval.common.config import config_factory
from nuscenes.eval.common.data_classes import EvalBox
from nuscenes.utils.data_classes import Box
from nuscenes.eval.common.data_classes import EvalBoxes
from nuscenes.eval.common.utils import center_distance, scale_iou, yaw_diff, velocity_l2, attr_acc, cummean
from nuscenes.eval.detection.data_classes import DetectionMetricData
from nuscenes import NuScenes
from nuscenes.eval.common.loaders import load_prediction, load_gt, add_center_dist, filter_eval_boxes
from nuscenes.eval.detection.algo import calc_ap, calc_tp
from nuscenes.eval.detection.data_classes import DetectionConfig, DetectionMetrics, DetectionBox, \
    DetectionMetricDataList
from nuscenes.eval.detection.render import summary_plot, class_pr_curve, class_tp_curve, dist_pr_curve, visualize_sample
from lyft_dataset_sdk.eval.detection.mAP_evaluation import (Box3D, get_ap,
                                                            get_class_names,
                                                            get_ious,
                                                            group_by_key,
                                                            wrap_in_box)
from mmdet3d.core.evaluation.lyft_eval import lyft_eval
from ..core import show_result
from ..core.bbox import Box3DMode, Coord3DMode, LiDARInstance3DBoxes
from .builder import DATASETS
from .custom_3d import Custom3DDataset
from .pipelines import Compose
from .lyft_dataset import LyftDataset, LyftDataset_my


def pitch2matrix_shift_box(pitch):
    R_y = np.array([[np.cos(pitch), 0, np.sin(pitch)],
                    [0, 1, 0],
                    [-np.sin(pitch), 0, np.cos(pitch)]])
    return R_y

def xyz2Quaternion(xyz, euler="xyz"):
    rot = np.array(xyz)
    rot_matrix = R.from_euler(euler, rot, degrees=False).as_matrix()
    return Quaternion(matrix=rot_matrix)


def output_to_lyft_box(detection):
    """Convert the output to the box class in the Lyft.

    Args:
        detection (dict): Detection results.

    Returns:
        list[:obj:`LyftBox`]: List of standard LyftBoxes.
    """
    box3d = detection['boxes_3d']
    scores = detection['scores_3d'].numpy()
    labels = detection['labels_3d'].numpy()

    box_gravity_center = box3d.gravity_center.numpy()
    box_dims = box3d.dims.numpy()
    box_yaw = box3d.yaw.numpy()

    # our LiDAR coordinate system -> Lyft box coordinate system
    lyft_box_dims = box_dims[:, [1, 0, 2]]

    box_list = []
    for i in range(len(box3d)):
        quat = Quaternion(axis=[0, 0, 1], radians=box_yaw[i])
        box = LyftBox(
            box_gravity_center[i],
            lyft_box_dims[i],
            quat,
            label=labels[i],
            score=scores[i])
        box_list.append(box)
    return box_list


def accumulate(gt_boxes: EvalBoxes,
               pred_boxes: EvalBoxes,
               class_name: str,
               dist_fcn: Callable,
               dist_th: float,
               verbose: bool = False) -> DetectionMetricData:
    """
    Average Precision over predefined different recall thresholds for a single distance threshold.
    The recall/conf thresholds and other raw metrics will be used in secondary metrics.
    :param gt_boxes: Maps every sample_token to a list of its sample_annotations.
    :param pred_boxes: Maps every sample_token to a list of its sample_results.
    :param class_name: Class to compute AP on.
    :param dist_fcn: Distance function used to match detections and ground truths.
    :param dist_th: Distance threshold for a match.
    :param verbose: If true, print debug messages.
    :return: (average_prec, metrics). The average precision value and raw data for a number of metrics.
    """
    # ---------------------------------------------
    # Organize input and initialize accumulators.
    # ---------------------------------------------
    # Count the positives.
    # gt_boxes=gts
    # pred_boxes=pre
    # dist_fcn=center_distance
    # dist_th=1
    # class_name='car'
    npos = len(gt_boxes)
    # print(npos)
    # print('---------------------------------------------')
    # print('---------------------------------------------')
    # print(gt_boxes[1])
    image_gts = group_by_key(gt_boxes, 'sample_token')
    image_gts = wrap_in_box(image_gts)
    # For missing classes in the GT, return a data structure corresponding to no predictions.
    if npos == 0:
        return DetectionMetricData.no_predictions()
    # Organize the predictions in a single list.
    pred_boxes_list = pred_boxes
    pred_confs = [box['score'] for box in pred_boxes_list]
    # Sort by confidence.
    sortind = [i for (v, i) in sorted((v, i) for (i, v) in enumerate(pred_confs))][::-1]
    # Do the actual matching.
    tp = []  # Accumulator of true positives.
    fp = []  # Accumulator of false positives.
    conf = []  # Accumulator of confidences.
    # match_data holds the extra metrics we calculate for each match.
    match_data = {'trans_err': [],  #  'vel_err': [],
                  'scale_err': [],
                  'orient_err': [],
                  'attr_err': [],
                  'conf': []}
    match_data_cumsum = {'trans_err': [],  # 'vel_err': [],
                  'scale_err': [],
                  'orient_err': [],
                  'attr_err': [],
                  'conf': []}
    # ---------------------------------------------
    # Match and accumulate match data.
    # ---------------------------------------------
    taken = set()  # Initially no gt bounding box is matched.
    for ind in sortind:
        pred_box = pred_boxes_list[ind]
        pred_box = Box3D(**pred_box)
        min_dist = np.inf
        match_gt_idx = None
        for gt_idx, gt_box in enumerate(image_gts[pred_box.sample_token]):
            # Find closest match among ground truth boxes
            if gt_box.name == class_name and not (pred_box.sample_token, gt_idx) in taken:
                this_distance = dist_fcn(gt_box, pred_box)
                if this_distance < min_dist:
                    min_dist = this_distance
                    match_gt_idx = gt_idx
        # If the closest match is close enough according to threshold we have a match!
        is_match = min_dist < dist_th
        if is_match:
            taken.add((pred_box.sample_token, match_gt_idx))
            #  Update tp, fp and confs.
            tp.append(1)
            fp.append(0)
            conf.append(pred_box.score)
            # Since it is a match, update match data also.
            gt_box_match = image_gts[pred_box.sample_token][match_gt_idx]
            gt_box_match.attribute_name = gt_box_match.name
            pred_box.attribute_name = pred_box.name
            match_data['trans_err'].append(center_distance(gt_box_match, pred_box))
            # match_data['vel_err'].append(velocity_l2(gt_box_match, pred_box))
            match_data['scale_err'].append(1 - scale_iou(gt_box_match, pred_box))
            # Barrier orientation is only determined up to 180 degree. (For cones orientation is discarded later)
            period = np.pi if class_name == 'barrier' else 2 * np.pi
            match_data['orient_err'].append(yaw_diff(gt_box_match, pred_box, period=period))
            match_data['attr_err'].append(1 - attr_acc(gt_box_match, pred_box))
            match_data['conf'].append(pred_box.score)
        else:
            # No match. Mark this as a false positive.
            tp.append(0)
            fp.append(1)
            conf.append(pred_box.score)
    # Check if we have any matches. If not, just return a "no predictions" array.
    if len(match_data['trans_err']) == 0:
        return DetectionMetricData.no_predictions()
    # ---------------------------------------------
    # Calculate and interpolate precision and recall
    # ---------------------------------------------
    # Accumulate.
    tp = np.cumsum(tp).astype(float)
    fp = np.cumsum(fp).astype(float)
    conf = np.array(conf)
    # Calculate precision and recall.
    prec = tp / (fp + tp)
    rec = tp / float(npos)
    rec_interp = np.linspace(0, 1, DetectionMetricData.nelem)  # 101 steps, from 0% to 100% recall.
    prec = np.interp(rec_interp, rec, prec, right=0)
    conf = np.interp(rec_interp, rec, conf, right=0)
    rec = rec_interp
    # ---------------------------------------------
    # Re-sample the match-data to match, prec, recall and conf.
    # ---------------------------------------------
    for key in match_data.keys():
        if key == "conf":
            continue  # Confidence is used as reference to align with fp and tp. So skip in this step.
        else:
            # For each match_data, we first calculate the accumulated mean.
            tmp = cummean(np.array(match_data[key]))
            # Then interpolate based on the confidences. (Note reversing since np.interp needs increasing arrays)
            match_data_cumsum[key] = np.interp(conf[::-1], match_data['conf'][::-1], tmp[::-1])[::-1]
            # ---------------------------------------------
            # Done. Instantiate MetricData and return
            # ---------------------------------------------
    return DetectionMetricData(recall=rec,
                               precision=prec,
                               confidence=conf,
                               trans_err=match_data_cumsum['trans_err'],
                               vel_err=match_data_cumsum['trans_err'],
                               scale_err=match_data_cumsum['scale_err'],
                               orient_err=match_data_cumsum['orient_err'],
                               attr_err=match_data_cumsum['attr_err'])


@DATASETS.register_module(force=True)
class CarlaDataset(LyftDataset_my):
    r"""Shift Dataset.
    This class serves as the API for experiments on the Shift Dataset.
    """
    NameMapping = {
        'bicycle': 'bicycle',
        'bus': 'bus',
        'car': 'car',
        'motorcycle': 'motorcycle',
        'pedestrian': 'pedestrian',
        'truck': 'truck'
    }

    DefaultAttribute = {
        'car': 'is_stationary',
        'truck': 'is_stationary',
        'bus': 'is_stationary',
        'motorcycle': 'is_stationary',
        'bicycle': 'is_stationary',
        'pedestrian': 'is_stationary'
    }
    CLASSES = ('car', 'truck', 'bus',
               'motorcycle', 'bicycle', 'pedestrian')

    def get_data_info(self, index):
        """Get data info according to the given index.

        Args:
            index (int): Index of the sample data to get.

        Returns:
            dict: Data information that will be passed to the data
                preprocessing pipelines. It includes the following keys:

                - sample_idx (str): sample index
                - pts_filename (str): filename of point clouds
                - sweeps (list[dict]): infos of sweeps
                - timestamp (float): sample timestamp
                - img_filename (str, optional): image filename
                - lidar2img (list[np.ndarray], optional): transformations
                    from lidar to different cameras
                - ann_info (dict): annotation info
        """
        info = self.data_infos[index]

        # standard protocol modified from SECOND.Pytorch
        input_dict = dict(
            sample_idx=info['token'],
            pts_filename=info['token'],  # !!!!!!!!!!!!!!!!!!!!!!
            sweeps=info['cam_sweeps'],
            timestamp=info['timestamp'],
        )

        if self.modality['use_camera']:
            image_paths = []
            lidar2img_rts = []
            input_dict.update(dict(curr=info))
            for cam_type, cam_info in info['cams'].items():
                image_paths.append(cam_info['data_path'])
                # obtain lidar to image transformation matrix
                location = cam_info['calibrated_sensor']['translation']
                rotation = pitch2matrix_shift_box(math.radians(cam_info['calibrated_sensor']['rotation']))
                lidar2cam_r = np.linalg.inv(rotation)  #  !!!!!!!!!!!!!!
                lidar2cam_t = [location[1], location[2], location[0]] @ lidar2cam_r.T # !!!!!!!!!!!!!!!!!!!!!
                lidar2cam_rt = np.eye(4)
                lidar2cam_rt[:3, :3] = lidar2cam_r.T
                lidar2cam_rt[3, :3] = -lidar2cam_t
                intrinsic = cam_info['cam_intrinsic']
                intrinsic = np.array(intrinsic)
                viewpad = np.eye(4)
                viewpad[:intrinsic.shape[0], :intrinsic.shape[1]] = intrinsic
                lidar2img_rt = (viewpad @ lidar2cam_rt.T)
                lidar2img_rts.append(lidar2img_rt)

            input_dict.update(
                dict(
                    img_filename=image_paths,
                    lidar2img=lidar2img_rts,
                ))
        #
        # if not self.test_mode:
        #     annos = self.get_ann_info(index)
        #     input_dict['ann_infos'] = annos
        # input_dict['ann_infos'] = get_gt(info)
        if 'ann_infos' in info:
            input_dict['ann_infos'] = info['ann_infos']
            # print('*(*((*(*(*(*(*(*(((')
        # print(input_dict.keys())
        # dict_keys(['sample_idx', 'pts_filename', 'sweeps', 'timestamp', 'img_filename', 'lidar2img', 'ann_infos'])
        # dict_keys(['gt_bboxes_3d', 'gt_labels_3d'])
        return input_dict

    def load_annotations(self, ann_file):
        """Load annotations from ann_file.

        Args:
            ann_file (str): Path of the annotation file.

        Returns:
            list[dict]: List of annotations sorted by timestamps.
        """
        # loading data from a file-like object needs file format
        data = mmcv.load(ann_file, file_format='pkl')
        data_infos = list(sorted(data['infos'], key=lambda e: e['timestamp']))
        data_infos = data_infos[::self.load_interval]
        self.metadata = dict(image_size=[1280, 800],
                             categories=[ 'car', 'truck', 'bus', 'motorcycle', 'bicycle', 'pedestrian'])
        self.version = 'v1'
        return data_infos

    def get_cat_ids(self, idx):
        """Get category distribution of single scene.

        Args:
            idx (int): Index of the data_info.

        Returns:
            dict[list]: for each category, if the current scene
                contains such boxes, store a list containing idx,
                otherwise, store empty list.
        """
        info = self.data_infos[idx]
        gt_indexs = info['ann_infos'][1]

        gt_names = list()
        classes = [
            'car', 'truck', 'bus',
            'motorcycle', 'bicycle', 'pedestrian'
        ]
        for gt_index in gt_indexs:
            gt_names.append(classes[gt_index])
        cat_ids = []
        for name in gt_names:
            if name in self.CLASSES:
                cat_ids.append(self.cat2id[name])
        return cat_ids

    def get_ann_info(self, index):
        """Get annotation info according to the given index.

        Args:
            index (int): Index of the annotation data to get.

        Returns:
            dict: Annotation information consists of the following keys:

                - gt_bboxes_3d (:obj:`LiDARInstance3DBoxes`):
                    3D ground truth bboxes.
                - gt_labels_3d (np.ndarray): Labels of ground truths.
                - gt_names (list[str]): Class names of ground truths.
        """
        info = self.data_infos[index]
        gt_bboxes_3d = info['gt_boxes']
        gt_names_3d = info['gt_names']
        gt_labels_3d = []
        for cat in gt_names_3d:
            if cat in self.CLASSES:
                gt_labels_3d.append(self.CLASSES.index(cat))
            else:
                gt_labels_3d.append(-1)
        gt_labels_3d = np.array(gt_labels_3d)

        if 'gt_shape' in info:
            gt_shape = info['gt_shape']
            gt_bboxes_3d = np.concatenate([gt_bboxes_3d, gt_shape], axis=-1)

        # the lyft box center is [0.5, 0.5, 0.5], we change it to be
        # the same as KITTI (0.5, 0.5, 0)
        gt_bboxes_3d = LiDARInstance3DBoxes(
            gt_bboxes_3d,
            box_dim=gt_bboxes_3d.shape[-1],
            origin=(0.5, 0.5, 0.5)).convert_to(self.box_mode_3d)

        anns_results = dict(
            gt_bboxes_3d=gt_bboxes_3d,
            gt_labels_3d=gt_labels_3d,
        )
        return anns_results

    def _format_bbox(self, results, jsonfile_prefix=None):
        """Convert the results to the standard format.

        Args:
            results (list[dict]): Testing results of the dataset.
            jsonfile_prefix (str): The prefix of the output jsonfile.
                You can specify the output directory/filename by
                modifying the jsonfile_prefix. Default: None.

        Returns:
            str: Path of the output json file.
        """
        lyft_annos = {}
        mapped_class_names = self.CLASSES
        pre_token = mmcv.load('/mnt/cfs/algorithm/hao.lu/Code/BEVDepth_pg/data/SHIFT/val_oder.pkl')
        print('Start to convert detection format...')
        for sample_id, det in enumerate(mmcv.track_iter_progress(results)):
            annos = []
            boxes = output_to_lyft_box(det)
            # sample_token = self.data_infos[sample_id]['token']
            # boxes = lidar_lyft_box_to_global(self.data_infos[sample_id], boxes)
            sample_token = pre_token[sample_id]['sample_idx']
            data_index_from_pre = self.find_index(self.data_infos, sample_token)
            boxes = lidar_lyft_box_to_global(self.data_infos[data_index_from_pre], boxes)
            for i, box in enumerate(boxes):
                name = mapped_class_names[box.label]
                lyft_anno = dict(
                    sample_token=sample_token,
                    translation=box.center.tolist(),
                    size=box.wlh[[1, 0, 2]].tolist(),
                    rotation=box.orientation.elements.tolist(),
                    name=name,
                    score=box.score)
                annos.append(lyft_anno)
            lyft_annos[sample_token] = annos
        lyft_submissions = {
            'meta': self.modality,
            'results': lyft_annos,
        }
        # mmcv.dump(lyft_submissions, '/mnt/cfs/algorithm/hao.lu/Code/BEVDet/scripts/lyft_submissions.pkl')
        mmcv.mkdir_or_exist(jsonfile_prefix)
        res_path = osp.join(jsonfile_prefix, 'results_lyft.json')
        print('Results writes to', res_path)
        mmcv.dump(lyft_submissions, res_path)
        return res_path

    def find_index(self, data_infos, sample_token):
        for index, sample in enumerate(data_infos):
            if sample['token'] == sample_token:
                return index

    def _evaluate_single(self,
                         result_path,
                         logger=None,
                         metric='bbox',
                         result_name='pts_bbox'):
        """Evaluation for a single model in Lyft protocol.

        Args:
            result_path (str): Path of the result file.
            logger (logging.Logger | str, optional): Logger used for printing
                related information during evaluation. Default: None.
            metric (str, optional): Metric name used for evaluation.
                Default: 'bbox'.
            result_name (str, optional): Result name in the metric prefix.
                Default: 'pts_bbox'.

        Returns:
            dict: Dictionary of evaluation details.
        """

        output_dir = osp.join(*osp.split(result_path)[:-1])
        # lyft = Lyft(
        #     data_path=osp.join(self.data_root, self.version),
        #     json_path=osp.join(self.data_root, self.version, self.version),
        #     verbose=True)
        # print('self.version')
        # print(self.version)
        # eval_set_map = {
        #     'v1.01-train': 'val',
        # }
        metrics = self.shift_eval(result_path, output_dir, logger)

        # record metrics
        detail = dict()
        metric_prefix = f'{result_name}_Lyft'

        for i, name in enumerate(metrics['class_names']):
            AP = float(metrics['mAPs_cate'][i])
            detail[f'{metric_prefix}/{name}_AP'] = AP

        detail[f'{metric_prefix}/mAP'] = metrics['Final mAP']
        return detail

    def load_predictions(self, res_path):
        """Load Lyft predictions from json file.
        Args:
            res_path (str): Path of result json file recording detections.

        Returns:
            list[dict]: List of prediction dictionaries.
        """
        predictions = mmcv.load(res_path)
        predictions = predictions['results']
        all_preds = []
        for sample_token in predictions.keys():
            all_preds.extend(predictions[sample_token])
        return all_preds

    def load_gts(self):
        """Load Lyft predictions from json file.
        Args:
            res_path (str): Path of result json file recording detections.

        Returns:
            list[dict]: List of prediction dictionaries.
        """
        all_annotations = []
        # Load annotations and filter predictions and annotations.
        for data_info in mmcv.track_iter_progress(self.data_infos):
            sample_annotations = data_info['ann_infos_v1']
            for sample_annotation in sample_annotations:
                # Get label name in detection task and filter unused labels.
                xyzw = xyz2Quaternion(sample_annotation['orientation'], euler='xyz')
                annotation = {
                    'sample_token': sample_annotation['sample_token'],
                    'translation': sample_annotation['translation'],
                    'size': sample_annotation['size'],
                    'rotation': [xyzw.x, xyzw.y, xyzw.z, xyzw.w],
                    'name': sample_annotation['category'],
                }
                all_annotations.append(annotation)

        return all_annotations

    def shift_eval(self, res_path, output_dir, logger=None, NDS=True):
        """Evaluation API for Lyft dataset.

        Args:
            lyft (:obj:`LyftDataset`): Lyft class in the sdk.
            data_root (str): Root of data for reading splits.
            res_path (str): Path of result json file recording detections.
            eval_set (str): Name of the split for evaluation.
            output_dir (str): Output directory for output json files.
            logger (logging.Logger | str, optional): Logger used for printing
                    related information during evaluation. Default: None.

        Returns:
            dict[str, float]: The evaluation results.
        """
        # evaluate by lyft metrics
        gts = self.load_gts()
        predictions = self.load_predictions(res_path)
        mmcv.dump(gts, osp.join(output_dir, 'gts.pkl'))
        mmcv.dump(predictions, osp.join(output_dir, 'pre.pkl'))
        class_name = 'car'
        gts = group_by_key(gts, 'name')
        pre = group_by_key(predictions, 'name')
        dist_th_list = [0.5, 1, 2, 4]
        gts = gts['car']
        pre = pre['car']
        metric_data_list = DetectionMetricDataList()
        for dist_th in dist_th_list:
            md = accumulate(gts, pre, class_name, center_distance, dist_th)
            metric_data_list.set(class_name, dist_th, md)
        TP_METRICS = ['trans_err', 'scale_err', 'orient_err']
        eval_version = 'detection_cvpr_2019'
        eval_detection_configs = config_factory(eval_version)
        metrics = DetectionMetrics(eval_detection_configs)

        # Compute APs.
        for dist_th in dist_th_list:
            metric_data = metric_data_list[(class_name, dist_th)]
            ap = calc_ap(metric_data, eval_detection_configs.min_recall, eval_detection_configs.min_precision)
            metrics.add_label_ap(class_name, dist_th, ap)

        # Compute TP metrics.
        for metric_name in TP_METRICS:
            metric_data = metric_data_list[(class_name, eval_detection_configs.dist_th_tp)]
            if class_name in ['traffic_cone'] and metric_name in ['attr_err', 'vel_err', 'orient_err']:
                tp = np.nan
            elif class_name in ['barrier'] and metric_name in ['attr_err', 'vel_err']:
                tp = np.nan
            else:
                tp = calc_tp(metric_data, eval_detection_configs.min_recall, metric_name)
            metrics.add_label_tp(class_name, metric_name, tp)

        metrics_summary = metrics.serialize()
        # with open(os.path.join('/mnt/cfs/algorithm/hao.lu/Code/BEVDet/scripts/Eval', 'metrics_summary.json'), 'w') as f:
        #     json.dump(metrics_summary, f, indent=2)
        # with open(os.path.join('/mnt/cfs/algorithm/hao.lu/Code/BEVDet/scripts/Eval', 'metrics_details.json'), 'w') as f:
        #     json.dump(metric_data_list.serialize(), f, indent=2)

        # Print high-level metrics.
        print('mAP: %.4f' % (metrics_summary['mean_ap']))
        err_name_mapping = {
            'trans_err': 'mATE',
            'scale_err': 'mASE',
            'orient_err': 'mAOE',
            'vel_err': 'mAVE',
            'attr_err': 'mAAE'
        }
        for tp_name, tp_val in metrics_summary['tp_errors'].items():
            print('%s: %.4f' % (err_name_mapping[tp_name], tp_val))
        print('NDS: %.4f' % (metrics_summary['nd_score']))
        # Print per-class metrics.
        print()
        print('Per-class results:')
        print('Object Class\tAP\tATE\tASE\tAOE')
        class_aps = metrics_summary['mean_dist_aps']
        class_tps = metrics_summary['label_tp_errors']
        for class_name in class_aps.keys():
            print('%s\t%.3f\t%.3f\t%.3f\t%.3f'
                  % (class_name, class_aps[class_name],
                     class_tps[class_name]['trans_err'],
                     class_tps[class_name]['scale_err'],
                     class_tps[class_name]['orient_err']))
        return metrics_summary


    def format_results(self, results, jsonfile_prefix=None, csv_savepath=None):
        """Format the results to json (standard format for COCO evaluation).

        Args:
            results (list[dict]): Testing results of the dataset.
            jsonfile_prefix (str): The prefix of json files. It includes
                the file path and the prefix of filename, e.g., "a/b/prefix".
                If not specified, a temp file will be created. Default: None.
            csv_savepath (str): The path for saving csv files.
                It includes the file path and the csv filename,
                e.g., "a/b/filename.csv". If not specified,
                the result will not be converted to csv file.

        Returns:
            tuple: Returns (result_files, tmp_dir), where `result_files` is a
                dict containing the json filepaths, `tmp_dir` is the temporal
                directory created for saving json files when
                `jsonfile_prefix` is not specified.
        """
        assert isinstance(results, list), 'results must be a list'
        assert len(results) == len(self), (
            'The length of results is not equal to the dataset len: {} != {}'.
            format(len(results), len(self)))
        print(len(results))
        mmcv.dump(results, '/mnt/cfs/algorithm/hao.lu/Code/BEVDepth_pg/work_dirs/bevdet-r50-cbgs-shift2X-car/resualt.pkl')
        # 3780
        if jsonfile_prefix is None:
            tmp_dir = tempfile.TemporaryDirectory()
            jsonfile_prefix = osp.join(tmp_dir.name, 'results')
        else:
            tmp_dir = None

        # currently the output prediction results could be in two formats
        # 1. list of dict('boxes_3d': ..., 'scores_3d': ..., 'labels_3d': ...)
        # 2. list of dict('pts_bbox' or 'img_bbox':
        #     dict('boxes_3d': ..., 'scores_3d': ..., 'labels_3d': ...))
        # this is a workaround to enable evaluation of both formats on Lyft
        # refer to https://github.com/open-mmlab/mmdetection3d/issues/449
        if not ('pts_bbox' in results[0] or 'img_bbox' in results[0]):
            # here is right!
            result_files = self._format_bbox(results, jsonfile_prefix)
        else:
            # should take the inner dict out of 'pts_bbox' or 'img_bbox' dict
            result_files = dict()
            for name in results[0]:
                print(f'\nFormating bboxes of {name}')
                results_ = [out[name] for out in results]
                tmp_file_ = osp.join(jsonfile_prefix, name)
                result_files.update(
                    {name: self._format_bbox(results_, tmp_file_)})
        if csv_savepath is not None:
            self.json2csv(result_files['pts_bbox'], csv_savepath)
        return result_files, tmp_dir

    def evaluate(self,
                 results,
                 metric='bbox',
                 logger=None,
                 jsonfile_prefix=None,
                 csv_savepath=None,
                 result_names=['pts_bbox'],
                 show=False,
                 out_dir=None,
                 pipeline=None):
        """Evaluation in Lyft protocol.

        Args:
            results (list[dict]): Testing results of the dataset.
            metric (str | list[str], optional): Metrics to be evaluated.
                Default: 'bbox'.
            logger (logging.Logger | str, optional): Logger used for printing
                related information during evaluation. Default: None.
            jsonfile_prefix (str, optional): The prefix of json files including
                the file path and the prefix of filename, e.g., "a/b/prefix".
                If not specified, a temp file will be created. Default: None.
            csv_savepath (str, optional): The path for saving csv files.
                It includes the file path and the csv filename,
                e.g., "a/b/filename.csv". If not specified,
                the result will not be converted to csv file.
            result_names (list[str], optional): Result names in the
                metric prefix. Default: ['pts_bbox'].
            show (bool, optional): Whether to visualize.
                Default: False.
            out_dir (str, optional): Path to save the visualization results.
                Default: None.
            pipeline (list[dict], optional): raw data loading for showing.
                Default: None.

        Returns:
            dict[str, float]: Evaluation results.
        """
        result_files, tmp_dir = self.format_results(results, jsonfile_prefix,
                                                    csv_savepath)
        if isinstance(result_files, dict):
            results_dict = dict()
            for name in result_names:
                print(f'Evaluating bboxes of {name}')
                ret_dict = self._evaluate_single(result_files[name])
            results_dict.update(ret_dict)
        elif isinstance(result_files, str):
            results_dict = self._evaluate_single(result_files)

        if tmp_dir is not None:
            tmp_dir.cleanup()

        if show or out_dir:
            self.show(results, out_dir, show=show, pipeline=pipeline)
        return results_dict

    def _build_default_pipeline(self):
        """Build the default pipeline for this dataset."""
        pipeline = [
            dict(
                type='LoadPointsFromFile',
                coord_type='LIDAR',
                load_dim=5,
                use_dim=5,
                file_client_args=dict(backend='disk')),
            dict(
                type='LoadPointsFromMultiSweeps',
                sweeps_num=10,
                file_client_args=dict(backend='disk')),
            dict(
                type='DefaultFormatBundle3D',
                class_names=self.CLASSES,
                with_label=False),
            dict(type='Collect3D', keys=['points'])
        ]
        return Compose(pipeline)

    def show(self, results, out_dir, show=False, pipeline=None):
        """Results visualization.

        Args:
            results (list[dict]): List of bounding boxes results.
            out_dir (str): Output directory of visualization result.
            show (bool): Whether to visualize the results online.
                Default: False.
            pipeline (list[dict], optional): raw data loading for showing.
                Default: None.
        """
        assert out_dir is not None, 'Expect out_dir, got none.'
        pipeline = self._get_pipeline(pipeline)
        for i, result in enumerate(results):
            if 'pts_bbox' in result.keys():
                result = result['pts_bbox']
            data_info = self.data_infos[i]
            pts_path = data_info['lidar_path']
            file_name = osp.split(pts_path)[-1].split('.')[0]
            points = self._extract_data(i, pipeline, 'points').numpy()
            points = Coord3DMode.convert_point(points, Coord3DMode.LIDAR,
                                               Coord3DMode.DEPTH)
            inds = result['scores_3d'] > 0.1
            gt_bboxes = self.get_ann_info(i)['gt_bboxes_3d'].tensor.numpy()
            show_gt_bboxes = Box3DMode.convert(gt_bboxes, Box3DMode.LIDAR,
                                               Box3DMode.DEPTH)
            pred_bboxes = result['boxes_3d'][inds].tensor.numpy()
            show_pred_bboxes = Box3DMode.convert(pred_bboxes, Box3DMode.LIDAR,
                                                 Box3DMode.DEPTH)
            show_result(points, show_gt_bboxes, show_pred_bboxes, out_dir,
                        file_name, show)

    def json2csv(self, json_path, csv_savepath):
        """Convert the json file to csv format for submission.

        Args:
            json_path (str): Path of the result json file.
            csv_savepath (str): Path to save the csv file.
        """
        results = mmcv.load(json_path)['results']
        sample_list_path = osp.join(self.data_root, 'sample_submission.csv')
        data = pd.read_csv(sample_list_path)
        Id_list = list(data['Id'])
        pred_list = list(data['PredictionString'])
        cnt = 0
        print('Converting the json to csv...')
        for token in results.keys():
            cnt += 1
            predictions = results[token]
            prediction_str = ''
            for i in range(len(predictions)):
                prediction_str += \
                    str(predictions[i]['score']) + ' ' + \
                    str(predictions[i]['translation'][0]) + ' ' + \
                    str(predictions[i]['translation'][1]) + ' ' + \
                    str(predictions[i]['translation'][2]) + ' ' + \
                    str(predictions[i]['size'][0]) + ' ' + \
                    str(predictions[i]['size'][1]) + ' ' + \
                    str(predictions[i]['size'][2]) + ' ' + \
                    str(Quaternion(list(predictions[i]['rotation']))
                        .yaw_pitch_roll[0]) + ' ' + \
                    predictions[i]['name'] + ' '
            prediction_str = prediction_str[:-1]
            idx = Id_list.index(token)
            pred_list[idx] = prediction_str
        df = pd.DataFrame({'Id': Id_list, 'PredictionString': pred_list})
        mmcv.mkdir_or_exist(os.path.dirname(csv_savepath))
        df.to_csv(csv_savepath, index=False)


def output_to_lyft_box(detection):
    """Convert the output to the box class in the Lyft.

    Args:
        detection (dict): Detection results.

    Returns:
        list[:obj:`LyftBox`]: List of standard LyftBoxes.
    """
    box3d = detection['boxes_3d']
    scores = detection['scores_3d'].numpy()
    labels = detection['labels_3d'].numpy()

    box_gravity_center = box3d.gravity_center.numpy()
    box_dims = box3d.dims.numpy()
    box_yaw = box3d.yaw.numpy()

    # our LiDAR coordinate system -> Lyft box coordinate system
    lyft_box_dims = box_dims[:, [1, 0, 2]]

    box_list = []
    for i in range(len(box3d)):
        quat = Quaternion(axis=[0, 0, 1], radians=box_yaw[i])
        box = LyftBox(
            box_gravity_center[i],
            lyft_box_dims[i],
            quat,
            label=labels[i],
            score=scores[i])
        box_list.append(box)
    return box_list


def lidar_lyft_box_to_global(info, boxes):
    """Convert the box from ego to global coordinate.

    Args:
        info (dict): Info for a specific sample data, including the
            calibration information.
        boxes (list[:obj:`LyftBox`]): List of predicted LyftBoxes.

    Returns:
        list: List of standard LyftBoxes in the global
            coordinate.
    """
    box_list = []
    for box in boxes:
        # # Move box to ego vehicle coord system
        # box.rotate(Quaternion(info['lidar2ego_rotation']))
        # box.translate(np.array(info['lidar2ego_translation']))
        # Move box to global coord system
        box.rotate(Quaternion(info['ego2global_rotation']))
        box.translate(np.array(info['ego2global_translation']))
        box_list.append(box)
    return box_list