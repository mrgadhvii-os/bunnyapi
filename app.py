from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit
import re
import requests
import yt_dlp
from hashlib import md5
from html import unescape
from random import random
from urllib.parse import urlparse, urlunparse
import os
import tempfile
import threading
import time
from functools import partial
import shutil

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app)

def get_safe_temp_dir(download_id):
    base_temp_dir = '/tmp/video_downloads'  # Use /tmp for Vercel
    temp_dir = os.path.join(base_temp_dir, download_id)
    os.makedirs(temp_dir, exist_ok=True)
    return temp_dir

class VideoDownloader:
    def __init__(self):
        self.user_agent = {
            'sec-ch-ua': '"Google Chrome";v="107", "Chromium";v="107", "Not=A?Brand";v="24"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Linux"',
            'user-agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36'
        }
        self.session = requests.session()
        self.session.headers.update(self.user_agent)
        self.download_progress = {}
        self.download_speed = {}
        self.fragment_counts = {}
        self.conversion_status = {}
        self.file_size = {}
        self.active_downloads = {}
        self.completed_files = {}
        self.base_download_dir = '/tmp/video_downloads'
        os.makedirs(self.base_download_dir, exist_ok=True)

    def clean_url(self, url):
        url_components = urlparse(url)
        return urlunparse((url_components.scheme, url_components.netloc, url_components.path, '', '', ''))

    def get_video_info(self, url):
        clean_url = self.clean_url(url)
        embed_response = self.session.get(clean_url, headers={
            'authority': 'iframe.mediadelivery.net',
            'accept': '*/*',
            'referer': clean_url,
            'sec-fetch-dest': 'iframe',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'cross-site',
            'upgrade-insecure-requests': '1'
        })
        
        embed_page = embed_response.text
        server_id = re.search(r'https://video-(.*?)\.mediadelivery\.net', embed_page)
        if not server_id:
            raise ValueError("Could not find server ID")
        
        server_id = server_id.group(1)
        context_match = re.search(r'contextId=(.*?)&secret=(.*?)"', embed_page)
        if not context_match:
            raise ValueError("Could not find context ID and secret")
        
        context_id, secret = context_match.groups()
        guid = urlparse(clean_url).path.split('/')[-1]
        
        file_name_match = re.search(r'og:title" content="(.*?)"', embed_page)
        if not file_name_match:
            file_name = f'video_{int(time.time())}'
        else:
            file_name = unescape(file_name_match.group(1))
        
        file_name = re.sub(r'[<>:"/\\|?*]', '', file_name)  # Remove invalid characters
        file_name = re.sub(r'\s+', '_', file_name)  # Replace spaces with underscores
        file_name = re.sub(r'[^\w\-.]', '', file_name)  # Remove any other non-word characters
        
        if not file_name.lower().endswith('.mp4'):
            file_name = file_name.rstrip('.') + '.mp4'
            
        if not file_name or file_name == '.mp4':
            file_name = f'video_{int(time.time())}.mp4'
        
        return {
            'server_id': server_id,
            'context_id': context_id,
            'secret': secret,
            'guid': guid,
            'file_name': file_name,
            'clean_url': clean_url
        }

    def get_resolutions(self, guid, context_id, secret, clean_url):
        playlist_response = self.session.get(
            f'https://iframe.mediadelivery.net/{guid}/playlist.drm',
            params={'contextId': context_id, 'secret': secret},
            headers={'authority': 'iframe.mediadelivery.net', 'accept': '*/*', 'referer': clean_url}
        )
        
        resolutions = re.findall(r'RESOLUTION=(\d+x\d+)', playlist_response.text)[::-1]
        if not resolutions:
            raise ValueError("No resolutions found")
        
        return resolutions

    def prepare_download(self, server_id, context_id, secret, guid, resolution):
        time_val = 0
        paused = 'true'
        res = '0'
        hash_val = md5(f'{secret}_{context_id}_{time_val}_{paused}_{res}'.encode('utf8')).hexdigest()
        
        self.session.get(
            f'https://video-{server_id}.mediadelivery.net/.drm/{context_id}/ping',
            params={'hash': hash_val, 'time': time_val, 'paused': paused, 'chosen_res': res},
            headers={'accept': '*/*', 'origin': 'https://iframe.mediadelivery.net', 'referer': 'https://iframe.mediadelivery.net/'}
        )
        
        self.session.get(
            f'https://video-{server_id}.mediadelivery.net/.drm/{context_id}/activate',
            headers={'accept': '*/*', 'origin': 'https://iframe.mediadelivery.net', 'referer': 'https://iframe.mediadelivery.net/'}
        )

        for i in range(0, 29, 4):
            time_val = i + round(random(), 6)
            self.session.get(
                f'https://video-{server_id}.mediadelivery.net/.drm/{context_id}/ping',
                params={'hash': md5(f'{secret}_{context_id}_{time_val}_false_{resolution.split("x")[-1]}'.encode('utf8')).hexdigest(), 'time': time_val, 'paused': 'false', 'chosen_res': resolution.split('x')[-1]},
                headers={'accept': '*/*', 'origin': 'https://iframe.mediadelivery.net', 'referer': 'https://iframe.mediadelivery.net/'}
            )

    def progress_hook(self, download_id, d):
        if d['status'] == 'downloading':
            total_bytes = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded_bytes = d.get('downloaded_bytes', 0)
            speed = d.get('speed', 0)
            
            if 'fragment_index' in d and 'fragment_count' in d:
                self.fragment_counts[download_id] = {'total': d['fragment_count'], 'downloaded': d['fragment_index'] + 1}
                socketio.emit('fragment_progress', {'id': download_id, 'downloaded': d['fragment_index'] + 1, 'total': d['fragment_count']})
            
            if total_bytes > 0:
                progress = (downloaded_bytes / total_bytes) * 100
                self.download_progress[download_id] = progress
                self.download_speed[download_id] = speed
                self.file_size[download_id] = total_bytes
                
                socketio.emit('download_progress', {
                    'id': download_id,
                    'progress': progress,
                    'speed': speed,
                    'size': total_bytes,
                    'downloaded': downloaded_bytes
                })
        elif d['status'] == 'finished':
            socketio.emit('conversion_started', {'id': download_id})

    def start_download(self, download_id, video_info, resolution, filename):
        try:
            self.active_downloads[download_id] = True
            
            # Use Vercel-compatible temp directory
            temp_dir = get_safe_temp_dir(download_id)
            temp_output_path = os.path.join(temp_dir, filename)
            
            self.prepare_download(
                video_info['server_id'],
                video_info['context_id'],
                video_info['secret'],
                video_info['guid'],
                resolution
            )
            
            url = [f'https://iframe.mediadelivery.net/{video_info["guid"]}/{resolution}/video.drm?contextId={video_info["context_id"]}']
            
            ydl_opts = {
                'http_headers': {'Referer': video_info['clean_url'], 'User-Agent': self.user_agent['user-agent']},
                'concurrent_fragment_downloads': 32,
                'fragment_retries': 10,
                'retries': 10,
                'progress_hooks': [partial(self.progress_hook, download_id)],
                'paths': {'temp': temp_dir, 'home': temp_dir},
                'outtmpl': {'default': temp_output_path},
                'merge_output_format': 'mp4',
                'postprocessors': [{'key': 'FFmpegVideoConvertor', 'preferedformat': 'mp4'}]
            }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download(url)
            
            # Ensure file is saved before marking as complete
            if os.path.exists(temp_output_path):
                self.completed_files[download_id] = {
                    'path': temp_output_path, 
                    'filename': filename
                }
                socketio.emit('download_complete', {'id': download_id, 'download_url': f'/download/{download_id}'})
            else:
                raise ValueError("Download failed: File not created")
            
            self.active_downloads[download_id] = False
            return True
            
        except Exception as e:
            self.active_downloads[download_id] = False
            socketio.emit('download_error', {'id': download_id, 'error': str(e)})
            return False

downloader = VideoDownloader()

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/check_url', methods=['POST'])
def check_url():
    try:
        url = request.json.get('url')
        if not url:
            return jsonify({'error': 'URL is required'}), 400
            
        video_info = downloader.get_video_info(url)
        resolutions = downloader.get_resolutions(
            video_info['guid'],
            video_info['context_id'],
            video_info['secret'],
            video_info['clean_url']
        )
        
        return jsonify({'video_info': video_info, 'resolutions': resolutions})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/start_download', methods=['POST'])
def start_download():
    try:
        data = request.json
        video_info = data.get('video_info')
        resolution = data.get('resolution')
        filename = data.get('filename', video_info['file_name'])
        download_id = str(time.time())
        
        if not all([video_info, resolution]):
            return jsonify({'error': 'Missing required parameters'}), 400
            
        threading.Thread(
            target=downloader.start_download,
            args=(download_id, video_info, resolution, filename)
        ).start()
        
        return jsonify({'download_id': download_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/download/<download_id>')
def download_file(download_id):
    if download_id not in downloader.completed_files:
        return "File not found", 404
        
    file_info = downloader.completed_files[download_id]
    file_path = file_info['path']
    filename = file_info['filename']
    
    if not os.path.exists(file_path):
        return "File not found", 404

    def generate():
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                yield chunk
        
        # Clean up after download
        try:
            os.remove(file_path)
            shutil.rmtree(os.path.dirname(file_path), ignore_errors=True)
            del downloader.completed_files[download_id]
        except Exception as e:
            print(f"Cleanup error: {e}")

    return app.response_class(
        generate(),
        mimetype='video/mp4',
        headers={'Content-Disposition': f'attachment; filename="{filename}.mp4"', 'Cache-Control': 'no-cache'}
    )

# Configure Socket.IO for Vercel
if 'VERCEL' in os.environ:
    socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')
else:
    socketio = SocketIO(app)

# Add CORS headers
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response

if __name__ == '__main__':
    socketio.run(app, debug=True)
