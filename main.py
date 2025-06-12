'''
========================================================================================================
Hunters-Eye: Real-Time On-Screen Object Detection System
========================================================================================================

Author: Zelgehai Zahid
Repository: https://github.com/zel/Hunters_Eye
'''

import cv2 as cv
import pyautogui
import mss
import numpy as np
import time

w, h = pyautogui.size() #Captures Screen Resolution
print("Screen Resolution:", w, 'x', h)
img = None
t0 = time.time()
n_frames = 1
monitor_area = {"top":0, "left":0, "width": w, "height": h}

#test

with mss.mss() as sct:
    while True:
        img = sct.grab(monitor_area)
        img = np.array(img)
        small = cv.resize(img, (0,0), fx = 0.5, fy = 0.5)
        cv.imshow("computer vision", small)

        #end test
        key = cv.waitKey(1)
        if key == ord('q'):
            break

        elapsed_time = time.time() - t0
        avg_fps = (n_frames / elapsed_time)
        print(f"FPS: {avg_fps:.2f}")
        n_frames += 1
#Final Stats [prints average Runtime FPS]
t_final = time.time()
total_elapsed = t_final - t0
average_FPS = n_frames / total_elapsed
print("total # of frames:", n_frames)
print(f"Total Runtime: {total_elapsed:.2f}")
print(f"Average FPS: {average_FPS:.2f}")
