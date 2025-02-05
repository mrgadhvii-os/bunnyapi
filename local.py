#
# The BunnyCDN DRM Video Downloader is a powerful tool designed
# to facilitate the downloading of DRM-protected videos hosted on BunnyCDN.
# With this tool, users can easily download their favorite videos from BunnyCDN's
# content delivery network, even if they are protected with
# Digital Rights Management (DRM) technology.
#


# Import required modules
import re
import sys
import requests
import yt_dlp
from hashlib import md5
from html import unescape
from random import random
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
from flask import Flask, render_template, request, send_file, jsonify, after_this_request
import os
from werkzeug.utils import secure_filename
from apscheduler.schedulers.background import BackgroundScheduler
import glob
from datetime import datetime, timedelta
import atexit
from flask_socketio import SocketIO
import json
import time
from contextlib import suppress
import threading
from flask_cors import CORS
import logging

# Disable engineio logging
logging.getLogger('engineio').setLevel(logging.ERROR)
logging.getLogger('socketio').setLevel(logging.ERROR)
logging.getLogger('werkzeug').setLevel(logging.ERROR)

app = Flask(__name__)
CORS(app)

# Configure SocketIO for local development
socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode='threading',
    logger=False,
    engineio_logger=False
)

# Create tmp directory if it doesn't exist
if not os.path.exists('tmp'):
    os.makedirs('tmp')

# Define path to tmp directory
path = 'tmp'

# Generating Sessions using custom user-agent
user_agent = {
    'sec-ch-ua': '"Google Chrome";v="107", "Chromium";v="107", "Not=A?Brand";v="24"',
    'sec-ch-ua-mobile': '?0',
    'sec-ch-ua-platform': '"Linux"',
    'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36'
}

# Create scheduler for tmp cleanup
scheduler = BackgroundScheduler()

def cleanup_tmp():
    """Clean up tmp directory - remove files older than 15 minutes"""
    current_time = datetime.now()
    for file in glob.glob('tmp/*.mp4'):
        file_modified = datetime.fromtimestamp(os.path.getmtime(file))
        if current_time - file_modified > timedelta(minutes=1):
            try:
                os.remove(file)
                print(f"Deleted old file: {file}")
            except Exception as e:
                print(f"Error deleting {file}: {e}")

# Start the scheduler
scheduler.add_job(func=cleanup_tmp, trigger="interval", minutes=15)
scheduler.start()

# Add these global variables after the existing imports
PING_COUNT = 0
last_ping_time = None

# Add this route before the existing routes
@app.route('/ping-stats')
def ping_stats():
    global PING_COUNT, last_ping_time
    return jsonify({
        "total_pings": PING_COUNT,
        "last_ping": last_ping_time.strftime("%Y-%m-%d %H:%M:%S") if last_ping_time else None
    })

@app.route('/')
def index():
    return render_template('index.html')

@socketio.on('connect')
def handle_connect():
    print('✓ Client connected')

@socketio.on('disconnect')
def handle_disconnect():
    print('× Client disconnected')

@socketio.on('get_qualities')
def get_qualities(data):
    try:
        url = data.get('url')
        if not url:
            print('× No URL provided')
            socketio.emit('qualities', {'error': 'URL is required'})
            return

        # Clean URL and setup
        url_components = urlparse(url)
        clean_url = urlunparse((url_components.scheme, url_components.netloc, url_components.path, '', '', ''))
        print(f'→ Processing: {clean_url}')

        session = requests.Session()
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/91.0.4472.124'
        })

        # Get embed page
        response = session.get(clean_url)
        embed_page = response.text
        video_id = url_components.path.split('/')[-1]

        # Extract parameters
        server_id = re.search(r'https://video-(.*?)\.mediadelivery\.net', embed_page)
        if not server_id:
            print('× Invalid video URL')
            socketio.emit('qualities', {'error': 'Invalid video URL'})
            return

        params_match = re.search(r'contextId=(.*?)&secret=(.*?)"', embed_page)
        if not params_match:
            print('× Could not find video parameters')
            socketio.emit('qualities', {'error': 'Could not find video parameters'})
            return

        context_id, secret = params_match.groups()

        # Get playlist
        playlist_url = f'https://iframe.mediadelivery.net/{video_id}/playlist.drm'
        playlist = session.get(
            playlist_url,
            params={'contextId': context_id, 'secret': secret},
            headers={'Referer': clean_url}
        ).text

        # Parse resolutions
        resolutions = [
            match.group(1) 
            for match in re.finditer(r'RESOLUTION=(\d+x\d+)', playlist)
        ]

        if not resolutions:
            print('× No qualities found')
            socketio.emit('qualities', {'error': 'No qualities found'})
            return

        # Map qualities
        quality_map = {
            '426x240': '240p',
            '640x360': '360p',
            '854x480': '480p',
            '1280x720': '720p',
            '1920x1080': '1080p'
        }

        qualities = [
            {'resolution': res, 'name': quality_map.get(res, res)} 
            for res in resolutions
        ]

        print(f'✓ Found qualities: {[q["name"] for q in qualities]}')
        socketio.emit('qualities', {'qualities': qualities})

    except Exception as e:
        print(f'× Error: {str(e)}')
        socketio.emit('qualities', {'error': str(e)})

class ProgressHook:
    def __init__(self, socket):
        self.socket = socket
        self.current_state = "Processing"

    def __call__(self, d):
        if d['status'] == 'downloading':
            try:
                total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                downloaded_bytes = d.get('downloaded_bytes', 0)
                speed = d.get('speed', 0)
                eta = d.get('eta', 0)
                fragment_count = d.get('fragment_count', 0)
                fragment_index = d.get('fragment_index', 0)

                progress = {
                    'status': 'downloading',
                    'percentage': (downloaded_bytes / total_bytes * 100) if total_bytes else 0,
                    'total_size': f"{total_bytes / 1024 / 1024:.2f}MiB",
                    'speed': f"{speed / 1024 / 1024:.2f}MiB/s",
                    'eta': str(timedelta(seconds=eta)),
                    'fragment': f"{fragment_index}/{fragment_count}",
                    'downloaded': f"{downloaded_bytes / 1024 / 1024:.2f}MiB"
                }
                socketio.emit('download_progress', progress)
            except Exception as e:
                print(f"Error in progress hook: {e}")

        elif d['status'] == 'finished':
            socketio.emit('download_progress', {
                'status': 'converting',
                'message': 'Converting to MP4...'
            })

@app.route('/download', methods=['POST'])
def download_video():
    output_path = None
    try:
        url = request.form.get('url')
        selected_quality = request.form.get('quality')
        custom_filename = request.form.get('filename')
        
        if not url or not selected_quality:
            return jsonify({'error': 'URL and quality are required'}), 400

        # Define output filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if custom_filename:
            file_name = f"{custom_filename} - From @MrGadhvii.mp4"
        else:
            file_name = f"video_{timestamp} - From @MrGadhvii.mp4"
        
        file_name = secure_filename(file_name)
        # Use tmp directory in current path
        output_path = 'tmp/' + file_name
        print(f"Output path: {output_path}")

        session = requests.session()
        session.headers.update(user_agent)

        # Parse URL
        url_components = urlparse(url)
        url = urlunparse((url_components.scheme, url_components.netloc, url_components.path, '', '', ''))
        
        referer = url
        embed_url = url
        guid = urlparse(embed_url).path.split('/')[-1]

        # Get embed page
        embed_response = session.get(embed_url)
        embed_page = embed_response.text

        # Extract server ID and parameters
        server_id = re.search(r'https://video-(.*?)\.mediadelivery\.net', embed_page).group(1)
        search = re.search(r'contextId=(.*?)&secret=(.*?)"', embed_page)
        if not search:
            return jsonify({'error': 'Could not find video parameters'}), 400
            
        context_id, secret = search.group(1), search.group(2)

        # Headers for different requests
        headers = {
            'ping|activate': {
                'accept': '*/*',
                'accept-language': 'en-US,en;q=0.9',
                'cache-control': 'no-cache',
                'origin': 'https://iframe.mediadelivery.net',
                'pragma': 'no-cache',
                'referer': 'https://iframe.mediadelivery.net/',
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-site',
                'authority': f'video-{server_id}.mediadelivery.net'
            },
            'playlist': {
                'authority': 'iframe.mediadelivery.net',
                'accept': '*/*',
                'accept-language': 'en-US,en;q=0.9',
                'cache-control': 'no-cache',
                'pragma': 'no-cache',
                'referer': embed_url,
                'sec-fetch-dest': 'empty',
                'sec-fetch-mode': 'cors',
                'sec-fetch-site': 'same-origin',
            }
        }

        # Prepare DRM session
        def ping(time: int, paused: str, res: str):
            md5_hash = md5(f'{secret}_{context_id}_{time}_{paused}_{res}'.encode('utf8')).hexdigest()
            params = {
                'hash': md5_hash,
                'time': time,
                'paused': paused,
                'chosen_res': res
            }
            session.get(f'https://video-{server_id}.mediadelivery.net/.drm/{context_id}/ping', 
                       params=params, 
                       headers=headers['ping|activate'])

        def activate():
            session.get(f'https://video-{server_id}.mediadelivery.net/.drm/{context_id}/activate', 
                       headers=headers['ping|activate'])

        def main_playlist():
            params = {'contextId': context_id, 'secret': secret}
            response = session.get(f'https://iframe.mediadelivery.net/{guid}/playlist.drm', 
                                 params=params, 
                                 headers={'Referer': embed_url})
            return selected_quality  # We already have the quality from form

        def video_playlist():
            params = {'contextId': context_id}
            session.get(f'https://iframe.mediadelivery.net/{guid}/{selected_quality}/video.drm', 
                       params=params, 
                       headers={'Referer': embed_url})

        # Initialize DRM session
        ping(time=0, paused='true', res='0')
        activate()
        resolution = main_playlist()
        video_playlist()
        for i in range(0, 29, 4):
            ping(time=i + round(random(), 6), paused='false', res=resolution.split('x')[-1])

        # Prepare download URL - Note: only contextId, no secret
        url = [f'https://iframe.mediadelivery.net/{guid}/{resolution}/video.drm?contextId={context_id}']

        # Simplified yt-dlp options
        ydl_opts = {
            'http_headers': {
                'Referer': embed_url,
                'User-Agent': user_agent['user-agent']
            },
            'concurrent_fragment_downloads': 10,
            'nocheckcertificate': True,
            'outtmpl': output_path,
            'restrictfilenames': True,
            'windowsfilenames': True,
            'nopart': True,
            'retries': float('inf'),
            'extractor_retries': float('inf'),
            'fragment_retries': float('inf'),
            'skip_unavailable_fragments': False,
            'no_warnings': True,
            'progress_hooks': [ProgressHook(socketio)]
        }

        # Emit starting status
        socketio.emit('download_progress', {
            'status': 'starting',
            'message': 'Initializing download...'
        })

        # Download video
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download(url)

        # Emit completion status
        socketio.emit('download_progress', {
            'status': 'complete',
            'message': 'Download complete!'
        })

        # Check both possible paths
        actual_path = output_path.replace('/', '\\')  # Convert to Windows path
        print(f"Checking path: {actual_path}")
        
        if os.path.exists(actual_path):
            output_path = actual_path  # Use the Windows path if found

        if not os.path.exists(output_path):
            raise Exception(f"Download completed but file not found at: {output_path}")

        # Send file
        response = send_file(
            output_path,
            as_attachment=True,
            mimetype='video/mp4'
        )

        @after_this_request
        def cleanup(response):
            delayed_file_cleanup(output_path)
            return response

        return response

    except Exception as e:
        # Emit error status
        socketio.emit('download_progress', {
            'status': 'error',
            'message': f'Error: {str(e)}'
        })
        print(f"Download error: {str(e)}")
        if output_path:
            # Try both path formats for cleanup
            paths_to_try = [output_path, output_path.replace('/', '\\')]
            for path in paths_to_try:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        print(f"Cleaned up file: {path}")
                        break
                    except Exception as del_err:
                        print(f"Error deleting file: {del_err}")

        return jsonify({'error': str(e)}), 500

def delayed_file_cleanup(file_path, delay=2):
    """Clean up file with retries after a delay"""
    def cleanup():
        time.sleep(delay)  # Wait for file handle to be released
        retries = 3
        while retries > 0:
            try:
                if os.path.exists(file_path):
                    # Force close any open handles
                    with open(file_path, 'r') as _:
                        pass
                    os.remove(file_path)
                    print(f"Successfully deleted: {file_path}")
                break
            except Exception as e:
                print(f"Retry {4-retries}: Error deleting {file_path}: {e}")
                retries -= 1
                time.sleep(1)
    
    # Start cleanup in separate thread
    threading.Thread(target=cleanup, daemon=True).start()

def cleanup_tmp_directory():
    print("Starting cleanup thread...")
    while True:
        try:
            print("\n=== Starting cleanup check ===")
            current_time = time.time()
            
            # Clean main tmp directory
            tmp_path = 'tmp'  # Use relative tmp directory
            if os.path.exists(tmp_path):
                files = glob.glob(os.path.join(tmp_path, '*'))
                print(f"Found {len(files)} files in tmp directory")
                
                for file in files:
                    try:
                        file_age = current_time - os.path.getctime(file)
                        hours_old = file_age / 3600  # Convert seconds to hours for display
                        print(f"Checking file: {file} (Age: {hours_old:.1f} hours)")
                        
                        if file_age > 14400:  # 4 hours = 14400 seconds
                            os.remove(file)
                            print(f"✓ Cleaned up: {file}")
                        else:
                            print(f"- Keeping: {file} (too new)")
                            
                    except Exception as e:
                        print(f"! Error cleaning file {file}: {e}")
            else:
                print("tmp directory does not exist")
                
            # Clean .tmp subdirectory
            tmp_temp = os.path.join(tmp_path, '.tmp')
            if os.path.exists(tmp_temp):
                temp_files = glob.glob(os.path.join(tmp_temp, '*'))
                print(f"\nFound {len(temp_files)} files in .tmp directory")
                
                for tmp_file in temp_files:
                    try:
                        os.remove(tmp_file)
                        print(f"✓ Cleaned up temp file: {tmp_file}")
                    except Exception as e:
                        print(f"! Error cleaning temp file {tmp_file}: {e}")
            else:
                print(".tmp subdirectory does not exist")
                
            print("=== Cleanup check completed ===\n")
            
        except Exception as e:
            print(f"! Cleanup error: {e}")
            
        time.sleep(14400)  # Check every 4 hours

# Start the cleanup thread when app starts
print("Initializing cleanup thread...")
cleanup_thread = threading.Thread(target=cleanup_tmp_directory, daemon=True)
cleanup_thread.start()
print("Cleanup thread started!")

@app.route('/health')
def health_check():
    global PING_COUNT, last_ping_time
    PING_COUNT += 1
    last_ping_time = datetime.now()
    return jsonify({
        "status": "healthy",
        "total_pings": PING_COUNT,
        "last_ping": last_ping_time.strftime("%Y-%m-%d %H:%M:%S")
    }), 200

def keep_alive():
    print("Keep-alive service started...")
    while True:
        try:
            # Get the server's own URL from environment or default to localhost
            server_url = os.getenv('SERVER_URL', 'http://localhost:5000')
            response = requests.get(f'{server_url}/health')
            if response.status_code == 200:
                print(f"✓ Keep-alive ping successful. Total pings: {PING_COUNT}")
            else:
                print(f"× Keep-alive ping failed with status: {response.status_code}")
        except Exception as e:
            print(f"× Keep-alive ping failed: {str(e)}")
        time.sleep(840)  # 14 minutes - Render's free tier times out at 15 minutes

# Start the keep-alive thread when app starts
print("Starting keep-alive thread...")
keep_alive_thread = threading.Thread(target=keep_alive, daemon=True)
keep_alive_thread.start()

if __name__ == '__main__':
    if not os.path.exists('tmp'):
        os.makedirs('tmp')
    print('\n⚡ Server running on http://localhost:5000\n')
    socketio.run(app, 
                host='0.0.0.0',           # Required for external access
                port=int(os.getenv('PORT', 5000)),  # Use PORT env var or default to 5000
                debug=False,              # Set to False for production
                allow_unsafe_werkzeug=True # Required for newer Flask versions
    )

# Cleanup scheduler on app shutdown
@atexit.register
def shutdown():
    scheduler.shutdown()
