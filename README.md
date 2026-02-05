# ACT/AAU absence API
A script that extracts absence count from the official student portal of ACT/AAU.

## To run:
Have python and the required dependencies installed.   
Provide the two required enviroment variables (**PORTAL_USERNAME** and **PORTAL_PASSWORD**). You can also provide the **STUDY_PROFILE_ID** found in the *academic-convergences* metadata in case the automatic detections fails.

## Result:
The app will start a webserver on which you can find a JSON structure under */absences* that contains per-course information (course name, absence count).   
Example:
<img width="492" height="245" alt="image" src="https://github.com/user-attachments/assets/9b6cd6a9-2093-4ced-935a-b94fd46bd14c" />

