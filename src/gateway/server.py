import os, gridfs, pika, json
import logging  # ADDED
from flask import Flask, request, send_file
from flask_pymongo import PyMongo
from auth import validate
from auth_svc import access
from storage import util
from bson.objectid import ObjectId

# ADDED: Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

server = Flask(__name__)

# ADDED: Log MongoDB initialization
logger.info("Initializing MongoDB connections...")
mongo_video = PyMongo(server, uri="mongodb://mongo:27017/videos")
mongo_mp3 = PyMongo(server, uri="mongodb://mongo:27017/mp3s")

fs_videos = gridfs.GridFS(mongo_video.db)
fs_mp3s = gridfs.GridFS(mongo_mp3.db)

# ADDED: Better RabbitMQ connection handling with retry
max_retries = 3
retry_count = 0
connection = None
channel = None

while retry_count < max_retries:
    try:
        logger.info(f"Attempting to connect to RabbitMQ (attempt {retry_count + 1}/{max_retries})...")
        connection = pika.BlockingConnection(pika.ConnectionParameters("rabbitmq"))
        channel = connection.channel()
        logger.info("Connected to RabbitMQ successfully!")
        break
    except Exception as e:
        retry_count += 1
        logger.error(f"Failed to connect to RabbitMQ: {e}")
        if retry_count < max_retries:
            import time
            time.sleep(2)  # Wait 2 seconds before retry
        else:
            logger.error("Max retries reached. Could not connect to RabbitMQ.")
            # Depending on your needs, you might want to exit or continue without RabbitMQ
            # raise e  # Uncomment to exit on failure

if channel:
    # Declare queues to ensure they exist
    channel.queue_declare(queue="video", durable=True)
    channel.queue_declare(queue="mp3", durable=True)
else:
    logger.warning("RabbitMQ channel not available. File processing will fail.")


@server.route("/login", methods=["POST"])
def login():
    logger.info("Login endpoint called")
    token, err = access.login(request)

    if not err:
        logger.info("Login successful")
        return token
    else:
        logger.warning(f"Login failed: {err}")
        return err


@server.route("/upload", methods=["POST"])
def upload():
    logger.info("Upload endpoint called")
    
    access_token, err = validate.token(request)

    if err:
        logger.warning(f"Token validation failed: {err}")
        return err

    access_data = json.loads(access_token)
    logger.info(f"User {access_data['username']} is uploading")

    if access_data["admin"]:
        logger.info(f"Admin user detected. Files received: {len(request.files)}")
        
        if len(request.files) > 1 or len(request.files) < 1:
            logger.warning("Wrong number of files uploaded")
            return "exactly 1 file required", 400

        for filename, f in request.files.items():
            logger.info(f"Processing file: {f.filename}, size: {f.content_length}")
            err = util.upload(f, fs_videos, channel, access_data)

            if err:
                logger.error(f"Upload failed: {err}")
                return err

        logger.info("Upload completed successfully")
        return "success!", 200
    else:
        logger.warning(f"Non-admin user {access_data['username']} attempted upload")
        return "not authorized", 401


@server.route("/download", methods=["GET"])
def download():
    logger.info("Download endpoint called")
    
    access_token, err = validate.token(request)

    if err:
        logger.warning(f"Token validation failed: {err}")
        return err

    access_data = json.loads(access_token)

    if access_data["admin"]:
        fid_string = request.args.get("fid")
        logger.info(f"Download requested for file ID: {fid_string}")

        if not fid_string:
            logger.warning("Download requested without file ID")
            return "fid is required", 400

        try:
            out = fs_mp3s.get(ObjectId(fid_string))
            logger.info(f"File {fid_string} found, sending to user")
            return send_file(out, download_name=f"{fid_string}.mp3")
        except Exception as err:
            logger.error(f"Download error: {err}")
            return "internal server error", 500

    logger.warning(f"Non-admin user {access_data['username']} attempted download")
    return "not authorized", 401


# ADDED: Health check endpoint
@server.route("/health", methods=["GET"])
def health():
    # Check MongoDB connection
    try:
        mongo_video.db.command('ping')
        mongo_mp3.db.command('ping')
        mongo_ok = True
    except Exception as e:
        logger.error(f"MongoDB health check failed: {e}")
        mongo_ok = False
    
    # Check RabbitMQ connection
    rabbitmq_ok = connection.is_open if connection else False
    
    if mongo_ok and rabbitmq_ok:
        return "OK", 200
    else:
        return f"MongoDB: {mongo_ok}, RabbitMQ: {rabbitmq_ok}", 503


if __name__ == "__main__":
    logger.info("Starting gateway server...")
    server.run(host="0.0.0.0", port=8080)