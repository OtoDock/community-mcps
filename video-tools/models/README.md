YuNet face detection model (face_detection_yunet_2023mar.onnx)
Source: https://github.com/opencv/opencv_zoo (models/face_detection_yunet)
Author: Shiqi Yu — MIT License (see LICENSE in this directory).
Used by smart_reframe for subject-aware 9:16 cropping.

VitTracker object tracking model (object_tracking_vittrack_2023sep.onnx)
Source: https://github.com/opencv/opencv_zoo (models/object_tracking_vittrack)
Author: OpenCV Zoo contributors — Apache-2.0 License.
Intended for track_object / smart_reframe follow mode / blur_region via
cv2.TrackerVit — currently DISABLED by default: opencv-python-headless
5.0.0's new DNN graph engine mangles this model's output (zero boxes,
constant ~0.11 score; measured 2026-07-22). TrackerMIL is the shipping
engine; set VIDEO_TOOLS_TRACKER=vit to re-test after an opencv upgrade.

RNNoise voice denoise model (rnnoise-voice.rnnn)
Source: https://github.com/GregorR/rnnoise-models
("beguiling-drafter-2018-08-30" — voice signal vs recording noise).
Upstream states the models are not creative work and not subject to
copyright (public domain); RNNoise itself is BSD-3.
Used by ffmpeg's arnndn for `denoise: "voice"` / enhance_audio voice preset.
