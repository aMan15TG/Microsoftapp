from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
import azure.cognitiveservices.speech as speechsdk
import threading
import queue
import logging
import json
import pyaudio
import subprocess
import uuid
import os
import openai

app = Flask(__name__)
CORS(app, resources={r"/stream_audio": {"origins": "*"}})
logging.basicConfig(level=logging.DEBUG)

openai.api_key = "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"

speech_key, service_region = "1yrNDe16LKhM1wGakO5XQs97S19hNZNvupqZ92UEJyM42zO3s72yJQQJ99AJACYeBjFXJ3w3AAAYACOGefVs", "eastus"

# Global variables for live streaming
is_streaming = False
transcription_queue = queue.Queue()
translation_queues = {}
audio_queue = queue.Queue()
is_recording = False
recording_file = None
RECORDINGS_DIR = "recordings"

# Ensure the recordings directory exists
if not os.path.exists(RECORDINGS_DIR):
    os.makedirs(RECORDINGS_DIR)

# Audio settings
CHUNK = 512
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 16000

async def translate_with_openai(text, target_language):
    language_prompts = {
        'hi': 'Translate this English text to grammatically correct Hindi, using proper Hindi sentence structure and vocabulary. Use Devanagari script only.',
        'ja': 'Translate this English text to natural, grammatically correct Japanese. Use Japanese script only.',
        'ko': 'Translate this English text to natural, grammatically correct Korean. Use Hangul script only.',
        'zh': 'Translate this English text to natural, grammatically correct Chinese. Use Simplified Chinese characters only.',
        'ar': 'Translate this English text to natural, grammatically correct Arabic. Use Arabic script only.'
    }
    
    try:
        prompt = language_prompts.get(target_language, f'Translate to {target_language}')
        
        response = await openai.ChatCompletion.acreate(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": f"{prompt} Respond with only the translation, no explanations, no question responseor or romanization."},
                {"role": "user", "content": text}
            ],
            temperature=0.1  # Lower temperature for more consistent translations
        )
        
        return response.choices[0].message.content.strip()
    except Exception as e:
        logging.error(f"Translation error: {str(e)}")
        return text
    

@app.route('/')
def home():
    return render_template('home.html')

@app.route('/start_recording', methods=['POST'])
def start_recording():
    global is_recording, recording_file
    if not is_recording:
        # Generate a unique filename using UUID
        filename = f"transcription_{uuid.uuid4()}.txt"
        file_path = os.path.join(RECORDINGS_DIR, filename)
        recording_file = open(file_path, 'w', encoding='utf-8')
        is_recording = True
        logging.debug(f"Recording started: {file_path}")
        return jsonify({"status": "recording_started", "file": filename}), 200
    else:
        return jsonify({"status": "already_recording"}), 400

@app.route('/stop_recording', methods=['POST'])
def stop_recording():
    global is_recording, recording_file
    if is_recording and recording_file:
        recording_file.close()
        logging.debug("Recording stopped")
        is_recording = False
        recording_file = None
        return jsonify({"status": "recording_stopped"}), 200
    else:
        return jsonify({"status": "not_recording"}), 400


@app.route('/go_live')
def go_live():
    return render_template('go_live1.html')

@app.route('/join_live')
def join_live():
    return render_template('join_live.html')

@app.route('/translate_join', methods=['POST'])
async def translate_join():
    data = request.get_json()
    text = data.get('text', '')
    target_language = data.get('targetLanguage', '')

    if not text or not target_language:
        return jsonify({'error': 'Missing required parameters'}), 400

    try:
        translated_text = await translate_with_openai(text, target_language)
        return jsonify({'translatedText': translated_text})
    except Exception as e:
        logging.error(f"Translation error: {str(e)}")
        return jsonify({
            'error': 'Translation failed',
            'details': str(e)
        }), 500

@app.route('/start_stream', methods=['POST'])
def start_stream():
    global is_streaming
    if not is_streaming:
        is_streaming = True
        threading.Thread(target=stream_audio).start()
        logging.debug("Streaming started")
    return jsonify({"status": "started"})

@app.route('/stop_stream', methods=['POST'])
def stop_stream():
    global is_streaming
    is_streaming = False
    logging.debug("Streaming stopped")
    return jsonify({"status": "stopped"})

import logging

@app.route('/translate', methods=['POST'])
async def translate():
    data = request.get_json()
    text = data.get('text', '')
    target_language = data.get('targetLanguage', '')

    if not text or not target_language:
        return jsonify({'error': 'Missing parameters'}), 400

    try:
        translated_text = await translate_with_openai(text, target_language)
        return jsonify({'translatedText': translated_text})
    except Exception as e:
        logging.error(f"Translation error: {str(e)}")
        return jsonify({'error': str(e)}), 500


@app.route('/stream_transcription')
def stream_transcription():
    def generate():
        while True:
            try:
                # Get the latest transcription from the queue
                transcription = transcription_queue.get(timeout=1)
                # Send the transcription as a Server-Sent Event
                yield f"data: {json.dumps({'transcription': transcription})}\n\n"
            except queue.Empty:
                # Send keepalive message every second if no new transcription
                yield f"data: {json.dumps({'keepalive': True})}\n\n"
    
    response = Response(generate(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Access-Control-Allow-Origin'] = '*'
    return response

@app.route('/stream_status')
def stream_status():
    return jsonify({'is_streaming': is_streaming})

@app.route('/stream_translation/<string:lang>')
def stream_translation(lang):
    def generate():
        logging.info(f"Started streaming translations for language: {lang}")
        q = queue.Queue()
        translation_queues[lang] = q
        try:
            while is_streaming:
                try:
                    translation = q.get(timeout=1)
                    logging.info(f"Sending translation for {lang}: {translation}")
                    yield f"data: {json.dumps({'translation': translation})}\n\n"
                except queue.Empty:
                    yield f"data: {json.dumps({'keepalive': True})}\n\n"
        except GeneratorExit:
            logging.info(f"Client disconnected from stream_translation/{lang}")
        finally:
            if lang in translation_queues:
                del translation_queues[lang]
    
    response = Response(generate(), content_type='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    return response

@app.route('/stream_audio')
def stream_audio_route():
    def generate():
        ffmpeg_process = subprocess.Popen(
            ['ffmpeg', '-i', '-', '-c:a', 'libopus', '-f', 'webm', '-'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL
        )

        def reader_thread():
            while is_streaming:
                try:
                    chunk = audio_queue.get(timeout=1)
                    ffmpeg_process.stdin.write(chunk)
                except queue.Empty:
                    pass

        threading.Thread(target=reader_thread).start()

        while is_streaming:
            output = ffmpeg_process.stdout.read(1024)
            if output:
                yield output

        ffmpeg_process.stdin.close()
        ffmpeg_process.wait()

    response = Response(generate(), mimetype='audio/webm')
    response.headers['Cache-Control'] = 'no-cache'
    return response


def stream_audio():
    speech_config = speechsdk.SpeechConfig(subscription=speech_key, region=service_region)
    speech_config.speech_recognition_language = "en-US"
    audio_config = speechsdk.audio.AudioConfig(use_default_microphone=True)
    
    speech_recognizer = speechsdk.SpeechRecognizer(speech_config=speech_config, audio_config=audio_config)
    
    # Open the text file for writing
    with open("recognized_text.txt", "a") as text_file:
        def recognized_cb(evt):
            text = evt.result.text
            logging.info(f"Recognized: {text}")
            transcription_queue.put(text)
            text_file.write(text)  

        def recognizing_cb(evt):
            text = evt.result.text
            logging.info(f"Recognizing: {text}")
            transcription_queue.put(text)
    
    speech_recognizer.recognized.connect(recognized_cb)
    speech_recognizer.recognizing.connect(recognizing_cb)
    
    speech_recognizer.start_continuous_recognition()
    
    p = pyaudio.PyAudio()
    stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
    
    global is_streaming
    while is_streaming:
        data = stream.read(CHUNK)
        audio_queue.put(data)
    
    stream.stop_stream()
    stream.close()
    p.terminate()
    speech_recognizer.stop_continuous_recognition()

if __name__ == '__main__':
    app.run(debug=True, threaded=True)




# from flask import Flask, render_template, request, jsonify, Response
# from flask_cors import CORS
# import openai
# import threading
# import queue
# import logging
# import json
# import pyaudio
# import subprocess
# import uuid
# import os

# app = Flask(__name__)
# CORS(app, resources={r"/stream_audio": {"origins": "*"}})
# logging.basicConfig(level=logging.DEBUG)

# openai.api_key = "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"

# # Global variables for live streaming
# is_streaming = False  
# transcription_queue = queue.Queue()
# audio_queue = queue.Queue()
# is_recording = False
# recording_file = None
# RECORDINGS_DIR = "recordings"

# # Ensure the recordings directory exists
# if not os.path.exists(RECORDINGS_DIR):
#     os.makedirs(RECORDINGS_DIR)

# # Audio settings
# CHUNK = 512
# FORMAT = pyaudio.paInt16  
# CHANNELS = 1
# RATE = 16000

# async def translate_with_openai(text, target_language):
#     try:
#         response = await openai.Completion.acreate(
#             engine="text-davinci-003",
#             prompt=f"Translate the following English text to {target_language}:\n\n{text}\n\nTranslation:",
#             max_tokens=1024
#         )
        
#         return response.choices[0].text.strip()
#     except Exception as e:
#         logging.error(f"Translation error: {str(e)}")
#         return text
    

# @app.route('/')
# def home():
#     return render_template('home.html')

# @app.route('/start_recording', methods=['POST'])
# def start_recording():
#     global is_recording, recording_file
#     if not is_recording:
#         # Generate a unique filename using UUID
#         filename = f"transcription_{uuid.uuid4()}.txt"
#         file_path = os.path.join(RECORDINGS_DIR, filename)  
#         recording_file = open(file_path, 'w', encoding='utf-8')
#         is_recording = True
#         logging.debug(f"Recording started: {file_path}")  
#         return jsonify({"status": "recording_started", "file": filename}), 200
#     else:
#         return jsonify({"status": "already_recording"}), 400

# @app.route('/stop_recording', methods=['POST'])
# def stop_recording():
#     global is_recording, recording_file
#     if is_recording and recording_file:
#         recording_file.close()
#         logging.debug("Recording stopped")
#         is_recording = False
#         recording_file = None
#         return jsonify({"status": "recording_stopped"}), 200
#     else:
#         return jsonify({"status": "not_recording"}), 400


# @app.route('/go_live')
# def go_live():
#     return render_template('go_live1.html')

# @app.route('/join_live') 
# def join_live():
#     return render_template('join_live.html')

# @app.route('/translate_join', methods=['POST'])
# async def translate_join():
#     data = request.get_json()
#     text = data.get('text', '')
#     target_language = data.get('targetLanguage', '')

#     if not text or not target_language:
#         return jsonify({'error': 'Missing required parameters'}), 400

#     try:
#         translated_text = await translate_with_openai(text, target_language)  
#         return jsonify({'translatedText': translated_text})
#     except Exception as e:
#         logging.error(f"Translation error: {str(e)}")
#         return jsonify({
#             'error': 'Translation failed',
#             'details': str(e)  
#         }), 500

# @app.route('/start_stream', methods=['POST'])
# def start_stream():
#     global is_streaming
#     if not is_streaming:
#         is_streaming = True
#         threading.Thread(target=stream_audio).start()
#         logging.debug("Streaming started")  
#     return jsonify({"status": "started"})

# @app.route('/stop_stream', methods=['POST'])
# def stop_stream():
#     global is_streaming
#     is_streaming = False
#     logging.debug("Streaming stopped") 
#     return jsonify({"status": "stopped"})

# @app.route('/translate', methods=['POST'])
# async def translate():
#     data = request.get_json()
#     text = data.get('text', '')
#     target_language = data.get('targetLanguage', '')

#     if not text or not target_language:  
#         return jsonify({'error': 'Missing parameters'}), 400

#     try:
#         translated_text = await translate_with_openai(text, target_language)
#         return jsonify({'translatedText': translated_text})  
#     except Exception as e:
#         logging.error(f"Translation error: {str(e)}")
#         return jsonify({'error': str(e)}), 500


# @app.route('/stream_transcription') 
# def stream_transcription():
#     def generate():
#         while True:
#             try:
#                 # Get the latest transcription from the queue
#                 transcription = transcription_queue.get(timeout=1) 
#                 # Send the transcription as a Server-Sent Event
#                 yield f"data: {json.dumps({'transcription': transcription})}\n\n"
#             except queue.Empty:
#                 # Send keepalive message every second if no new transcription
#                 yield f"data: {json.dumps({'keepalive': True})}\n\n" 
    
#     response = Response(generate(), mimetype='text/event-stream')
#     response.headers['Cache-Control'] = 'no-cache'
#     response.headers['X-Accel-Buffering'] = 'no'
#     response.headers['Access-Control-Allow-Origin'] = '*'
#     return response

# @app.route('/stream_status')
# def stream_status():
#     return jsonify({'is_streaming': is_streaming})  

# @app.route('/stream_audio')
# def stream_audio_route():
#     def generate():
#         ffmpeg_process = subprocess.Popen(  
#             ['ffmpeg', '-i', '-', '-c:a', 'libopus', '-f', 'webm', '-'], 
#             stdin=subprocess.PIPE,
#             stdout=subprocess.PIPE,
#             stderr=subprocess.DEVNULL
#         )

#         def reader_thread():
#             while is_streaming: 
#                 try:
#                     chunk = audio_queue.get(timeout=1)
#                     ffmpeg_process.stdin.write(chunk)
#                 except queue.Empty:  
#                     pass

#         threading.Thread(target=reader_thread).start()

#         while is_streaming:
#             output = ffmpeg_process.stdout.read(1024)  
#             if output:
#                 yield output

#         ffmpeg_process.stdin.close()
#         ffmpeg_process.wait()  

#     response = Response(generate(), mimetype='audio/webm') 
#     response.headers['Cache-Control'] = 'no-cache'
#     return response


# def stream_audio():
#     p = pyaudio.PyAudio()
#     stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, input=True, frames_per_buffer=CHUNK)
    
#     def recognized_cb(text):
#         logging.debug(f"Recognized: {text}")
#         transcription_queue.put(text)
        
#         if is_recording and recording_file:
#             recording_file.write(text)
    
#     global is_streaming
#     while is_streaming:
#         data = stream.read(CHUNK)
#         audio_queue.put(data)
        
#         # Stream audio to OpenAI for recognition
#         response = openai.Audio.atranscribe("whisper-1", data, language="en")
#         recognized_cb(response)
    
#     stream.stop_stream()
#     stream.close()
#     p.terminate()

# if __name__ == '__main__':
#     app.run(debug=True, threaded=True)