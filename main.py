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
import threading
from queue import Queue

#Globals
w, h = pyautogui.size() #Captures Screen Resolution [1920x1080]
monitor_area = {"top":0, "left":0, "width": w, "height": h}

#Queues for thread-safe comms
screenshot_queue = Queue(maxsize = 3)
detection_queue = Queue(maxsize = 3)


#Thread 1
def get_screenshot():
    with mss.mss() as sct:
        while True:
            img = sct.grab(monitor_area)
            screenshot = np.array(img)
            #if detection is falling behind, dropping old frams lets pipeline tay real-time and not delayed
            if screenshot_queue.full():
                screenshot_queue.get_nowait() #remove and return an item without waiting, drops oldest screenshot
            screenshot_queue.put(screenshot) #thread-safe put
#Thread 2
def detect_objects():
    print("Detecting Objects...")
    while True:
        screenshot = screenshot_queue.get()
        #detection code
        detected_SS = screenshot.copy()
        detection_queue.put(detected_SS)

#Thread 3
def draw_and_output():
    t0 = time.time()
    n_frames = 1

    while True:
        screenshot = detection_queue.get()
        output_img = cv.resize(screenshot, (0,0), fx=0.5, fy=0.5) #can move to right after screenshot takes to improve fps
        cv.imshow("Hunters Eye", output_img)

        if cv.waitKey(1) == ord('q'):
            break

        #realtime FPS
        elapsed_time = time.time() - t0
        avg_fps = (n_frames / elapsed_time)
        print(f"FPS: {avg_fps:.2f}")
        n_frames += 1

    cv.destroyAllWindows()  
    #Final Stats [prints average Runtime FPS]
    t_final = time.time()
    total_elapsed = t_final - t0
    average_FPS = n_frames / total_elapsed
    print("total # of frames:", n_frames)
    print(f"Total Runtime: {total_elapsed:.2f}")
    print(f"Average FPS: {average_FPS:.2f}")


def main():
    print("Starting Hunter's Eye...")

    t1 = threading.Thread(target=get_screenshot, daemon=True)
    t2 = threading.Thread(target=detect_objects, daemon=True)
    t3 = threading.Thread(target=draw_and_output)

    t1.start()
    t2.start()
    t3.start()

    t3.join() #keeps program alive until 'q' pressed

if __name__ == "__main__":
    main()