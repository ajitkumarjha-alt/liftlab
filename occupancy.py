#!/usr/bin/env python3
"""Detection-only cabin occupancy — the LOAD proxy (directional boarded/alighted is
descoped: 2fps YOLO can't sustain ByteTrack transit tracking; that's the Hailo case).
No tracker, no IDs: per frame, run onnx YOLO11n, count persons whose FOOT falls inside
zone_cabin. Runs under the B4 venv (onnxruntime + cv2 + numpy already present). Used by
continuous_scheduler as a PARALLEL sampler grafted onto the committed-cycle path — the
door/openness pipeline is untouched."""
import cv2
import numpy as np
import onnxruntime as ort


def _in_poly(pt, poly):
    """Ray casting. Cabin cams are barrel-distorted, so zone_cabin is a polygon."""
    x, y = pt
    inside = False
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xin = (x2 - x1) * (y - y1) / (y2 - y1 + 1e-12) + x1
            if x < xin:
                inside = not inside
    return inside


class CabinCounter:
    """Per-frame count of persons standing in zone_cabin. onnx CPU, 3 threads (the
    door loop keeps a core), imgsz 640 (fixed model), person = class 0."""

    def __init__(self, model_path, zone_cabin, conf=0.35, nms=0.5, threads=3):
        so = ort.SessionOptions()
        so.intra_op_num_threads = threads
        so.inter_op_num_threads = 1
        self.sess = ort.InferenceSession(model_path, sess_options=so, providers=["CPUExecutionProvider"])
        self.name = self.sess.get_inputs()[0].name
        self.zone = [(float(x), float(y)) for x, y in zone_cabin]
        self.conf = conf
        self.nms = nms

    def _pre(self, frame):
        x = cv2.cvtColor(cv2.resize(frame, (640, 640)), cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        return np.transpose(x, (2, 0, 1))[None]

    def count(self, frame_bgr):
        """Persons with foot inside zone_cabin. Returns int (0 on any failure)."""
        try:
            h, w = frame_bgr.shape[:2]
            out = self.sess.run(None, {self.name: self._pre(frame_bgr)})[0]   # [1,84,8400]
            scores = out[0, 4, :]                                            # class 0 = person
            idx = np.where(scores > self.conf)[0]
            if len(idx) == 0:
                return 0
            sx, sy = w / 640.0, h / 640.0
            boxes, confs = [], []
            for i in idx:
                cx, cy, bw, bh = out[0, 0, i] * sx, out[0, 1, i] * sy, out[0, 2, i] * sx, out[0, 3, i] * sy
                boxes.append([float(cx - bw / 2), float(cy - bh / 2), float(bw), float(bh)])
                confs.append(float(scores[i]))
            keep = cv2.dnn.NMSBoxes(boxes, confs, self.conf, self.nms)
            if keep is None or len(keep) == 0:
                return 0
            n = 0
            for k in np.array(keep).flatten():
                x1, y1, bw, bh = boxes[int(k)]
                foot = (x1 + bw / 2.0, y1 + bh)          # bottom-center = where they stand
                if _in_poly(foot, self.zone):
                    n += 1
            return n
        except Exception:
            return 0
