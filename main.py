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
needle_image = cv.imread("image1.png")
needle_image = cv.resize(needle_image, (0,0) , fx=0.5, fy=0.5)
needle_w = needle_image.shape[1]
needle_h = needle_image.shape[0]

#Queues for thread-safe comms
screenshot_queue = Queue(maxsize = 3)
detection_queue = Queue(maxsize = 3)

#Thread 1
def get_screenshot():
    with mss.mss() as sct:
        while True:
            img = sct.grab(monitor_area)
            screenshot = np.array(img)
            if screenshot.shape[2] == 4:
                screenshot = cv.cvtColor(screenshot, cv.COLOR_BGRA2BGR) #changes from BGRA to BGR [ stripping alpha val]
            screenshot = cv.resize(screenshot, (0,0) ,fx=0.5, fy=0.5) #resizing image for better performance 

            #if detection is falling behind, dropping old frams lets pipeline tay real-time and not delayed
            if screenshot_queue.full():
                screenshot_queue.get_nowait() #remove and return an item without waiting, drops oldest screenshot
            screenshot_queue.put(screenshot) #thread-safe put

#Thread 2
def detect_objects():
    print("Detecting Objects...")
    while True:
        screenshot = screenshot_queue.get()
        #detection code-----------

        result = cv.matchTemplate(screenshot, needle_image, method=cv.TM_CCOEFF_NORMED) #returns confidence score
        threshold = 0.60
        max_results = 10 #Limiting results #
        locations = np.where(result >= threshold) #array([334,335]), array [91,92]
        #zip into position tuples:
        locations = list(zip(*locations[::-1]))

        #building list
        rectangles = []
        for loc in locations:
            rect = [int(loc[0]), int(loc[1]), needle_w, needle_h]
            rectangles.append(rect)
        #grouping all rectangles that are close to eachother
        rectangles, weights = cv.groupRectangles(rectangles, 1, 0.5)
        #print(rectangles)
        if len(rectangles) > max_results:
            print("Too Many Results, raise the threshhold [max_results]")
            rectangles = rectangles[max_results:] #trancades results if > threshhold

        #--------------------------
        detected_SS = screenshot.copy()
        detection_queue.put((detected_SS,rectangles)) #puts screenshot and rectangle locations in Queue

#Thread 3
def draw_and_output():
    t0 = time.time()
    n_frames = 1

    while True:
        screenshot, rectangles = detection_queue.get()
        for (x,y,w,h) in rectangles:
            cv.rectangle(screenshot, (x,y), (x+w,y+h), (0,255,0), 2)
        cv.imshow("Hunters Eye", screenshot)

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