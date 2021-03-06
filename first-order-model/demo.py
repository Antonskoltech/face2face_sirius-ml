import matplotlib

matplotlib.use('Agg')
import os, sys
import yaml
from argparse import ArgumentParser
from tqdm import tqdm

import imageio
import numpy as np
import crop
from skimage.transform import resize
from skimage import img_as_ubyte
import torch
from sync_batchnorm import DataParallelWithCallback

from modules.generator import OcclusionAwareGenerator
from modules.keypoint_detector import KPDetector
from animate import normalize_kp
from scipy.spatial import ConvexHull
import cv2


if sys.version_info[0] < 3:
    raise Exception("You must use Python 3 or higher. Recommended version is Python 3.7")


def load_checkpoints(config_path, checkpoint_path, cpu=False):
    with open(config_path) as f:
        config = yaml.load(f)

    generator = OcclusionAwareGenerator(**config['model_params']['generator_params'],
                                        **config['model_params']['common_params'])
    if not cpu:
        generator.cuda()

    kp_detector = KPDetector(**config['model_params']['kp_detector_params'],
                             **config['model_params']['common_params'])
    if not cpu:
        kp_detector.cuda()

    if cpu:
        checkpoint = torch.load(checkpoint_path, map_location=torch.device('cpu'))
    else:
        checkpoint = torch.load(checkpoint_path)

    generator.load_state_dict(checkpoint['generator'])
    kp_detector.load_state_dict(checkpoint['kp_detector'])

    if not cpu:
        generator = DataParallelWithCallback(generator)
        kp_detector = DataParallelWithCallback(kp_detector)

    generator.eval()
    kp_detector.eval()

    return generator, kp_detector


def make_animation(source_images, driving_video, generator, kp_detector, relative=True, adapt_movement_scale=True,
                   cpu=False):
    with torch.no_grad():
        predictions = []
        source = [torch.tensor(s[np.newaxis].astype(np.float32)).permute(0, 3, 1, 2) for s in source_images]
        driving = torch.tensor(np.array(driving_video)[np.newaxis].astype(np.float32)).permute(0, 4, 1, 2, 3)
        if not cpu:
            source = [s.cuda() for s in source]
        kp_source = [kp_detector(s) for s in source]
        kp_source_value = [kp_s['value'][0].detach().cpu().numpy() for kp_s in kp_source]
        kp_driving_initial = kp_detector(driving[:, :, 0])

        distance = lambda y: lambda x: np.sum(np.sum((x - y) ** 2, axis=1) ** 0.5)

        kp_frame_value = kp_driving_initial['value'][0].detach().cpu().numpy()
        i_prev = np.argmin(list(map(distance(kp_frame_value), kp_source_value)))
        kp_source_prev, source_prev = kp_source[i_prev], source[i_prev]

        diff = 20
        alpha = 0
        n = len(source_images)

        for frame_idx in tqdm(range(driving.shape[2])):
            driving_frame = driving[:, :, frame_idx]
            if not cpu:
                driving_frame = driving_frame.cuda()
            kp_driving = kp_detector(driving_frame)
            kp_frame_value = kp_driving['value'][0].detach().cpu().numpy()

            i = np.argmin(list(map(distance(kp_frame_value), kp_source_value[max(0, i_prev - diff):min(n, i_prev + diff)])))
            i += max(0, i_prev - diff)

            if i != i_prev:
                kp_source_prev['value'] = (kp_source_prev['value'] + kp_source[i]['value']) / 2
                kp_source_prev['jacobian'] = (kp_source_prev['jacobian'] + kp_source[i]['jacobian']) / 2
                source_prev = (source_prev + source[i]) / 2
                i_prev = i
            else:
                kp_source_prev['value'] = alpha * kp_source_prev['value'] + (1 - alpha) * kp_source[i]['value']
                kp_source_prev['jacobian'] = alpha * kp_source_prev['jacobian'] + (1 - alpha) * kp_source[i]['jacobian']
                source_prev = alpha * source_prev + (1 - alpha) * source[i]

            kp_norm = normalize_kp(kp_source=kp_source_prev, kp_driving=kp_driving,
                                   kp_driving_initial=kp_driving_initial, use_relative_movement=relative,
                                   use_relative_jacobian=relative, adapt_movement_scale=adapt_movement_scale)
            out = generator(source_prev, kp_source=kp_source_prev, kp_driving=kp_norm)

            predictions.append(np.transpose(out['prediction'].data.cpu().numpy(), [0, 2, 3, 1])[0])
    return predictions


def make_photo_animation(source_image, driving_video, generator, kp_detector, relative=True, adapt_movement_scale=True, cpu=False):
    with torch.no_grad():
        predictions = []
        source = torch.tensor(source_image[np.newaxis].astype(np.float32)).permute(0, 3, 1, 2)
        if not cpu:
            source = source.cuda()
        driving = torch.tensor(np.array(driving_video)[np.newaxis].astype(np.float32)).permute(0, 4, 1, 2, 3)
        kp_source = kp_detector(source)
        kp_driving_initial = kp_detector(driving[:, :, 0])

        for frame_idx in tqdm(range(driving.shape[2])):
            driving_frame = driving[:, :, frame_idx]
            if not cpu:
                driving_frame = driving_frame.cuda()
            kp_driving = kp_detector(driving_frame)
            kp_norm = normalize_kp(kp_source=kp_source, kp_driving=kp_driving,
                                   kp_driving_initial=kp_driving_initial, use_relative_movement=relative,
                                   use_relative_jacobian=relative, adapt_movement_scale=adapt_movement_scale)
            out = generator(source, kp_source=kp_source, kp_driving=kp_norm)

            predictions.append(np.transpose(out['prediction'].data.cpu().numpy(), [0, 2, 3, 1])[0])
    return predictions


def find_best_frame(source, driving, cpu=False):
    import face_alignment

    def normalize_kp(kp):
        kp = kp - kp.mean(axis=0, keepdims=True)
        area = ConvexHull(kp[:, :2]).volume
        area = np.sqrt(area)
        kp[:, :2] = kp[:, :2] / area
        return kp

    fa = face_alignment.FaceAlignment(face_alignment.LandmarksType._2D, flip_input=True,
                                      device='cpu' if cpu else 'cuda')
    kp_source = fa.get_landmarks(255 * source)[0]
    kp_source = normalize_kp(kp_source)
    norm = float('inf')
    frame_num = 0
    for i, image in tqdm(enumerate(driving)):
        kp_driving = fa.get_landmarks(255 * image)[0]
        kp_driving = normalize_kp(kp_driving)
        new_norm = (np.abs(kp_source - kp_driving) ** 2).sum()
        if new_norm < norm:
            norm = new_norm
            frame_num = i
    return frame_num

def super_resolution(source_image, modelScale):
    sr = cv2.dnn_superres.DnnSuperResImpl_create()
    sr.readModel('../first-order-model/pretrained_models/ESPCN_x4.pb')
    sr.setModel('espcn', modelScale)
    upscaled = sr.upsample(source_image)
    return upscaled

def read_video(reader):
    video = []
    try:
        for im in reader:
            video.append(im)
    except RuntimeError:
        pass
    reader.close()

    from multiprocessing.pool import ThreadPool
    pool = ThreadPool(12)

    def mapping_resize(frame):
        return resize(frame, (256, 256))[..., :3]

    video = list(pool.map(mapping_resize, video))
    return video

if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--config", required=True, help="path to config")
    parser.add_argument("--checkpoint", default='vox-cpk.pth.tar', help="path to checkpoint to restore")

    parser.add_argument("--source_image", default='sup-mat/source.png', help="path to source image")
    parser.add_argument("--driving_video", default='sup-mat/source.png', help="path to driving video")
    parser.add_argument("--result_video", default='result.mp4', help="path to output")

    parser.add_argument("--relative", dest="relative", action="store_true",
                        help="use relative or absolute keypoint coordinates")
    parser.add_argument("--adapt_scale", dest="adapt_scale", action="store_true",
                        help="adapt movement scale based on convex hull of keypoints")

    parser.add_argument("--find_best_frame", dest="find_best_frame", action="store_true",
                        help="Generate from the frame that is the most alligned with source. (Only for faces, requires face_aligment lib)")

    parser.add_argument("--best_frame", dest="best_frame", type=int, default=None,
                        help="Set frame to start from.")

    parser.add_argument("--cpu", dest="cpu", action="store_true", help="cpu mode.")
    parser.add_argument("--from_image", dest="from_image", action="store_true")

    parser.set_defaults(relative=False)
    parser.set_defaults(adapt_scale=False)
    parser.set_defaults(from_image=False)

    opt = parser.parse_args()

    # opt.cpu = True

    if opt.from_image:
        crop.crop_image(opt.source_image)
    else:
        crop.crop_video(opt.source_image)

    crop.crop_video(opt.driving_video)

    try:
        source_reader = imageio.get_reader('crop_' + opt.source_image)
    except Exception as e:
        print(e)
        source_reader = imageio.get_reader(opt.source_image)

    if opt.from_image:
        source_photo = resize(next(iter(source_reader)), (256, 256))[..., :3]
    else:
        source_video = read_video(source_reader)

    try:
        target_reader = imageio.get_reader('crop_' + opt.driving_video)
    except Exception as e:
        print(e)
        target_reader = imageio.get_reader(opt.driving_video)

    fps = target_reader.get_meta_data()['fps']
    driving_video = read_video(target_reader)

    generator, kp_detector = load_checkpoints(config_path=opt.config, checkpoint_path=opt.checkpoint, cpu=opt.cpu)
    if opt.from_image:
        predictions = make_photo_animation(source_photo, driving_video, generator, kp_detector,
                                           relative=opt.relative,
                                           adapt_movement_scale=opt.adapt_scale,
                                           cpu=opt.cpu)
    else:
        predictions = make_animation(source_video, driving_video, generator, kp_detector,
                                     relative=opt.relative,
                                     adapt_movement_scale=opt.adapt_scale,
                                     cpu=opt.cpu)

    #1024x1024
    imageio.mimsave(opt.result_video, [super_resolution(img_as_ubyte(frame), 4) for frame in predictions], fps=fps)
    
    #256x256
    # imageio.mimsave(opt.result_video, [img_as_ubyte(frame) for frame in predictions], fps=fps)

