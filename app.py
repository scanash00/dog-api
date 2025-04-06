import os
import logging
import random
import traceback
import threading
import time
import json
import gzip
import io
import re
import uuid
import socket
import datetime
import concurrent.futures
from functools import wraps
from threading import Lock
import requests
from flask import Flask, jsonify, request, Response, g
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_talisman import Talisman
from marshmallow import Schema, fields, ValidationError
from dotenv import load_dotenv
import praw
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import psutil

load_dotenv()

log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'logs')
os.makedirs(log_dir, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(os.path.join(log_dir, 'app.log'), encoding='utf-8')
    ]
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

csp = {
    'default-src': "'self'",
    'img-src': ['*', 'data:'],
    'style-src': ["'self'", "'unsafe-inline'"],
    'script-src': ["'self'", "'unsafe-inline'"]
}
talisman = Talisman(
    app, 
    content_security_policy=csp, 
    force_https=False,
    strict_transport_security=False,
    session_cookie_secure=False,
    referrer_policy='no-referrer'
)

allowed_origins = os.getenv('ALLOWED_ORIGINS')
if allowed_origins:
    origins = allowed_origins.split(',')
    CORS(app, resources={r"/*": {"origins": origins}})
else:
    CORS(app)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["1000 per day", "300 per hour", "100 per minute"],
    storage_uri="memory://",
    strategy="fixed-window"
)

reddit_client_id = os.getenv('REDDIT_CLIENT_ID')
reddit_client_secret = os.getenv('REDDIT_CLIENT_SECRET')
reddit_user_agent = os.getenv('REDDIT_USER_AGENT')

if not all([reddit_client_id, reddit_client_secret, reddit_user_agent]):
    logger.error("Reddit API credentials are not set")
    raise ValueError("Reddit API credentials are not set")

reddit = praw.Reddit(
    client_id=reddit_client_id,
    client_secret=reddit_client_secret,
    user_agent=reddit_user_agent
)

redis_client = None
redis_url = os.getenv('REDIS_URL')
if redis_url:
    try:
        import redis
        redis_client = redis.from_url(redis_url)
        redis_client.ping() 
        logger.info("Redis connected successfully")
    except Exception as e:
        logger.warning(f"Redis connection failed: {e}")
        redis_client = None

session = requests.Session()
retry_strategy = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
)
adapter = HTTPAdapter(
    max_retries=retry_strategy,
    pool_connections=10,
    pool_maxsize=100
)
session.mount("http://", adapter)
session.mount("https://", adapter)

DOG_SUBREDDITS = {
    'corgi': 1, 'wigglebutts': 1, 'dogswithjobs': 1, 'dogshowerthoughts': 1, 'bostonterrier': 1, 'blop': 1, 'puppysmiles': 1, 'whatswrongwithyourdog': 1,
    'rarepuppers': 2, 'goldenretrievers': 2, 'germanshepherds': 2, 'labrador': 2, 'husky': 2, 'BorderCollie': 2, 'AustralianShepherd': 2,
    'pitbulls': 3, 'shiba': 3, 'dachshund': 3, 'greatdanes': 3, 'samoyeds': 3, 'bernesemountaindogs': 3, 'boxers': 3, 'pugs': 3, 'chihuahua': 3,
    'beagle': 3, 'dalmatians': 3, 'poodles': 3, 'basset': 3, 'weimaraner': 3, 'schnauzers': 3, 'corgis': 3, 'newfoundlands': 3,
    'irishwolfhound': 3, 'stbernards': 3, 'greyhounds': 3, 'bulldog': 3, 'frenchbulldogs': 3,
    'dogs': 3, 'dogpictures': 3, 'lookatmydog': 3, 'PuppySmiles': 3, 'puppies': 3, 'dogsenjoyingnature': 3, 'servicedogs': 3, 'dogtraining': 3,
    'rescuedogs': 3, 'dogphotography': 3, 'dogsofreddits': 3, 'dogsmirin': 3, 'huskypuppy': 3, 'dogsonboats': 3, 'dogsindisguise': 3,
    'doggles': 3, 'dogsinboots': 3, 'dogsinhats': 3, 'dogsinties': 3, 'dogsinsunglasses': 3, 'dogswithhats': 3, 'dogswithsocks': 3,
    'happydogs': 3, 'sleepingdogs': 3, 'zoomies': 3, 'barkour': 3, 'dogbees': 3, 'dogloaf': 3,
    'dogpile': 3, 'dogsstandingup': 3, 'floof': 3, 'masterreturns': 3, 'notakeonlythrow': 3, 'petthedamndog': 3,
    'puppybellies': 3, 'tippytaps': 3,
    'dogswearinghats': 3, 'dogswitheyebrows': 3, 'DogsWithUnderbites': 3, 'woofbarkwoof': 3, 'beachdogs': 3
}

def clean_url(url):
    if not url:
        return url
    cleaned_url = url.strip()
    cleaned_url = cleaned_url.replace(' ', '%20')
    return cleaned_url

prefetched_images = {}
prefetch_lock = Lock()
subreddit_access_count = {}  
subreddit_error_count = {}  
last_access_time = {}        
thread_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=int(os.getenv('THREAD_POOL_SIZE', '10'))
)

CACHE_TTL = int(os.getenv('CACHE_TTL', '3600')) 
REQUEST_TIMEOUT = int(os.getenv('REQUEST_TIMEOUT', '5')) 
COMPRESSION_THRESHOLD = int(os.getenv('COMPRESSION_THRESHOLD', '1024')) 
PREFETCH_BATCH_SIZE = int(os.getenv('PREFETCH_BATCH_SIZE', '10'))  
PREFETCH_INTERVAL = int(os.getenv('PREFETCH_INTERVAL', '300'))    
MIN_IMAGES_PER_SUBREDDIT = int(os.getenv('MIN_IMAGES_PER_SUBREDDIT', '10'))
MAX_PREFETCH_ERRORS = int(os.getenv('MAX_PREFETCH_ERRORS', '3'))
PREFETCH_RETRY_DELAY = int(os.getenv('PREFETCH_RETRY_DELAY', '30'))

class DogRequestSchema(Schema):
    subreddit = fields.String(required=False)
    limit = fields.Integer(required=False, validate=lambda n: 1 <= n <= 50)
    no_cache = fields.Boolean(required=False)

def compress_response(response):

    if not isinstance(response, Response) and not isinstance(response, str):
        return response
    
    if 'gzip' not in request.headers.get('Accept-Encoding', ''):
        return response
    
    data = response.data if isinstance(response, Response) else response.encode('utf-8')
    
    if len(data) < COMPRESSION_THRESHOLD:
        return response
    
    gzip_buffer = io.BytesIO()
    with gzip.GzipFile(mode='wb', fileobj=gzip_buffer) as f:
        f.write(data)
    
    compressed_data = gzip_buffer.getvalue()
    
    if isinstance(response, Response):
        resp = Response(compressed_data)
        resp.headers = dict(response.headers)
    else:
        resp = Response(compressed_data)
    
    resp.headers['Content-Encoding'] = 'gzip'
    resp.headers['Content-Length'] = str(len(compressed_data))
    
    return resp

def cache_response(ttl_seconds=CACHE_TTL):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            return f(*args, **kwargs)
        return decorated_function
    return decorator

def log_request():
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            logger.info(f"Request: {request.method} {request.path} from {request.remote_addr}")
            
            start_time = time.time()
            response = f(*args, **kwargs)
            duration = time.time() - start_time
            
            status_code = response.status_code if hasattr(response, 'status_code') else 200
            logger.info(f"Response: {status_code} in {duration:.2f}s")
            
            return response
        
        return decorated_function
    
    return decorator

def validate_request(schema):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            try:
                data = {}
                for key, value in request.args.items():
                    data[key] = value
                
                validated_data = schema().load(data)
                g.validated_data = validated_data
                
                return f(*args, **kwargs)
            except ValidationError as e:
                return jsonify({
                    "error": "Validation error",
                    "details": str(e.messages),
                    "status": 400
                }), 400
        
        return decorated_function
    
    return decorator

def is_safe_content(text):
    if not text or not isinstance(text, str):
        return False
    
    unsafe_patterns = [
        r'\b(nsfw|porn|xxx|sex|explicit|nude|naked|adult|obscene)\b',
        r'\b(violence|gore|blood|death|kill|murder|suicide)\b',
        r'\b(racist|racism|nazi|hitler|hate|offensive)\b'
    ]
    
    for pattern in unsafe_patterns:
        if re.search(pattern, text.lower()):
            return False
    
    return True

def fetch_safe_dog_images_from_subreddit(subreddit_name, limit=20):
    try:
        subreddit = reddit.subreddit(subreddit_name)
        image_posts = []
        
        for post in subreddit.hot(limit=limit):
            if post.url.endswith(('.jpg', '.jpeg', '.png', '.gif')):
                if is_safe_content(post.title):
                    post.url = clean_url(post.url)
                    image_posts.append(post)
        
        logger.info(f"Fetched {len(image_posts)} safe dog images from r/{subreddit_name}")
        return image_posts
    
    except Exception as e:
        logger.error(f"Error fetching from r/{subreddit_name}: {e}")
        return []

def prefetch_dog_images_batch():
    start_time = time.time()
    
    subreddits_to_prefetch = []
    with prefetch_lock:
        for subreddit in DOG_SUBREDDITS:
            if subreddit not in prefetched_images:
                prefetched_images[subreddit] = []
                subreddits_to_prefetch.append(subreddit)
            elif len(prefetched_images[subreddit]) < MIN_IMAGES_PER_SUBREDDIT:
                subreddits_to_prefetch.append(subreddit)
    
    if not subreddits_to_prefetch and random.random() < 0.3:
        subreddits_to_prefetch = random.sample(list(DOG_SUBREDDITS.keys()), 
                                              min(PREFETCH_BATCH_SIZE, len(DOG_SUBREDDITS)))
    
    if len(subreddits_to_prefetch) > PREFETCH_BATCH_SIZE:
        subreddits_to_prefetch = random.sample(subreddits_to_prefetch, PREFETCH_BATCH_SIZE)
    
    if not subreddits_to_prefetch:
        logger.info("No subreddits need prefetching at this time")
        return 0
    
    logger.info(f"Prefetching images from {len(subreddits_to_prefetch)} subreddits: {', '.join(subreddits_to_prefetch)}")
    
    with thread_pool as executor:
        future_to_subreddit = {
            executor.submit(fetch_safe_dog_images_from_subreddit, subreddit, 30): 
            subreddit for subreddit in subreddits_to_prefetch
        }
        
        for future in concurrent.futures.as_completed(future_to_subreddit):
            subreddit = future_to_subreddit[future]
            try:
                posts = future.result()
                
                with prefetch_lock:
                    if subreddit not in prefetched_images:
                        prefetched_images[subreddit] = []
                    
                    new_posts = [p for p in posts if p not in prefetched_images[subreddit]]
                    
                    max_cache_size = MIN_IMAGES_PER_SUBREDDIT * 2
                    if len(prefetched_images[subreddit]) + len(new_posts) > max_cache_size:
                        prefetched_images[subreddit] = prefetched_images[subreddit][-(max_cache_size-len(new_posts)):]
                    
                    prefetched_images[subreddit].extend(new_posts)
                    
                    logger.info(f"Added {len(new_posts)} new images to prefetch cache for r/{subreddit}")
                    
            except Exception as e:
                logger.error(f"Error prefetching from r/{subreddit}: {e}")
    
    duration = time.time() - start_time
    logger.info(f"Prefetch batch completed in {duration:.2f}s")
    return duration

def get_random_dog():
    start_time = time.time()
    g.start_time = start_time
    
    try:
        subreddit_param = request.args.get('subreddit', '').strip().lower()
        
        if subreddit_param and subreddit_param in DOG_SUBREDDITS:
            subreddit_to_use = subreddit_param
        else:
            available_subreddits = []
            with prefetch_lock:
                available_subreddits = [s for s in DOG_SUBREDDITS if s in prefetched_images and prefetched_images[s]]
            
            if available_subreddits:
                tier1_available = [s for s in available_subreddits if DOG_SUBREDDITS[s] == 1]
                if tier1_available:
                    subreddit_to_use = random.choice(tier1_available)
                else:
                    subreddit_to_use = random.choice(available_subreddits)
                
                logger.info(f"Using prefetched images from r/{subreddit_to_use}")
                
                with prefetch_lock:
                    random_post = random.choice(prefetched_images[subreddit_to_use])
                    prefetched_images[subreddit_to_use].remove(random_post)
                    return random_post
            
            tier_weights = {
                1: 0.6,  
                2: 0.3,  
                3: 0.1   
            }
            
            tier_to_use = random.choices(
                population=list(tier_weights.keys()),
                weights=list(tier_weights.values()),
                k=1
            )[0]
            
            subreddits_in_tier = [s for s, t in DOG_SUBREDDITS.items() if t == tier_to_use]
            if not subreddits_in_tier:
                subreddits_in_tier = list(DOG_SUBREDDITS.keys())
            
            subreddit_to_use = random.choice(subreddits_in_tier)
        
        logger.info(f"Selected subreddit: r/{subreddit_to_use}")
        
        with prefetch_lock:
            if subreddit_to_use in prefetched_images and prefetched_images[subreddit_to_use]:
                random_post = random.choice(prefetched_images[subreddit_to_use])
                prefetched_images[subreddit_to_use].remove(random_post)
                logger.info(f"Using prefetched image from r/{subreddit_to_use}")
                return random_post
            
        image_posts = fetch_safe_dog_images_from_subreddit(subreddit_to_use, limit=30)
        
        if not image_posts:
            logger.warning(f"No images found in r/{subreddit_to_use}, trying another subreddit")
            
            alternative_subreddits = [s for s in DOG_SUBREDDITS.keys() if s != subreddit_to_use]
            if alternative_subreddits:
                alt_subreddit = random.choice(alternative_subreddits)
                logger.info(f"Trying alternative subreddit: r/{alt_subreddit}")
                image_posts = fetch_safe_dog_images_from_subreddit(alt_subreddit, limit=30)
        
        if not image_posts:
            raise ValueError("No dog images found")
        
        random_post = random.choice(image_posts)
        
        with prefetch_lock:
            if subreddit_to_use not in prefetched_images:
                prefetched_images[subreddit_to_use] = []
            
            other_posts = [p for p in image_posts if p.id != random_post.id and p not in prefetched_images[subreddit_to_use]]
            prefetched_images[subreddit_to_use].extend(other_posts[:MIN_IMAGES_PER_SUBREDDIT])
            
            if other_posts:
                logger.info(f"Added {len(other_posts[:MIN_IMAGES_PER_SUBREDDIT])} new images to prefetch cache for r/{subreddit_to_use}")
        
        return random_post
    
    except Exception as e:
        logger.error(f"Error in get_random_dog: {e}")
        traceback.print_exc()
        raise

@app.route('/random-dog', methods=['GET'])
@limiter.limit("100 per minute")
def random_dog_endpoint():
    try:
        start_time = time.time()
        
        dog_data = get_random_dog()
        
        user_agent = request.headers.get('User-Agent', '').lower()
        accept_header = request.headers.get('Accept', '').lower()
        
        is_discord_request = (
            'discord' in user_agent or 
            'discordbot' in user_agent or 
            'discordapp.com' in user_agent or 
            'preview' in accept_header
        )
        
        is_browser_request = any(
            browser in user_agent for browser in 
            ['mozilla', 'chrome', 'safari', 'firefox', 'opera', 'edge', 'webkit']
        )
        
        if is_discord_request:
            headers = {
                'Content-Type': 'text/plain',
                'Cache-Control': 'no-cache, no-store, must-revalidate',
                'Pragma': 'no-cache',
                'Expires': '0',
                'X-Timestamp': str(time.time())
            }
            return dog_data.url, 200, headers
        
        if is_browser_request:
            return format_dog_response(dog_data, start_time)
        
        return dog_data.url, 200, {'Content-Type': 'text/plain'}
    
    except Exception as e:
        logger.error(f"Error generating random dog: {e}")
        return jsonify({
            "error": "Failed to generate random dog image",
            "status": 500
        }), 500

def format_dog_response(post, start_time):
    response_time = int((time.time() - start_time) * 1000)
    
    response = {
        "title": post.title,
        "url": clean_url(post.url),
        "subreddit": post.subreddit.display_name,
        "upvotes": post.score,
        "source": "reddit",
        "response_time_ms": response_time
    }
    
    return jsonify(response)

@app.before_request
def block_ssl_requests():
    if request.path.startswith('/.well-known/acme-challenge/'):
        return '', 404
    
    if request.path.startswith('/.well-known/pki-validation/'):
        return '', 404
    
    if request.path.startswith('/apple-app-site-association'):
        return '', 404
    
    if request.path.startswith('/.well-known/assetlinks.json'):
        return '', 404

@app.before_request
def log_all_requests():
    g.start_time = time.time()
    logger.debug(f"Request: {request.method} {request.path} from {request.remote_addr}")

@app.after_request
def add_security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['Server'] = 'Dog API'
    
    return response

@app.after_request
def apply_compression(response):
    return compress_response(response)

@app.route('/')
@log_request()
def health_check():
    try:
        redis_status = "ok" if redis_client and redis_client.ping() else "error"
        
        reddit_status = "ok"
        try:
            future = thread_pool.submit(lambda: reddit.subreddit('dogs').display_name)
            future.result(timeout=REQUEST_TIMEOUT)
        except Exception as e:
            reddit_status = "error"
            logger.warning(f"Reddit API check failed: {e}")
        
        with prefetch_lock:
            prefetch_count = sum(len(images) for images in prefetched_images.values())
            prefetch_subreddits = list(prefetched_images.keys())
        
        thread_stats = {
            "active": len([t for t in thread_pool._threads if t.is_alive()]),
            "total": thread_pool._max_workers,
            "queue_size": thread_pool._work_queue.qsize()
        }
        
        system_stats = {
            "hostname": socket.gethostname(),
            "uptime": int(time.time() - app.start_time),
            "memory_usage": f"{psutil.virtual_memory().percent}%",
            "cpu_usage": f"{psutil.cpu_percent(interval=0.1)}%"
        }
        
        start_time = getattr(g, 'start_time', time.time())
        response_time = int((time.time() - start_time) * 1000)
        
        response = {
            "status": "healthy" if all(s == "ok" for s in [redis_status, reddit_status]) else "degraded",
            "timestamp": datetime.datetime.now().isoformat(),
            "version": "1.0.0",
            "services": {
                "redis": redis_status,
                "reddit": reddit_status
            },
            "prefetch": {
                "count": prefetch_count,
                "subreddits": prefetch_subreddits
            },
            "threads": thread_stats,
            "system": system_stats,
            "response_time_ms": response_time
        }
        
        status_code = 200 if response["status"] == "healthy" else 503
        return jsonify(response), status_code
    
    except Exception as e:
        logger.error(f"Health check error: {e}")
        traceback.print_exc()
        return jsonify({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.datetime.now().isoformat()
        }), 500

@app.route('/stats')
@log_request()
@cache_response(ttl_seconds=60)
def api_stats():
    try:
        start_time = getattr(g, 'start_time', time.time())
        
        thread_stats = {
            "active": len([t for t in thread_pool._threads if t.is_alive()]),
            "total": thread_pool._max_workers,
            "utilization": round(len([t for t in thread_pool._threads if t.is_alive()]) / thread_pool._max_workers * 100, 1)
        }
        
        with prefetch_lock:
            prefetch_count = sum(len(images) for images in prefetched_images.values())
            prefetch_age = int(time.time() - getattr(app, 'last_prefetch_time', app.start_time))
            
            popular_subreddits = sorted(
                subreddit_access_count.items(),
                key=lambda x: x[1],
                reverse=True
            )[:5]
            
            error_subreddits = {
                sub: count for sub, count in subreddit_error_count.items() 
                if count > 0
            }
            
            prefetch_distribution = {
                sub: len(posts) for sub, posts in prefetched_images.items()
                if posts
            }
        
        response_time = int((time.time() - start_time) * 1000)
        
        response = {
            "uptime": int(time.time() - app.start_time),
            "requests": {
                "total": getattr(app, 'request_count', 0),
                "random_dog": getattr(app, 'random_dog_count', 0)
            },
            "performance": {
                "threads": thread_stats
            },
            "cache": {
                "enabled": redis_client is not None,
                "prefetched_images": prefetch_count,
                "prefetch_age_seconds": prefetch_age,
                "prefetch_distribution": prefetch_distribution,
                "prefetch_config": {
                    "batch_size": PREFETCH_BATCH_SIZE,
                    "interval": PREFETCH_INTERVAL,
                    "min_images_per_subreddit": MIN_IMAGES_PER_SUBREDDIT
                }
            },
            "subreddits": {
                "total": len(DOG_SUBREDDITS),
                "popular": dict(popular_subreddits),
                "errors": error_subreddits
            },
            "timestamp": datetime.datetime.now().isoformat(),
            "response_time_ms": response_time
        }
        
        return jsonify(response)
    
    except Exception as e:
        logger.error(f"Stats error: {e}")
        return jsonify({
            "error": "Error generating stats",
            "details": str(e)
        }), 500

@app.errorhandler(400)
def bad_request(e):
    return jsonify({
        "error": "Bad request",
        "details": str(e),
        "status": 400
    }), 400

@app.errorhandler(404)
def not_found(e):
    return jsonify({
        "error": "Not found",
        "details": "The requested resource was not found",
        "status": 404
    }), 404

@app.errorhandler(429)
def ratelimit_handler(e):
    return jsonify({
        "error": "Rate limit exceeded",
        "details": str(e.description),
        "retry_after": e.retry_after if hasattr(e, 'retry_after') else None,
        "status": 429
    }), 429

@app.errorhandler(500)
def server_error(e):
    return jsonify({
        "error": "Internal server error",
        "details": str(e),
        "status": 500
    }), 500

@app.before_request
def record_request_start_time():
    g.start_time = time.time()
    app.request_count = getattr(app, 'request_count', 0) + 1
    if request.path == '/random-dog':
        app.random_dog_count = getattr(app, 'random_dog_count', 0) + 1

app.start_time = time.time()

def start_prefetching():
    def prefetch_worker():
        logger.info("Starting prefetch worker thread")
        
        consecutive_errors = 0
        last_prefetch_time = time.time()
        
        while True:
            try:
                current_time = time.time()
                time_since_last_prefetch = current_time - last_prefetch_time
                
                total_prefetched = 0
                with prefetch_lock:
                    total_prefetched = sum(len(images) for images in prefetched_images.values())
                
                need_prefetch = False
                
                if total_prefetched < MIN_IMAGES_PER_SUBREDDIT * PREFETCH_BATCH_SIZE:
                    need_prefetch = True
                    logger.info(f"Triggering prefetch due to low total image count ({total_prefetched})")
                
                elif time_since_last_prefetch >= PREFETCH_INTERVAL:
                    need_prefetch = True
                    logger.info(f"Triggering prefetch due to interval ({time_since_last_prefetch:.2f}s)")
                
                else:
                    with prefetch_lock:
                        popular_subs = sorted(
                            subreddit_access_count.keys(), 
                            key=lambda s: subreddit_access_count.get(s, 0),
                            reverse=True
                        )[:5]  
                        
                        for sub in popular_subs:
                            if sub not in prefetched_images or len(prefetched_images.get(sub, [])) < MIN_IMAGES_PER_SUBREDDIT:
                                need_prefetch = True
                                logger.info(f"Triggering prefetch due to popular subreddit {sub} being low on images")
                                break
                
                if need_prefetch:
                    last_prefetch_time = current_time
                    duration = prefetch_dog_images_batch()
                    consecutive_errors = 0
                    
                    sleep_time = max(10, min(PREFETCH_INTERVAL // 2, duration * 2))
                else:
                    sleep_time = 30
                
                logger.info(f"Prefetch worker sleeping for {sleep_time:.2f}s")
                time.sleep(sleep_time)
                
            except Exception as e:
                consecutive_errors += 1
                logger.error(f"Error in prefetch worker (consecutive errors: {consecutive_errors}): {e}")
                
                sleep_time = min(PREFETCH_INTERVAL, PREFETCH_RETRY_DELAY * (2 ** (consecutive_errors - 1)))
                logger.info(f"Prefetch worker backing off for {sleep_time}s after error")
                time.sleep(sleep_time)
    
    prefetch_thread = threading.Thread(target=prefetch_worker, daemon=True)
    prefetch_thread.start()
    logger.info("Prefetch worker thread started")

start_prefetching()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', 5000)), debug=False, threaded=True)
